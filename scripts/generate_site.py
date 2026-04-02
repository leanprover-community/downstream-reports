#!/usr/bin/env python3
"""Generate a static HTML status page for the GitHub Pages site.

Usage (SQL backend — production):
    python3 scripts/generate_site.py \\
        --backend sql \\
        --run-id "$RUN_ID" \\
        --output site/index.html

    The connection string is read from the POSTGRES_DSN environment variable.
    If --run-id is omitted the latest regression run is used.

Usage (filesystem backend — local development):
    python3 scripts/generate_site.py \\
        --backend filesystem \\
        --state-root <path> \\
        --output site/index.html
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UPSTREAM_REPO = "leanprover-community/mathlib4"
THIS_REPO = "leanprover-community/hopscotch-reports"
GITHUB = "https://github.com"
GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def esc(s: Any) -> str:
    return _html.escape(str(s)) if s is not None else ""


def short_sha(sha: str | None) -> str | None:
    return sha[:7] if sha else None


def commit_link(
    repo: str,
    sha: str | None,
    title: str | None = None,
    tag: str | None = None,
    date: str | None = None,
) -> str:
    if not sha:
        return "<span class='none'>—</span>"
    url = f"{GITHUB}/{repo}/commit/{sha}"
    display = tag if tag else short_sha(sha)
    date_str = date[:10] if date else None  # YYYY-MM-DD
    NL = "&#10;"
    if tag:
        parts: list[str] = [short_sha(sha)]  # type: ignore[list-item]
        if date_str:
            parts.append(date_str)
        if title:
            parts.append(title)
        tooltip = NL.join(esc(p) for p in parts)
    else:
        parts = []
        if date_str:
            parts.append(date_str)
        if title:
            parts.append(title)
        tooltip = NL.join(esc(p) for p in parts) if parts else esc(sha)
    cls = "sha sha-tag" if tag else "sha"
    return f'<a href="{esc(url)}" class="{cls}" data-tooltip="{tooltip}" target="_blank" rel="noopener noreferrer">{esc(display)}</a>'


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return esc(iso)


# ---------------------------------------------------------------------------
# GitHub API helpers (memoized)
# ---------------------------------------------------------------------------

def fetch_commit_titles(
    shas: set[str],
    repo: str,
    token: str | None,
) -> dict[str, dict[str, str | None]]:
    """Return {sha: {"title": ..., "date": ...}} for every SHA in *shas*.

    *title* is the first line of the commit message; *date* is the committer
    date as an ISO 8601 string.  On any error the SHA maps to
    ``{"title": None, "date": None}`` so callers can fall back gracefully.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hopscotch-reports/generate_site",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    cache: dict[str, dict[str, str | None]] = {}
    for sha in sorted(shas):  # deterministic order for predictable log output
        url = f"{GITHUB_API}/repos/{repo}/commits/{sha}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            message: str = data.get("commit", {}).get("message", "") or ""
            date: str | None = data.get("commit", {}).get("committer", {}).get("date") or None
            cache[sha] = {
                "title": message.splitlines()[0] if message else None,
                "date": date,
            }
        except Exception as exc:
            print(f"  warning: could not fetch commit title for {sha[:7]}: {exc}")
            cache[sha] = {"title": None, "date": None}

    return cache


def fetch_tags(
    repo: str,
    token: str | None,
    max_pages: int = 5,
) -> dict[str, str]:
    """Return {full_sha: tag_name} for the most recent tags in *repo*.

    Fetches up to *max_pages* × 100 tags (newest first).  On any error the
    partial result collected so far is returned so callers degrade gracefully.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hopscotch-reports/generate_site",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        url = f"{GITHUB_API}/repos/{repo}/tags?per_page=100&page={page}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if not data:
                break
            for tag in data:
                sha = tag.get("commit", {}).get("sha")
                name = tag.get("name")
                if sha and name and sha not in result:
                    result[sha] = name
        except Exception as exc:
            print(f"  warning: could not fetch tags for {repo} (page {page}): {exc}")
            break
    return result


COL_DESC = {
    "downstream":      "Project tested updating the Mathlib dependency",
    "compatibility":   "Compatibility of the downstream with the target Mathlib revision\n(based on the result of the latest validation run)",
    "target":          "Mathlib revision targeted in the latest validation run",
    "last_known_good": "Latest Mathlib revision compatible with the downstream",
    "first_known_bad": "Earliest Mathlib revision incompatible with the downstream",
    "pinned":          "Mathlib revision in the downstream's lake manifest",
    "age":             "Days between 'pinned' and 'target' (commit count below)",
    "bump":            "Commits that can be safely advanced ('pinned' -> 'last known good')",
}

COMPATIBILITY_CLASS = {
    "passed": "badge-green",
    "failed": "badge-red",
    "error":  "badge-yellow",
}
COMPATIBILITY_LABEL = {
    "passed": "compatible",
    "failed": "incompatible",
    "error":  "error",
}
COMPATIBILITY_TOOLTIP = {
    "passed": "Compatible with the target Mathlib commit",
    "failed": "Incompatible with the target Mathlib commit",
    "error":  "CI job encountered an unexpected error",
}
EPISODE_CLASS = {
    "passing":     "badge-green",
    "recovered":   "badge-green",
    "new_failure": "badge-red",
    "failing":     "badge-red",
    "error":       "badge-yellow",
}
EPISODE_LABEL = {
    "passing":     "compatible",
    "recovered":   "recovered",
    "new_failure": "new incompatibility",
    "failing":     "incompatible",
    "error":       "error",
}
EPISODE_TOOLTIP = {
    "passing":     "Has been compatible consistently",
    "recovered":   "Was incompatible but is now compatible",
    "new_failure": "Newly incompatible — was compatible in the previous run",
    "failing":     "Has been incompatible across multiple runs",
    "error":       "CI job has been erroring across multiple runs",
}


def badge(value: str | None, cls_map: dict, label_map: dict | None = None, tooltip_map: dict | None = None) -> str:
    if not value:
        return "<span class='none'>—</span>"
    cls = cls_map.get(value, "badge-grey")
    label = (label_map or {}).get(value, value)
    tooltip = (tooltip_map or {}).get(value)
    tooltip_attr = f' data-tooltip="{esc(tooltip)}"' if tooltip else ""
    return f'<span class="badge {esc(cls)}"{tooltip_attr}>{esc(label)}</span>'


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_from_filesystem(state_root: Path) -> tuple[dict, list[dict]]:
    """Return (run_meta, rows) from the filesystem backend's latest.json.

    Reads from the filesystem backend's latest.json.
    to the shorter SQL column names so the render function works with both.
    """
    latest_path = state_root / "reports" / "latest.json"
    if not latest_path.exists():
        raise FileNotFoundError(f"No latest.json found at {latest_path}")

    data = json.loads(latest_path.read_text())

    run_meta = {
        "run_id":       data.get("run_id", "unknown"),
        "upstream_ref": data.get("upstream_ref", "unknown"),
        "run_url":      data.get("run_url", ""),
        "reported_at":  data.get("reported_at"),
        "started_at":   None,
    }

    rows = []
    for r in data.get("results", []):
        row = dict(r)
        rows.append(row)

    return run_meta, rows


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """\
:root {
  --green:  #22863a;
  --red:    #b31d28;
  --yellow: #b08800;
  --grey:   #6a737d;
  --bg:     #f6f8fa;
  --border: #e1e4e8;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 14px; background: var(--bg); color: #24292e; margin: 0; padding: 0;
}
main { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
.run-banner {
  background: #fff; border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 16px; margin-bottom: 16px; font-size: 13px;
}
.run-banner-title {
  font-size: 17px; font-weight: 600; color: #24292e; margin-bottom: 8px;
}
.run-banner-meta {
  display: flex; gap: 20px; flex-wrap: wrap; align-items: center;
}
.run-banner a { color: #0366d6; text-decoration: none; }
.run-banner a:hover { text-decoration: underline; }
.run-banner .divider { color: var(--border); }
.stats { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
.stat {
  background: #fff; border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 20px; text-align: center; min-width: 80px;
}
.stat .n { font-size: 26px; font-weight: 600; }
.stat .l { font-size: 11px; color: var(--grey); text-transform: uppercase; letter-spacing: .04em; margin-top: 2px; }
.stat.green  .n { color: var(--green); }
.stat.red    .n { color: var(--red); }
.stat.yellow .n { color: var(--yellow); }
.table-wrap {
  border: 1px solid var(--border); border-radius: 6px;
}
table {
  width: 100%; border-collapse: collapse;
  background: #fff;
}
th {
  background: #f1f3f5; border-bottom: 1px solid var(--border);
  padding: 8px 12px; text-align: left; font-size: 11px;
  font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: var(--grey);
}
.report-desc {
  background: #fff; border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 16px; margin-bottom: 16px; font-size: 13px;
}
.report-desc-intro { margin: 0 0 8px; color: #57606a; }
.report-desc-list { margin: 0 0 8px; padding-left: 20px; color: #57606a; display: flex; flex-direction: column; gap: 4px; }
.report-desc-footer { margin: 0; color: #57606a; }
.glossary-toggle { margin-top: 10px; }
.glossary-toggle summary {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12px; font-weight: 600; color: #57606a;
  background: #f1f3f5; border: 1px solid var(--border); border-radius: 4px;
  padding: 3px 10px;
  cursor: pointer; user-select: none; list-style: none;
}
.glossary-toggle summary:hover { background: #e8eaed; color: #24292e; }
.glossary-toggle summary::-webkit-details-marker { display: none; }
.glossary-toggle summary::before {
  content: "▶"; font-size: 9px; transition: transform .15s;
}
.glossary-toggle[open] summary::before { transform: rotate(90deg); }
.col-glossary { display: flex; flex-direction: row; gap: 24px; margin-top: 8px; flex-wrap: wrap; align-items: flex-start; }
.col-glossary-col { display: flex; flex-direction: column; gap: 3px; flex: 1 1 0; min-width: 0; }
.col-glossary-item { display: flex; gap: 6px; align-items: baseline; font-size: 12px; }
.col-glossary-key {
  font-weight: 600; text-transform: uppercase; font-size: 11px;
  letter-spacing: .04em; color: var(--grey); white-space: nowrap; min-width: 120px;
}
.col-glossary-val { color: #57606a; }
.col-glossary-badge-intro { font-size: 12px; color: #57606a; margin: 0 0 6px; font-weight: 500; }
.col-glossary-badge-item { display: flex; gap: 8px; align-items: center; font-size: 12px; }
.col-glossary-badge-key { white-space: nowrap; min-width: 120px; }
.col-glossary-badge-val { color: #57606a; }
td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tbody tr.data-row:hover td { background: #f6f8fa; }
.name { font-weight: 600; font-size: 13px; }
.name a { color: #24292e; text-decoration: none; }
.name a:hover { color: #0366d6; }
.repo-label { font-size: 11px; color: var(--grey); }
.episode-label { margin-top: 4px; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: 11px; font-weight: 600; white-space: nowrap;
}
.badge-green  { background: #dcffe4; color: var(--green); }
.badge-red    { background: #ffdce0; color: var(--red); }
.badge-yellow { background: #fff8c5; color: var(--yellow); }
.badge-grey   { background: #f1f3f5; color: var(--grey); }
[data-tooltip] { position: relative; }
[data-tooltip]::before {
  content: '';
  position: absolute;
  bottom: calc(100% + 2px);
  left: 50%; transform: translateX(-50%);
  border: 5px solid transparent;
  border-top-color: #24292e;
  pointer-events: none;
  opacity: 0; transition: opacity 0.07s ease;
  z-index: 11;
}
[data-tooltip]::after {
  content: attr(data-tooltip);
  position: absolute;
  bottom: calc(100% + 12px);
  left: 50%; transform: translateX(-50%);
  background: #24292e; color: #fff;
  padding: 5px 10px; border-radius: 5px;
  font-size: 12px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-weight: normal; text-transform: none; letter-spacing: normal;
  white-space: pre; pointer-events: none;
  box-shadow: 0 3px 10px rgba(0,0,0,0.25);
  opacity: 0; transition: opacity 0.12s ease;
  z-index: 10;
}
[data-tooltip]:hover::before,
[data-tooltip]:hover::after { opacity: 1; }
.sha {
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px;
  color: #0366d6; text-decoration: none;
  background: #f1f3f5; padding: 1px 4px; border-radius: 3px;
}
.sha:hover { background: #e1e4e8; }
.sha.sha-tag { color: #6f42c1; background: #f0ebff; }
.sha.sha-tag:hover { background: #e4d9f7; }
.none { color: #bbb; }
.distance { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; }
.distance.stale { color: var(--yellow); }
.distance.zero { color: var(--grey); }
.distance-sub { font-family: "SFMono-Regular", Consolas, monospace; font-size: 11px; color: var(--grey); margin-top: 1px; }
.links { display: flex; gap: 4px; flex-wrap: wrap; }
.btn {
  display: inline-block; padding: 2px 8px;
  border: 1px solid var(--border); border-radius: 4px;
  font-size: 11px; color: #0366d6; text-decoration: none; white-space: nowrap;
}
.btn:hover { background: #f1f3f5; }
.tips {
  font-size: 12px; color: var(--grey);
  margin-bottom: 10px;
}
.tips strong { font-weight: 600; color: #57606a; }
.tips .sep { margin: 0 8px; color: var(--border); }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { background: #e8eaed; }
th.sort-asc::after  { content: " ▲"; font-size: 9px; color: var(--grey); }
th.sort-desc::after { content: " ▼"; font-size: 9px; color: var(--grey); }
.filter-bar { margin-bottom: 12px; }
.filter-bar input {
  width: 100%; padding: 7px 12px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px; outline: none;
  background: #fff; color: #24292e;
}
.filter-bar input:focus { border-color: #0366d6; box-shadow: 0 0 0 3px rgba(3,102,214,.15); }
footer {
  text-align: center; color: var(--grey); font-size: 12px;
  padding: 24px 16px 32px;
}
footer a { color: var(--grey); text-decoration: none; }
footer a:hover { text-decoration: underline; }
"""


def days_between(date_a: str | None, date_b: str | None) -> int | None:
    """Return (date_b - date_a).days, or None if either date is missing/unparseable."""
    if not date_a or not date_b:
        return None
    try:
        a = datetime.fromisoformat(date_a.replace("Z", "+00:00"))
        b = datetime.fromisoformat(date_b.replace("Z", "+00:00"))
        return (b - a).days
    except Exception:
        return None


def distance_cell(n: int | None, days: int | None = None) -> str:
    if n is None and days is None:
        return "<span class='none'>—</span>"
    if days is not None:
        if days == 0:
            primary = '<span class="distance zero">0d</span>'
        else:
            primary = f'<span class="distance stale">{days}d</span>'
        sub = f'<div class="distance-sub">(+{n})</div>' if n is not None else ""
        return primary + sub
    if n == 0:
        return '<span class="distance zero">0</span>'
    return f'<span class="distance stale">+{n}</span>'


def render_stats(rows: list[dict]) -> str:
    n_passed = sum(1 for r in rows if r.get("outcome") == "passed")
    n_failed = sum(1 for r in rows if r.get("outcome") == "failed")
    n_error  = sum(1 for r in rows if r.get("outcome") == "error")
    return (
        f'<div class="stats">'
        f'<div class="stat green"><div class="n">{n_passed}</div><div class="l">compatible</div></div>'
        f'<div class="stat red"><div class="n">{n_failed}</div><div class="l">incompatible</div></div>'
        f'<div class="stat yellow"><div class="n">{n_error}</div><div class="l">errors</div></div>'
        f'</div>'
    )


def render_run_banner(
    *,
    run_id: str,
    run_url: str,
    upstream_ref: str,
    reported_at: Any,
    generated_at: str,
    target_banner: str,
) -> str:
    return (
        f'<div class="run-banner">'
        f'<div class="run-banner-title">Mathlib Hopscotch Report</div>'
        f'<div class="run-banner-meta">'
        f'<span><strong>Upstream ref:</strong>&nbsp;<code>{esc(upstream_ref)}</code></span>'
        f'<span class="divider">|</span>'
        f'<span><strong>Latest run:</strong>&nbsp;<a href="{esc(run_url)}" target="_blank" rel="noopener noreferrer">{esc(run_id)}</a></span>'
        f'<span class="divider">|</span>'
        f'<span><strong>Reported:</strong>&nbsp;{fmt_dt(reported_at)}</span>'
        f'{target_banner}'
        f'<span style="margin-left:auto;color:var(--grey);font-size:12px;">Generated&nbsp;{esc(generated_at)}</span>'
        f'</div>'
        f'</div>'
    )


def render_table_row(
    r: dict,
    *,
    run_url: str,
    commit_titles: dict[str, dict[str, str | None]],
    downstream_commit_titles: dict[str, dict[str, str | None]],
    sha_to_tag: dict[str, str],
) -> str:
    downstream = r.get("downstream", "")
    repo = r.get("repo", "")
    repo_url = f"{GITHUB}/{repo}" if repo and "/" in repo else None

    name_cell = f'<div class="name">'
    if repo_url:
        name_cell += f'<a href="{esc(repo_url)}" title="{esc(repo)}" target="_blank" rel="noopener noreferrer">{esc(downstream)}</a>'
    else:
        name_cell += esc(downstream)
    name_cell += "</div>"
    if repo:
        name_cell += f'<div class="repo-label">{esc(repo)}</div>'

    def ct(sha: str | None) -> str | None:
        info = commit_titles.get(sha) if sha else None
        return info.get("title") if info else None

    def cd(sha: str | None) -> str | None:
        info = commit_titles.get(sha) if sha else None
        return info.get("date") if info else None

    def tg(sha: str | None) -> str | None:
        return sha_to_tag.get(sha) if sha else None

    pin = r.get("pinned_commit")
    target = r.get("target_commit")
    lkg = r.get("last_known_good")
    fkb = r.get("first_known_bad")
    age_val  = r.get("age_commits")
    bump_val = r.get("bump_commits")

    episode_state = r.get("episode_state")
    # Only show the episode badge for transitions; steady-state is conveyed by the compatibility column.
    _episode_is_transition = episode_state in ("new_failure", "recovered")
    episode_badge = badge(episode_state, EPISODE_CLASS, EPISODE_LABEL, EPISODE_TOOLTIP) if _episode_is_transition else None
    episode_title = f"status: {EPISODE_LABEL.get(episode_state, episode_state)}" if episode_state and _episode_is_transition else None
    ds_commit = r.get("downstream_commit")
    ds_info = downstream_commit_titles.get(ds_commit) if ds_commit else None
    ds_title = ds_info.get("title") if ds_info else None
    ds_date = ds_info.get("date") if ds_info else None
    ds_commit_link = commit_link(repo, ds_commit, ds_title, date=ds_date) if ds_commit and repo and "/" in repo else ""
    name_cell += f'<div class="episode-label">'
    if ds_commit_link:
        name_cell += f'<tt>@</tt> {ds_commit_link}&nbsp;'
    if episode_title and episode_badge:
        name_cell += f'<span title="{esc(episode_title)}">{episode_badge}</span>'
    name_cell += '</div>'

    compatibility_cell  = badge(r.get("outcome"), COMPATIBILITY_CLASS, label_map=COMPATIBILITY_LABEL, tooltip_map=COMPATIBILITY_TOOLTIP)
    target_cell   = commit_link(UPSTREAM_REPO, target, ct(target), tg(target), cd(target))
    lkg_cell      = commit_link(UPSTREAM_REPO, lkg,    ct(lkg),    tg(lkg),    cd(lkg))
    fkb_cell      = commit_link(UPSTREAM_REPO, fkb,    ct(fkb),    tg(fkb),    cd(fkb))
    pin_cell      = commit_link(UPSTREAM_REPO, pin,    ct(pin),    tg(pin),    cd(pin))
    age_days  = days_between(cd(pin), cd(target))
    age_cell  = distance_cell(age_val, age_days)
    bump_cell = distance_cell(bump_val)

    row_run_url = r.get("run_url") or run_url
    btns: list[str] = []
    if r.get("job_url"):
        btns.append(f'<a href="{esc(r["job_url"])}" class="btn" target="_blank" rel="noopener noreferrer">CI job&nbsp;↗</a>')
    if row_run_url:
        btns.append(f'<a href="{esc(row_run_url)}" class="btn" target="_blank" rel="noopener noreferrer">Run&nbsp;↗</a>')
    links_cell = f'<div class="links">{"".join(btns)}</div>'

    _av = str(age_days) if age_days is not None else (str(age_val) if age_val is not None else "-1")
    _bv = str(bump_val) if bump_val is not None else "-1"
    cells = (
        f'<td data-sort-val="{esc(downstream.lower())}">{name_cell}</td>'
        f'<td data-sort-val="{esc(pin or "")}">{pin_cell}</td>'
        f'<td data-sort-val="{_av}">{age_cell}</td>'
        f'<td data-sort-val="{esc(target or "")}">{target_cell}</td>'
        f'<td data-sort-val="{esc(r.get("compatibility", ""))}">{compatibility_cell}</td>'
        f'<td data-sort-val="{esc(lkg or "")}">{lkg_cell}</td>'
        f'<td data-sort-val="{esc(fkb or "")}">{fkb_cell}</td>'
        f'<td data-sort-val="{_bv}">{bump_cell}</td>'
        f"<td>{links_cell}</td>"
    )
    ep_label = EPISODE_LABEL.get(episode_state, episode_state or "")
    compatibility_search = {"passed": "compatible", "failed": "incompatible"}.get(r.get("compatibility", ""), r.get("compatibility", ""))
    filter_tokens = " ".join(filter(None, [
        downstream.lower(),
        repo.lower(),
        compatibility_search,
        ep_label,
        short_sha(pin),
        tg(pin),
        short_sha(target),
        tg(target),
        short_sha(lkg),
        tg(lkg),
        short_sha(fkb),
        tg(fkb),
    ]))
    return f'<tr class="data-row" data-filter="{esc(filter_tokens)}">{cells}</tr>'


def render(
    *,
    run_id: str,
    run_url: str,
    upstream_ref: str,
    reported_at: Any,
    generated_at: str,
    rows: list[dict],
    commit_titles: dict[str, dict[str, str | None]],
    downstream_commit_titles: dict[str, dict[str, str | None]],
    sha_to_tag: dict[str, str],
) -> str:
    col_glossary_items = "".join(
        f'<div class="col-glossary-item">'
        f'<span class="col-glossary-key">{label}</span>'
        f'<span class="col-glossary-val">{esc(desc)}</span>'
        f'</div>'
        for label, desc in [
            ("Downstream",      COL_DESC["downstream"]),
            ("Target",          COL_DESC["target"]),
            ("Compatibility",    COL_DESC["compatibility"]),
            ("Last known good", COL_DESC["last_known_good"]),
            ("First known bad", COL_DESC["first_known_bad"]),
            ("Pinned",          COL_DESC["pinned"]),
            ("Age",             COL_DESC["age"]),
            ("Bump",            COL_DESC["bump"]),
        ]
    )
    badge_glossary_intro = (
        '<div class="col-glossary-badge-intro">'
        "Each run attempts to build the downstream project after updating its Mathlib "
        "dependency to the target revision. "
        "A failure in this build means something conflicts between the dependency "
        "and the dependent at that revision (an API change, namespace conflict, ...)"
        "</div>"
    )
    badge_glossary_items = badge_glossary_intro + "".join(
        f'<div class="col-glossary-badge-item">'
        f'<span class="col-glossary-badge-key">'
        f'<span class="badge {cls}">{label}</span>'
        f'</span>'
        f'<span class="col-glossary-badge-val">{esc(desc)}</span>'
        f'</div>'
        for cls, label, desc in [
            ("badge-green",  "compatible",         COMPATIBILITY_TOOLTIP["passed"]),
            ("badge-red",    "incompatible",        COMPATIBILITY_TOOLTIP["failed"]),
            ("badge-yellow", "error",               COMPATIBILITY_TOOLTIP["error"])
        ]
    )
    readme_url = f"{GITHUB}/{THIS_REPO}#readme"
    mathlib_url = f"{GITHUB}/{UPSTREAM_REPO}"
    report_desc = (
        f'<div class="report-desc">'
        f'<p class="report-desc-intro">This dashboard answers these questions about projects that depend on '
        f'<a href="{mathlib_url}" target="_blank" rel="noopener noreferrer">mathlib4</a>:</p>'
        f'<ul class="report-desc-list">'
        f'<li><strong>How far behind is the dependency revision?</strong> '
        f'A scheduled workflow builds each registered downstream against the most recent mathlib commit (the <em>target</em>). '
        f'The <em>age</em> column shows how many commits your pinned revision lags behind it.</li>'
        f'<li><strong>Which commit introduced the incompatibility?</strong> '
        f'When the downstream is incompatible with the target, we run '
        f'<a href="https://github.com/leanprover-community/hopscotch" target="_blank" rel="noopener noreferrer">hopscotch</a> '
        f'to scan the mathlib history between the pinned revision and the target, '
        f'to identify the <em>first known bad</em> commit — the earliest Mathlib revision incompatible with the downstream — '
        f'and the <em>last known good</em> commit just before it.</li>'
        f'<li><strong>How much can I safely advance the dependency?</strong> '
        f'The <em>last known good</em> commit is a safe upgrade target. '
        f'The <em>bump</em> column shows the distance between it and the currently pinned revision.</li>'
        f'</ul>'
        f'<p class="report-desc-footer">'
        f'To register a project here or learn how the workflow operates, see the '
        f'<a href="{readme_url}" target="_blank" rel="noopener noreferrer">hopscotch-reports README</a>.'
        f'</p>'
        f'<details class="glossary-toggle">'
        f'<summary>Glossary</summary>'
        f'<div class="col-glossary">'
        f'<div class="col-glossary-col">'
        f'{col_glossary_items}'
        f'</div>'
        f'<div class="col-glossary-col">'
        f'{badge_glossary_items}'
        f'</div>'
        f'</div>'
        f'</details>'
        f'</div>'
    )

    stats_html = render_stats(rows)

    target_shas = {r.get("target_commit") for r in rows if r.get("target_commit")}
    common_target: str | None = next(iter(target_shas)) if len(target_shas) == 1 else None

    target_banner = ""
    if common_target:
        target_info = commit_titles.get(common_target) or {}
        target_title = target_info.get("title")
        target_date = target_info.get("date")
        target_tag = sha_to_tag.get(common_target)
        target_banner = (
            f'<span class="divider">|</span>'
            f'<span><strong>Target:</strong>&nbsp;{commit_link(UPSTREAM_REPO, common_target, target_title, target_tag, target_date)}</span>'
        )

    run_banner = render_run_banner(
        run_id=run_id,
        run_url=run_url,
        upstream_ref=upstream_ref,
        reported_at=reported_at,
        generated_at=generated_at,
        target_banner=target_banner,
    )

    def sort_key(r: dict) -> tuple:
        return (r.get("downstream", "").lower(),)

    table_rows = [
        render_table_row(
            r,
            run_url=run_url,
            commit_titles=commit_titles,
            downstream_commit_titles=downstream_commit_titles,
            sha_to_tag=sha_to_tag,
        )
        for r in sorted(rows, key=sort_key)
    ]

    def _th(label: str, key: str | None = None, sortable: bool = False, sort_type: str = "string") -> str:
        cls = ' class="sortable"' if sortable else ""
        stype = f' data-sort-type="{sort_type}"' if sortable else ""
        inner = f'<span data-tooltip="{esc(COL_DESC[key])}">{label}</span>' if key else label
        return f"<th{cls}{stype}>{inner}</th>"

    thead_row = (
        _th("Downstream",      "downstream",      sortable=True)
        + _th("Pinned to",          "pinned")
        + _th("Age",             "age",             sortable=True, sort_type="numeric")
        + _th("Target",          "target")
        + _th("Compatibility",         "compatibility",         sortable=True)
        + _th("Last known good", "last_known_good")
        + _th("First known bad", "first_known_bad")
        + _th("Bump",            "bump",            sortable=True, sort_type="numeric")
        + _th("Links")
    )

    n_cols = 9
    if not table_rows:
        table_rows = [
            f'<tr><td colspan="{n_cols}" style="text-align:center;padding:20px;color:#bbb;">'
            "No results for this run.</td></tr>"
        ]

    tbody = "\n".join(table_rows)
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mathlib Downstream Status</title>
  <style>
{CSS}  </style>
</head>
<body>
<main>
  {run_banner}
  {report_desc}
  {stats_html}
  <div class="tips">
    <strong>Tips:</strong>
    Hover any badge or commit SHA to see details. Click on column headers to sort. Use the filter box to quickly find your project or filter by compatibility.
  </div>
  <div class="filter-bar">
    <input id="filter" type="search" placeholder="Filter by repository, commit, compatibility…" aria-label="Filter by repository, commit, compatibility">
  </div>
  <div class="table-wrap">
  <table>
    <thead>
      <tr>{thead_row}</tr>
    </thead>
    <tbody>
{tbody}
    </tbody>
  </table>
  </div>
</main>
<footer>
  Generated {esc(generated_at)}&nbsp;&middot;&nbsp;<a href="{esc(run_url)}" target="_blank" rel="noopener noreferrer">Workflow run {esc(run_id)}</a>
</footer>
<script>
  const input = document.getElementById('filter');
  input.addEventListener('input', () => {{
    const q = input.value.toLowerCase();
    document.querySelectorAll('tr.data-row').forEach(row => {{
      const match = !q || row.dataset.filter.includes(q);
      row.hidden = !match;
    }});
  }});

  (() => {{
    const tbody = document.querySelector('tbody');
    let sortCol = -1, sortAsc = true;
    document.querySelectorAll('th.sortable').forEach(th => {{
      th.addEventListener('click', () => {{
        const idx = Array.from(th.parentElement.children).indexOf(th);
        sortAsc = sortCol === idx ? !sortAsc : true;
        sortCol = idx;
        document.querySelectorAll('th.sortable').forEach(t => t.classList.remove('sort-asc', 'sort-desc'));
        th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
        const isNumeric = th.dataset.sortType === 'numeric';
        Array.from(tbody.querySelectorAll('tr.data-row'))
          .sort((a, b) => {{
            const av = a.children[idx]?.dataset.sortVal ?? '';
            const bv = b.children[idx]?.dataset.sortVal ?? '';
            const cmp = isNumeric ? parseFloat(av) - parseFloat(bv) : av.localeCompare(bv);
            return sortAsc ? cmp : -cmp;
          }})
          .forEach(r => tbody.appendChild(r));
      }});
    }});
  }})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate a static HTML status page from the regression database.",
    )
    ap.add_argument("--backend", choices=["sql", "filesystem"], default="sql")
    ap.add_argument("--state-root", help="Filesystem backend: state root directory")
    ap.add_argument(
        "--run-id",
        help="SQL backend: workflow run ID to render (default: latest regression run)",
    )
    ap.add_argument("--output", required=True, help="Output HTML file path")
    ap.add_argument(
        "--inventory",
        default=str(Path(__file__).resolve().parent.parent / "ci" / "inventory" / "downstreams.json"),
        help="Path to downstreams.json inventory; disabled entries are excluded from the page",
    )
    ap.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token for commit title lookups (default: $GITHUB_TOKEN)",
    )
    args = ap.parse_args()

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if args.backend == "sql":
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            ap.error("POSTGRES_DSN environment variable is required for --backend sql")

        from sqlalchemy import create_engine
        from scripts.storage import latest_regression_run_id, load_run_for_site

        engine = create_engine(dsn)

        run_id = args.run_id
        if not run_id:
            run_id = latest_regression_run_id(engine)
            if not run_id:
                ap.error("No regression runs found in the database.")

        run_meta, rows = load_run_for_site(engine, run_id)

    else:  # filesystem
        if not args.state_root:
            ap.error("--state-root is required for --backend filesystem")
        run_meta, rows = load_from_filesystem(Path(args.state_root))

    run_id = run_meta.get("run_id", "unknown")
    run_url = run_meta.get("run_url", "") or ""
    upstream_ref = run_meta.get("upstream_ref", "unknown") or "unknown"
    reported_at = run_meta.get("reported_at") or None

    # Filter out downstreams that are disabled in the inventory.
    inventory_path = Path(args.inventory)
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text())
        enabled = {
            item["name"]
            for item in inventory.get("downstreams", [])
            if item.get("enabled", True)
        }
        rows = [r for r in rows if r.get("downstream") in enabled]
    else:
        print(f"  warning: inventory not found at {inventory_path}, skipping filter")

    # Collect every unique Mathlib SHA referenced across all rows, then fetch
    # their commit titles in one pass (memoized — each SHA fetched at most once).
    sha_fields = ("target_commit", "last_known_good", "first_known_bad", "pinned_commit")
    unique_shas = {r[f] for r in rows for f in sha_fields if r.get(f)}
    print(f"Fetching commit titles for {len(unique_shas)} unique SHA(s)…")
    commit_titles = fetch_commit_titles(unique_shas, UPSTREAM_REPO, args.github_token)

    # Fetch downstream commit titles, grouped by repo.
    ds_by_repo: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("downstream_commit") and r.get("repo") and "/" in r.get("repo", ""):
            ds_by_repo[r["repo"]].add(r["downstream_commit"])
    downstream_commit_titles: dict[str, str | None] = {}
    for repo, shas in sorted(ds_by_repo.items()):
        print(f"Fetching downstream commit titles for {repo} ({len(shas)} SHA(s))…")
        downstream_commit_titles.update(fetch_commit_titles(shas, repo, args.github_token))

    # Fetch tags for the upstream repo so tagged commits display the tag name.
    print(f"Fetching tags for {UPSTREAM_REPO}…")
    sha_to_tag = fetch_tags(UPSTREAM_REPO, args.github_token)
    print(f"  {len(sha_to_tag)} tag(s) loaded.")

    html = render(
        run_id=run_id,
        run_url=run_url,
        upstream_ref=upstream_ref,
        reported_at=reported_at,
        generated_at=generated_at,
        rows=rows,
        commit_titles=commit_titles,
        downstream_commit_titles=downstream_commit_titles,
        sha_to_tag=sha_to_tag,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Site written to {out}")


if __name__ == "__main__":
    main()
