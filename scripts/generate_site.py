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


def commit_link(repo: str, sha: str | None, title: str | None = None) -> str:
    if not sha:
        return "<span class='none'>—</span>"
    url = f"{GITHUB}/{repo}/commit/{sha}"
    s = short_sha(sha)
    tooltip = esc(title if title else sha)
    return f'<a href="{esc(url)}" class="sha" data-tooltip="{tooltip}" target="_blank" rel="noopener noreferrer">{esc(s)}</a>'


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
) -> dict[str, str | None]:
    """Return {sha: title} for every SHA in *shas*, fetched from the GitHub API.

    The title is the first line of the commit message.  On any error the SHA
    maps to None so callers can fall back gracefully.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hopscotch-reports/generate_site",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    cache: dict[str, str | None] = {}
    for sha in sorted(shas):  # deterministic order for predictable log output
        url = f"{GITHUB_API}/repos/{repo}/commits/{sha}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            message: str = data.get("commit", {}).get("message", "") or ""
            cache[sha] = message.splitlines()[0] if message else None
        except Exception as exc:
            print(f"  warning: could not fetch commit title for {sha[:7]}: {exc}")
            cache[sha] = None

    return cache



COL_DESC = {
    "downstream":      "Project tested updating the Mathlib dependency",
    "outcome":         "Result of the build updating the downstream to the target revision",
    "target":          "Mathlib revision this run targeted",
    "last_known_good": "Latest Mathlib revision where build passed",
    "first_known_bad": "Earliest Mathlib revision where build failed",
    "pinned":          "Mathlib revision in the lake manifest",
    "age":             "Commits between 'pinned' and 'target'",
    "bump":            "Commits that can be safely advanced ('pinned' -> 'last known good')",
}

OUTCOME_CLASS = {
    "passed": "badge-green",
    "failed": "badge-red",
    "error":  "badge-yellow",
}
OUTCOME_TOOLTIP = {
    "passed": "Build succeeded against the target Mathlib commit",
    "failed": "Build failed against the target Mathlib commit",
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
    "passing":     "passing",
    "recovered":   "recovered",
    "new_failure": "new failure",
    "failing":     "failing",
    "error":       "error",
}
EPISODE_TOOLTIP = {
    "passing":     "Has been passing consistently",
    "recovered":   "Was failing but has now recovered",
    "new_failure": "Newly broken — was passing in the previous run",
    "failing":     "Has been failing across multiple runs",
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
.glossary-toggle { margin-top: 8px; }
.glossary-toggle summary {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 12px; font-weight: 600; color: var(--grey);
  cursor: pointer; user-select: none; list-style: none;
}
.glossary-toggle summary::-webkit-details-marker { display: none; }
.glossary-toggle summary::before {
  content: "▶"; font-size: 9px; transition: transform .15s;
}
.glossary-toggle[open] summary::before { transform: rotate(90deg); }
.col-glossary { display: flex; flex-direction: column; gap: 3px; margin-top: 8px; }
.col-glossary-item { display: flex; gap: 6px; align-items: baseline; font-size: 12px; }
.col-glossary-key {
  font-weight: 600; text-transform: uppercase; font-size: 11px;
  letter-spacing: .04em; color: var(--grey); white-space: nowrap; min-width: 120px;
}
.col-glossary-val { color: #57606a; }
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
[data-tooltip]::after {
  content: attr(data-tooltip);
  position: absolute;
  bottom: calc(100% + 6px);
  left: 50%; transform: translateX(-50%);
  background: #24292e; color: #fff;
  padding: 4px 8px; border-radius: 4px;
  font-size: 11px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-weight: normal; text-transform: none; letter-spacing: normal;
  white-space: nowrap; pointer-events: none;
  opacity: 0; transition: opacity 0s;
  z-index: 10;
}
[data-tooltip]:hover::after { opacity: 1; }
.sha {
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px;
  color: #0366d6; text-decoration: none;
  background: #f1f3f5; padding: 1px 4px; border-radius: 3px;
}
.sha:hover { background: #e1e4e8; }
.none { color: #bbb; }
.distance { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; }
.distance.stale { color: var(--yellow); }
.distance.zero { color: var(--grey); }
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


def distance_cell(n: int | None) -> str:
    if n is None:
        return "<span class='none'>—</span>"
    if n == 0:
        return '<span class="distance zero">0</span>'
    return f'<span class="distance stale">+{n}</span>'


def render_stats(rows: list[dict]) -> str:
    n_passed = sum(1 for r in rows if r.get("outcome") == "passed")
    n_failed = sum(1 for r in rows if r.get("outcome") == "failed")
    n_error  = sum(1 for r in rows if r.get("outcome") == "error")
    return (
        f'<div class="stats">'
        f'<div class="stat green"><div class="n">{n_passed}</div><div class="l">passing</div></div>'
        f'<div class="stat red"><div class="n">{n_failed}</div><div class="l">failing</div></div>'
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
        f'<span><strong>Run:</strong>&nbsp;<a href="{esc(run_url)}" target="_blank" rel="noopener noreferrer">{esc(run_id)}</a></span>'
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
    show_target_column: bool,
    commit_titles: dict[str, str | None],
    downstream_commit_titles: dict[str, str | None],
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
        return commit_titles.get(sha) if sha else None

    pin = r.get("pinned_commit")
    target = r.get("target_commit")
    lkg = r.get("last_known_good")
    fkb = r.get("first_known_bad")

    episode_state = r.get("episode_state")
    episode_badge = badge(episode_state, EPISODE_CLASS, EPISODE_LABEL, EPISODE_TOOLTIP)
    episode_title = f"status: {EPISODE_LABEL.get(episode_state, episode_state)}" if episode_state else None
    ds_commit = r.get("downstream_commit")
    ds_title = downstream_commit_titles.get(ds_commit) if ds_commit else None
    ds_commit_link = commit_link(repo, ds_commit, ds_title) if ds_commit and repo and "/" in repo else ""
    name_cell += f'<div class="episode-label">'
    if ds_commit_link:
        name_cell += f'{ds_commit_link}&nbsp;'
    if episode_title:
        name_cell += f'<span title="{esc(episode_title)}">{episode_badge}</span>'
    name_cell += '</div>'

    outcome_cell  = badge(r.get("outcome"), OUTCOME_CLASS, tooltip_map=OUTCOME_TOOLTIP)
    target_cell   = commit_link(UPSTREAM_REPO, target, ct(target)) if show_target_column else None
    lkg_cell      = commit_link(UPSTREAM_REPO, lkg,    ct(lkg))
    fkb_cell      = commit_link(UPSTREAM_REPO, fkb,    ct(fkb))
    pin_cell      = commit_link(UPSTREAM_REPO, pin,    ct(pin))
    age_cell      = distance_cell(r.get("age_commits"))
    bump_cell     = distance_cell(r.get("bump_commits"))

    btns: list[str] = []
    if r.get("job_url"):
        btns.append(f'<a href="{esc(r["job_url"])}" class="btn" target="_blank" rel="noopener noreferrer">CI job&nbsp;↗</a>')
    if run_url:
        btns.append(f'<a href="{esc(run_url)}" class="btn" target="_blank" rel="noopener noreferrer">Run&nbsp;↗</a>')
    links_cell = f'<div class="links">{"".join(btns)}</div>'

    cells = (
        f"<td>{name_cell}</td>"
        f"<td>{outcome_cell}</td>"
        + (f"<td>{target_cell}</td>" if show_target_column else "")
        + f"<td>{lkg_cell}</td>"
        f"<td>{fkb_cell}</td>"
        f"<td>{pin_cell}</td>"
        f"<td>{age_cell}</td>"
        f"<td>{bump_cell}</td>"
        f"<td>{links_cell}</td>"
    )
    ep_label = EPISODE_LABEL.get(episode_state, episode_state or "")
    filter_tokens = " ".join(filter(None, [
        downstream.lower(),
        repo.lower(),
        r.get("outcome", ""),
        ep_label,
        short_sha(pin),
        short_sha(target),
        short_sha(lkg),
        short_sha(fkb),
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
    commit_titles: dict[str, str | None],
    downstream_commit_titles: dict[str, str | None],
) -> str:
    col_glossary_items = "".join(
        f'<div class="col-glossary-item">'
        f'<span class="col-glossary-key">{label}</span>'
        f'<span class="col-glossary-val">{esc(desc)}</span>'
        f'</div>'
        for label, desc in [
            ("Downstream",      COL_DESC["downstream"]),
            ("Outcome",         COL_DESC["outcome"]),
            ("Target",          COL_DESC["target"]),
            ("Last known good", COL_DESC["last_known_good"]),
            ("First known bad", COL_DESC["first_known_bad"]),
            ("Pinned",          COL_DESC["pinned"]),
            ("Age",             COL_DESC["age"]),
            ("Bump",            COL_DESC["bump"]),
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
        f'<li><strong>Which commit broke my build?</strong> '
        f'When a build against the target fails, we run '
        f'<a href="https://github.com/leanprover-community/hopscotch" target="_blank" rel="noopener noreferrer">hopscotch</a> '
        f'to scan the mathlib history between the pinned revision and the target, '
        f'to identify the <em>first known bad</em> commit — the earliest one that breaks your build — '
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
        f'<div class="col-glossary">{col_glossary_items}</div>'
        f'</details>'
        f'</div>'
    )

    stats_html = render_stats(rows)

    # If every downstream resolved the same Mathlib commit, show it once in the
    # banner and omit the per-row column; otherwise keep the column.
    target_shas = {r.get("target_commit") for r in rows if r.get("target_commit")}
    common_target: str | None = next(iter(target_shas)) if len(target_shas) == 1 else None
    show_target_column = len(target_shas) > 1

    target_banner = ""
    if common_target:
        target_title = commit_titles.get(common_target)
        target_banner = (
            f'<span class="divider">|</span>'
            f'<span><strong>Target:</strong>&nbsp;{commit_link(UPSTREAM_REPO, common_target, target_title)}</span>'
        )

    run_banner = render_run_banner(
        run_id=run_id,
        run_url=run_url,
        upstream_ref=upstream_ref,
        reported_at=reported_at,
        generated_at=generated_at,
        target_banner=target_banner,
    )

    # Sort: failing first, then errors, then passing; alpha within each group
    def sort_key(r: dict) -> tuple:
        order = {"failed": 0, "error": 1, "passed": 2}.get(r.get("outcome", ""), 3)
        return (order, r.get("downstream", "").lower())

    table_rows = [
        render_table_row(
            r,
            run_url=run_url,
            show_target_column=show_target_column,
            commit_titles=commit_titles,
            downstream_commit_titles=downstream_commit_titles,
        )
        for r in sorted(rows, key=sort_key)
    ]

    def _th(label: str, key: str | None = None) -> str:
        if key:
            return f'<th><span data-tooltip="{esc(COL_DESC[key])}">{label}</span></th>'
        return f"<th>{label}</th>"

    thead_row = (
        _th("Downstream",      "downstream")
        + _th("Outcome",         "outcome")
        + (_th("Target",         "target") if show_target_column else "")
        + _th("Last known good", "last_known_good")
        + _th("First known bad", "first_known_bad")
        + _th("Pinned",          "pinned")
        + _th("Age",             "age")
        + _th("Bump",            "bump")
        + _th("Links")
    )

    n_cols = 9 if show_target_column else 8
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
    <strong>Tip:</strong>
    hover any badge or commit SHA to see details
  </div>
  <div class="filter-bar">
    <input id="filter" type="search" placeholder="Filter by repository…" aria-label="Filter by repository">
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

    html = render(
        run_id=run_id,
        run_url=run_url,
        upstream_ref=upstream_ref,
        reported_at=reported_at,
        generated_at=generated_at,
        rows=rows,
        commit_titles=commit_titles,
        downstream_commit_titles=downstream_commit_titles,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Site written to {out}")


if __name__ == "__main__":
    main()
