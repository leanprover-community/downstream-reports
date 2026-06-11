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
import math
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UPSTREAM_REPO = "leanprover-community/mathlib4"
THIS_REPO = "leanprover-community/downstream-reports"
HOPSCOTCH_REPO = "leanprover-community/hopscotch"
GITHUB = "https://github.com"
GITHUB_API = "https://api.github.com"
# Public snapshot store (see publish-lkg.yml / fetch-latest.sh).
SNAPSHOT_BASE = "https://downstreamreports.z13.web.core.windows.net"
# Number of recent runs rendered in each row's history strip.
HISTORY_LIMIT = 15
# Client-side warning threshold for an out-of-date report.
STALE_AFTER_HOURS = 36


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


def _as_datetime(value: Any) -> datetime | None:
    """Coerce an ISO 8601 string or datetime (SQL backend) to an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def fmt_dt(iso: Any) -> str:
    if not iso:
        return "—"
    dt = _as_datetime(iso)
    if dt is None:
        return esc(iso)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def iso_epoch(iso: Any) -> int | None:
    """Return the Unix timestamp for an ISO 8601 string or datetime, or None."""
    dt = _as_datetime(iso)
    return int(dt.timestamp()) if dt else None


def fmt_duration(start: Any, finish: Any) -> str | None:
    """Return a human-readable duration between two ISO timestamps, or None."""
    a, b = iso_epoch(start), iso_epoch(finish)
    if a is None or b is None or b < a:
        return None
    secs = b - a
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


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
        "User-Agent": "downstream-reports/generate_site",
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
        "User-Agent": "downstream-reports/generate_site",
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


def fetch_commit_distances(
    pairs: set[tuple[str, str]],
    repo: str,
    token: str | None,
) -> dict[tuple[str, str], int | None]:
    """Return {(base_sha, head_sha): signed_distance} for every pair.

    Positive: head is ahead of base by that many commits (can advance).
    Negative: base is ahead of head by that many commits (already past).
    Zero: same commit.  None: API error.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "downstream-reports/generate_site",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result: dict[tuple[str, str], int | None] = {}
    for base, head in sorted(pairs):
        if base == head:
            result[(base, head)] = 0
            continue
        url = f"{GITHUB_API}/repos/{repo}/compare/{base}...{head}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            ahead = data.get("ahead_by") or 0
            behind = data.get("behind_by") or 0
            result[(base, head)] = ahead if ahead > 0 else -behind
        except Exception as exc:
            print(f"  warning: could not fetch distance {base[:7]}…{head[:7]}: {exc}")
            result[(base, head)] = None
    return result


# ---------------------------------------------------------------------------
# Local git helpers (used when --upstream-dir is provided)
# ---------------------------------------------------------------------------

def git_commit_info(
    repo_dir: Path,
    shas: set[str],
) -> dict[str, dict[str, str | None]]:
    """Return {sha: {"title": ..., "date": ...}} by reading a local clone.

    Uses a single ``git log --no-walk`` call for all SHAs at once.
    Any SHA not found in the repo maps to ``{"title": None, "date": None}``.
    """
    if not shas:
        return {}
    # %H = full SHA, %s = subject (first line only — no newlines), %cI = ISO committer date.
    # tformat emits exactly 3 lines per commit with no blank separators.
    args = ["git", "log", "--no-walk=unsorted", "--format=%H%n%s%n%cI"] + list(shas)
    try:
        out = subprocess.check_output(
            args, cwd=repo_dir, text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        print(f"  warning: git log failed: {exc}")
        return {sha: {"title": None, "date": None} for sha in shas}

    result: dict[str, dict[str, str | None]] = {}
    lines = out.splitlines()
    for i in range(0, len(lines) - 2, 3):
        sha = lines[i].strip()
        title = lines[i + 1].strip() or None
        date = lines[i + 2].strip() or None
        if sha:
            result[sha] = {"title": title, "date": date}
    for sha in shas:
        result.setdefault(sha, {"title": None, "date": None})
    return result


def git_tag_map(repo_dir: Path) -> dict[str, str]:
    """Return {commit_sha: tag_name} from a local clone.

    Uses ``git for-each-ref`` sorted by descending semver so the newest tag
    wins when multiple tags point to the same commit — matching the behaviour
    of ``fetch_tags`` which iterates pages newest-first.
    """
    # Each output line: "tag_name full_sha deref_sha_or_empty"
    # Tag names and SHAs contain no spaces, so space-splitting is safe.
    # Annotated tags: *objectname is the commit SHA; lightweight tags: objectname is.
    try:
        out = subprocess.check_output(
            [
                "git", "for-each-ref", "refs/tags",
                "--sort=-v:refname",
                "--format=%(refname:short) %(objectname) %(*objectname)",
            ],
            cwd=repo_dir, text=True, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        print(f"  warning: git for-each-ref failed: {exc}")
        return {}

    result: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        obj_sha = parts[1]
        deref_sha = parts[2] if len(parts) >= 3 else ""
        commit_sha = deref_sha or obj_sha
        if commit_sha and name and commit_sha not in result:
            result[commit_sha] = name
    return result


def git_signed_distance(repo_dir: Path, base: str, head: str) -> int | None:
    """Return the signed commit distance between *base* and *head* locally.

    Positive: *head* is ahead of *base*.  Negative: *base* is ahead of *head*.
    Returns ``None`` on any git error.
    """
    if base == head:
        return 0
    try:
        ahead = int(subprocess.check_output(
            ["git", "rev-list", "--count", f"{base}..{head}"],
            cwd=repo_dir, text=True, stderr=subprocess.DEVNULL,
        ).strip())
        if ahead > 0:
            return ahead
        behind = int(subprocess.check_output(
            ["git", "rev-list", "--count", f"{head}..{base}"],
            cwd=repo_dir, text=True, stderr=subprocess.DEVNULL,
        ).strip())
        return -behind
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"  warning: git distance {base[:7]}…{head[:7]} failed: {exc}")
        return None


COL_DESC = {
    "downstream":        "Project tested updating the Mathlib dependency\n(click the row for run details)",
    "compatibility":     "Compatibility of the downstream with the target Mathlib revision\n(based on the result of the latest validation run)",
    "target":            "Mathlib revision targeted in the latest validation run",
    "last_known_good":   "Latest Mathlib revision compatible with the downstream",
    "last_good_release": "Latest Mathlib semver release tag compatible with the downstream",
    "first_known_bad":   "Earliest Mathlib revision incompatible with the downstream\n(always the commit immediately after 'last known good')",
    "pinned":            "Mathlib revision in the downstream's lake manifest",
    "age":               "Days between 'pinned' and 'target' (commit count below)",
    "bump":              "Commits that can be safely advanced ('pinned' -> 'last known good')",
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
    "error":  "Validation job encountered an unexpected error",
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
    "error":       "Validation job has been erroring across multiple runs",
}

# Plain-English explanations of how the row's LKG/FKB endpoints were obtained.
SEARCH_MODE_DESC = {
    "bisect":               "Full bisect — the Mathlib commit window was searched in this run to pinpoint the exact breaking commit",
    "head-only":            "Direct build of the target revision only (no commit-window search was needed or possible)",
    "head-only-known-bad":  "Direct build of the target revision; it matches a previously identified incompatibility, so the known good/bad boundary from the earlier bisect is shown",
    "skipped-already-good": "Skipped — this exact project revision was already validated as compatible with this Mathlib revision in a previous run",
    "setup-error":          "The job failed while preparing the build, before any validation could start",
}

# Optional context for hopscotch / harness failure-stage identifiers.
FAILURE_STAGE_DESC = {
    "setup":  "preparing the working copy, before any build started",
    "runner": "the CI runner itself failed or timed out",
    "update": "updating the Mathlib dependency (lake update)",
    "build":  "compiling the project (lake build)",
}


def badge(value: str | None, cls_map: dict, label_map: dict | None = None, tooltip_map: dict | None = None) -> str:
    if not value:
        return "<span class='none'>—</span>"
    cls = cls_map.get(value, "badge-grey")
    label = (label_map or {}).get(value, value)
    tooltip = (tooltip_map or {}).get(value)
    # tabindex makes the tooltip keyboard-reachable (shown on :focus-visible).
    tooltip_attr = f' data-tooltip="{esc(tooltip)}" tabindex="0"' if tooltip else ""
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

    # Load release-tag fields from status/current.json (downstream_status table analogue).
    release_by_name: dict[str, dict] = {}
    status_path = state_root / "status" / "current.json"
    if status_path.exists():
        try:
            sdata = json.loads(status_path.read_text())
            for name, s in sdata.get("downstreams", {}).items():
                release_by_name[name] = {
                    "last_good_release": s.get("last_good_release"),
                    "last_good_release_commit": s.get("last_good_release_commit"),
                }
        except Exception:
            pass

    rows = []
    for r in data.get("results", []):
        row = dict(r)
        ds = row.get("downstream", "")
        row.setdefault("last_good_release", release_by_name.get(ds, {}).get("last_good_release"))
        row.setdefault("last_good_release_commit", release_by_name.get(ds, {}).get("last_good_release_commit"))
        # All filesystem rows come from the single latest run.
        row.setdefault("row_reported_at", data.get("reported_at"))
        rows.append(row)

    return run_meta, rows


def load_history_from_filesystem(state_root: Path, limit: int = HISTORY_LIMIT) -> dict[str, list[dict]]:
    """Return newest-first regression outcomes per downstream from the
    filesystem backend's history tree.

    Layout: ``results/{day}/{run_id}/{downstream}.json``.  On-demand history
    lives under ``results/ondemand/`` and is excluded.  History files carry no
    timestamp, so entries are dated by their day directory and within a day
    ordered by run-id only.
    """
    base = state_root / "results"
    if not base.exists():
        return {}
    day_dirs = sorted(
        (d for d in base.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)),
        key=lambda d: d.name,
        reverse=True,
    )
    result: dict[str, list[dict]] = {}
    for day in day_dirs:
        for run_dir in sorted((p for p in day.iterdir() if p.is_dir()), reverse=True):
            for f in sorted(run_dir.glob("*.json")):
                try:
                    row = json.loads(f.read_text())
                except Exception:
                    continue
                ds = row.get("downstream")
                outcome = row.get("outcome")
                if not ds or not outcome:
                    continue
                entries = result.setdefault(ds, [])
                if len(entries) < limit:
                    entries.append({
                        "outcome": outcome,
                        "first_known_bad": row.get("first_known_bad"),
                        "reported_at": day.name,
                        "run_url": None,
                    })
    return result


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_LIGHT_VARS = """\
  color-scheme: light;
  --green:  #22863a;
  --red:    #b31d28;
  --orange: #d4670e;
  --yellow: #b08800;
  --grey:   #6a737d;
  --bg:     #f6f8fa;
  --surface: #ffffff;
  --surface-alt: #f1f3f5;
  --surface-hover: #e8eaed;
  --border: #e1e4e8;
  --fg:     #24292e;
  --fg-muted: #57606a;
  --link:   #0366d6;
  --badge-green-bg:  #dcffe4;
  --badge-red-bg:    #ffdce0;
  --badge-yellow-bg: #fff8c5;
  --tag-fg: #6f42c1;
  --tag-bg: #f0ebff;
  --tag-bg-hover: #e4d9f7;
  --tooltip-bg: #24292e;
  --tooltip-fg: #ffffff;
  --focus-ring: rgba(3,102,214,.15);
  --none: #bbbbbb;
"""

_DARK_VARS = """\
  color-scheme: dark;
  --green:  #3fb950;
  --red:    #f85149;
  --orange: #f0883e;
  --yellow: #d29922;
  --grey:   #8b949e;
  --bg:     #0d1117;
  --surface: #161b22;
  --surface-alt: #21262d;
  --surface-hover: #2d333b;
  --border: #30363d;
  --fg:     #e6edf3;
  --fg-muted: #8b949e;
  --link:   #58a6ff;
  --badge-green-bg:  rgba(63,185,80,.16);
  --badge-red-bg:    rgba(248,81,73,.16);
  --badge-yellow-bg: rgba(210,153,34,.16);
  --tag-fg: #bc8cff;
  --tag-bg: rgba(188,140,255,.14);
  --tag-bg-hover: rgba(188,140,255,.26);
  --tooltip-bg: #2d333b;
  --tooltip-fg: #e6edf3;
  --focus-ring: rgba(88,166,255,.25);
  --none: #484f58;
"""

# Theme resolution: light by default; the dark palette applies when the OS
# prefers dark (unless the visitor forced light via the toggle) or when the
# visitor forced dark.  The toggle writes data-theme on <html>.
_CSS_THEME = (
    ":root {\n" + _LIGHT_VARS + "}\n"
    '@media (prefers-color-scheme: dark) {\n'
    '  :root:not([data-theme="light"]) {\n' + _DARK_VARS + "  }\n"
    "}\n"
    ':root[data-theme="dark"] {\n' + _DARK_VARS + "}\n"
)

CSS = _CSS_THEME + """\
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 14px; background: var(--bg); color: var(--fg); margin: 0; padding: 0;
}
main { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
a { color: var(--link); }
code { color: var(--fg); }
.run-banner {
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 16px; margin-bottom: 16px; font-size: 13px;
}
.run-banner-title {
  font-size: 17px; font-weight: 600; color: var(--fg); margin-bottom: 8px;
}
.run-banner-meta {
  display: flex; gap: 20px; flex-wrap: wrap; align-items: center;
}
.run-banner a { color: var(--link); text-decoration: none; }
.run-banner a:hover { text-decoration: underline; }
.run-banner .divider { color: var(--border); }
.theme-toggle {
  margin-left: 12px; padding: 3px 10px;
  border: 1px solid var(--border); border-radius: 4px;
  background: var(--surface-alt); color: var(--fg-muted);
  font: inherit; font-size: 12px; cursor: pointer; white-space: nowrap;
}
.theme-toggle:hover { background: var(--surface-hover); color: var(--fg); }
.rel-note { color: var(--fg-muted); }
.stats { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
.stat {
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 20px; text-align: center; min-width: 92px;
  font: inherit; color: var(--fg); cursor: pointer;
}
.stat:hover { background: var(--surface-alt); }
.stat.active { border-color: var(--link); box-shadow: 0 0 0 2px var(--focus-ring); }
.stat .n { font-size: 26px; font-weight: 600; }
.stat .l { font-size: 11px; color: var(--grey); text-transform: uppercase; letter-spacing: .04em; margin-top: 2px; }
.stat.green  .n { color: var(--green); }
.stat.red    .n { color: var(--red); }
.stat.yellow .n { color: var(--yellow); }
.table-wrap {
  border: 1px solid var(--border); border-radius: 6px;
  overflow-x: auto;
}
table {
  width: 100%; border-collapse: collapse;
  background: var(--surface);
}
th {
  background: var(--surface-alt); border-bottom: 1px solid var(--border);
  padding: 8px 12px; text-align: left; font-size: 11px;
  font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: var(--grey);
}
.report-desc {
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  padding: 12px 16px; margin-bottom: 16px; font-size: 13px;
}
.report-desc-intro { margin: 0 0 8px; color: var(--fg-muted); }
.report-desc-list { margin: 0 0 8px; padding-left: 20px; color: var(--fg-muted); display: flex; flex-direction: column; gap: 4px; }
.report-desc-footer { margin: 0; color: var(--fg-muted); }
.glossary-toggle { margin-top: 10px; }
.glossary-toggle summary {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12px; font-weight: 600; color: var(--fg-muted);
  background: var(--surface-alt); border: 1px solid var(--border); border-radius: 4px;
  padding: 3px 10px;
  cursor: pointer; user-select: none; list-style: none;
}
.glossary-toggle summary:hover { background: var(--surface-hover); color: var(--fg); }
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
.col-glossary-val { color: var(--fg-muted); }
.col-glossary-badge-intro { font-size: 12px; color: var(--fg-muted); margin: 0 0 6px; font-weight: 500; }
.col-glossary-badge-item { display: flex; gap: 8px; align-items: center; font-size: 12px; }
.col-glossary-badge-key { white-space: nowrap; min-width: 120px; }
.col-glossary-badge-val { color: var(--fg-muted); }
td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tbody tr.data-row { cursor: pointer; }
tbody tr.data-row:hover td { background: var(--bg); }
.name-wrap { display: flex; gap: 6px; align-items: flex-start; }
.expander {
  flex: none; background: none; border: none; padding: 1px 2px 0 0; margin: 0;
  font-size: 11px; color: var(--grey); cursor: pointer;
  transition: transform .12s; line-height: 1.4;
}
tr.expanded .expander { transform: rotate(90deg); }
.name { font-weight: 600; font-size: 13px; }
.name a { color: var(--fg); text-decoration: none; }
.name a:hover { color: var(--link); }
.repo-label { font-size: 11px; color: var(--grey); }
.episode-label { margin-top: 4px; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: 11px; font-weight: 600; white-space: nowrap;
}
.badge-green  { background: var(--badge-green-bg); color: var(--green); }
.badge-red    { background: var(--badge-red-bg); color: var(--red); }
.badge-yellow { background: var(--badge-yellow-bg); color: var(--yellow); }
.badge-grey   { background: var(--surface-alt); color: var(--grey); }
.checked-sub { font-size: 11px; color: var(--grey); margin-top: 3px; white-space: nowrap; }
.history-strip { display: flex; gap: 2px; margin-top: 5px; }
.hist-cell { width: 8px; height: 8px; border-radius: 2px; display: block; }
.hist-cell:hover, .hist-cell:focus-visible { outline: 1px solid var(--fg-muted); }
.hist-passed { background: var(--green); opacity: .75; }
.hist-failed { background: var(--red); opacity: .75; }
.hist-failed-other { background: var(--orange); opacity: .75; }
.hist-error  { background: var(--yellow); opacity: .75; }
.copy-sha {
  background: none; border: none; padding: 0 2px; margin-left: 4px;
  font-size: 12px; color: var(--grey); cursor: pointer; vertical-align: middle;
  line-height: 1;
}
.copy-sha:hover { color: var(--link); }
.copy-sha.copied { color: var(--green); }
.stale-warning {
  background: var(--badge-yellow-bg); border: 1px solid var(--yellow);
  color: var(--yellow); border-radius: 6px; padding: 10px 16px;
  margin-bottom: 16px; font-size: 13px; font-weight: 500;
}
[data-tooltip] { position: relative; }
[data-tooltip]::before {
  content: '';
  position: absolute;
  bottom: calc(100% + 2px);
  left: 50%; transform: translateX(-50%);
  border: 5px solid transparent;
  border-top-color: var(--tooltip-bg);
  pointer-events: none;
  opacity: 0; transition: opacity 0.07s ease;
  z-index: 11;
}
[data-tooltip]::after {
  content: attr(data-tooltip);
  position: absolute;
  bottom: calc(100% + 12px);
  left: 50%; transform: translateX(-50%);
  background: var(--tooltip-bg); color: var(--tooltip-fg);
  padding: 5px 10px; border-radius: 5px;
  font-size: 12px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  font-weight: normal; text-transform: none; letter-spacing: normal;
  white-space: pre; pointer-events: none;
  box-shadow: 0 3px 10px rgba(0,0,0,0.25);
  opacity: 0; transition: opacity 0.12s ease;
  z-index: 10;
}
[data-tooltip]:hover::before,
[data-tooltip]:focus-visible::before,
[data-tooltip]:hover::after,
[data-tooltip]:focus-visible::after { opacity: 1; }
.sha {
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px;
  color: var(--link); text-decoration: none;
  background: var(--surface-alt); padding: 1px 4px; border-radius: 3px;
}
.sha:hover { background: var(--surface-hover); }
.sha.sha-tag { color: var(--tag-fg); background: var(--tag-bg); }
.sha.sha-tag:hover { background: var(--tag-bg-hover); }
.none { color: var(--none); }
.distance { font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px; }
.distance.stale { color: var(--yellow); }
.distance.zero { color: var(--grey); }
.distance-sub { font-family: "SFMono-Regular", Consolas, monospace; font-size: 11px; color: var(--grey); margin-top: 1px; }
.links { display: flex; gap: 4px; flex-wrap: wrap; }
.btn {
  display: inline-block; padding: 2px 8px;
  border: 1px solid var(--border); border-radius: 4px;
  font-size: 11px; color: var(--link); text-decoration: none; white-space: nowrap;
  background: var(--surface);
}
.btn:hover { background: var(--surface-alt); }
.tips {
  font-size: 12px; color: var(--grey);
  margin-bottom: 10px;
}
.tips strong { font-weight: 600; color: var(--fg-muted); }
.tips .sep { margin: 0 8px; color: var(--border); }
th.sortable { cursor: pointer; user-select: none; }
th.sortable:hover { background: var(--surface-hover); }
th.sort-asc::after  { content: " ▲"; font-size: 9px; color: var(--grey); }
th.sort-desc::after { content: " ▼"; font-size: 9px; color: var(--grey); }
.filter-bar { margin-bottom: 4px; }
.filter-bar input {
  width: 100%; padding: 7px 12px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px; outline: none;
  background: var(--surface); color: var(--fg);
}
.filter-bar input:focus { border-color: var(--link); box-shadow: 0 0 0 3px var(--focus-ring); }
.row-count { font-size: 12px; color: var(--grey); margin: 0 2px 8px; min-height: 16px; }

/* --- expandable per-row detail panel --- */
tr.detail-row > td {
  background: var(--bg);
  padding: 14px 18px;
  cursor: default;
  border-bottom: 1px solid var(--border);
}
.detail { display: flex; flex-direction: column; gap: 12px; font-size: 13px; }
.detail-summary { margin: 0; color: var(--fg); line-height: 1.55; max-width: 80ch; }
.detail-summary .sha { white-space: nowrap; }
.detail-note { color: var(--fg-muted); }
.detail-warn {
  display: flex; gap: 8px; align-items: baseline;
  font-size: 12px; color: var(--yellow);
}
.detail-facts {
  display: grid; grid-template-columns: max-content 1fr;
  gap: 4px 16px; font-size: 12px; align-items: baseline;
}
.df-k {
  font-weight: 600; text-transform: uppercase; font-size: 11px;
  letter-spacing: .04em; color: var(--grey); white-space: nowrap;
}
.df-v { color: var(--fg-muted); }
.detail-error {
  margin: 0; padding: 10px 12px;
  background: var(--surface); border: 1px solid var(--border);
  border-left: 3px solid var(--yellow); border-radius: 4px;
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px;
  color: var(--fg-muted); white-space: pre-wrap; word-break: break-word;
  max-height: 180px; overflow-y: auto;
}

/* --- commit-window strip --- */
.window-strip-wrap { max-width: 720px; }
.window-strip-caption { font-size: 11px; color: var(--grey); text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px; }
.window-strip { display: flex; align-items: flex-start; }
.ws-node { display: flex; flex-direction: column; align-items: center; gap: 3px; flex: none; }
.ws-dot { width: 10px; height: 10px; border-radius: 50%; margin-top: 1px; }
.ws-dot.ws-good { background: var(--green); }
.ws-dot.ws-bad  { background: var(--red); }
.ws-dot.ws-neutral { background: var(--grey); }
.ws-label { font-size: 11px; color: var(--fg-muted); white-space: nowrap; }
.ws-seg { flex: 1 1 0; min-width: 28px; height: 2px; margin-top: 5px; }
.ws-seg.ws-good  { background: var(--green); }
.ws-seg.ws-bad   { background: var(--red); }
.ws-seg.ws-break { background: linear-gradient(to right, var(--green), var(--red)); }
.ws-seg.ws-unknown {
  background: repeating-linear-gradient(to right, var(--grey) 0 6px, transparent 6px 12px);
}
.ws-seg-note { font-size: 10px; color: var(--grey); text-align: center; margin-top: 4px; }
.ws-seg-wrap { flex: 1 1 0; min-width: 28px; display: flex; flex-direction: column; }
.ws-seg-wrap .ws-seg { width: 100%; flex: none; }
.ws-seg-wrap.ws-adjacent { flex: 0 0 26px; min-width: 26px; padding-bottom: 10px; }

/* --- advance map --- */
[hidden] { display: none !important; }
.chart-section { margin-top: 20px; }
.chart-section summary {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 13px; font-weight: 600; color: var(--fg-muted);
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 14px; cursor: pointer; user-select: none; list-style: none;
}
.chart-section summary:hover { background: var(--surface-alt); color: var(--fg); }
.chart-section summary::-webkit-details-marker { display: none; }
.chart-section summary::before { content: "▶"; font-size: 9px; transition: transform .15s; }
.chart-section[open] summary::before { transform: rotate(90deg); }
.chart-wrap {
  margin-top: 8px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 6px; padding: 16px;
}
.chart-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; margin-bottom: 14px; }
.chart-caption { font-size: 12px; color: var(--fg-muted); max-width: 80ch; }
.chart-scale-toggle {
  display: flex; gap: 0; align-items: center;
  font-size: 11px; color: var(--grey); white-space: nowrap;
}
.chart-scale-toggle button {
  font: inherit; font-size: 11px; padding: 2px 9px; margin-left: 0;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--fg-muted); cursor: pointer;
}
.chart-scale-toggle button:first-of-type { border-radius: 4px 0 0 4px; margin-left: 6px; }
.chart-scale-toggle button:last-of-type { border-radius: 0 4px 4px 0; border-left: none; }
.chart-scale-toggle button:hover { background: var(--surface-alt); }
.chart-scale-toggle button.active { background: var(--surface-alt); color: var(--link); font-weight: 600; }
.chart-wrap[data-scale="log"] .scale-linear { display: none; }
.chart-wrap[data-scale="linear"] .scale-log { display: none; }
.chart-bar, .chart-marker { transition: left .25s ease, width .25s ease; }
.chart-axis { position: relative; height: 16px; margin-left: 170px; font-size: 11px; color: var(--grey); }
.chart-axis span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.chart-axis span.tick-end { transform: translateX(-100%); }
.chart-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.chart-row:hover { background: var(--bg); }
.chart-label {
  flex: 0 0 160px; text-align: right; font-size: 12px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chart-track { position: relative; flex: 1; height: 16px; }
.chart-baseline { position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: var(--border); }
.chart-gridline { position: absolute; top: -3px; bottom: -3px; width: 1px; background: var(--border); opacity: .55; }
.chart-bar { position: absolute; top: 5px; height: 6px; border-radius: 3px; z-index: 1; }
.chart-bar-good { background: var(--green); opacity: .75; }
.chart-bar-bad  { background: var(--red); opacity: .75; }
.chart-bar-unknown { background: repeating-linear-gradient(90deg, var(--red) 0 5px, transparent 5px 10px); opacity: .6; }
.chart-bar-error   { background: repeating-linear-gradient(90deg, var(--grey) 0 5px, transparent 5px 10px); opacity: .5; }
.chart-marker {
  position: absolute; top: 50%; width: 9px; height: 9px;
  transform: translate(-50%, -50%); z-index: 2; display: block;
}
.chart-marker-fkb { width: 10px; height: 10px; }
.chart-shape {
  display: block; width: 100%; height: 100%; box-sizing: border-box;
  border-radius: 50%; border: 1.5px solid var(--surface);
}
.chart-shape-pin { background: var(--grey); }
.chart-shape-lkg { background: var(--green); }
.chart-shape-fkb { background: var(--red); border-radius: 2px; transform: rotate(45deg); }
.chart-legend {
  display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
  margin-top: 14px; font-size: 11px; color: var(--fg-muted);
}
.chart-legend > span { display: inline-flex; align-items: center; gap: 5px; }
.legend-swatch { display: inline-block; width: 18px; height: 6px; border-radius: 3px; }
.legend-dashed-red  { background: repeating-linear-gradient(90deg, var(--red) 0 5px, transparent 5px 10px); }
.legend-dashed-grey { background: repeating-linear-gradient(90deg, var(--grey) 0 5px, transparent 5px 10px); }
.chart-marker-demo {
  display: inline-block; width: 9px; height: 9px; border-radius: 50%;
  border: 1.5px solid var(--surface);
}
.chart-marker-demo.chart-shape-fkb { border-radius: 2px; transform: rotate(45deg); }
.chart-callouts {
  margin-top: 12px; font-size: 12px; color: var(--fg-muted);
  display: flex; flex-direction: column; gap: 4px;
}
footer {
  text-align: center; color: var(--grey); font-size: 12px;
  padding: 24px 16px 32px;
}
footer a { color: var(--grey); text-decoration: none; }
footer a:hover { text-decoration: underline; }
footer .sep { margin: 0 6px; }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition: none !important; }
}
"""


# All interaction is plain inline JS so the page stays a single static file.
JS = r"""
(() => {
  // ---- combined text + status filtering -------------------------------
  const input = document.getElementById('filter');
  const rowCount = document.getElementById('row-count');
  const noMatch = document.getElementById('no-match-row');
  const chips = Array.from(document.querySelectorAll('.stat[data-status]'));
  let statusFilter = '';

  const detailOf = row => {
    const sib = row.nextElementSibling;
    return sib && sib.classList.contains('detail-row') ? sib : null;
  };

  const applyFilters = () => {
    // Each query word must match at the start of a filter token, so e.g.
    // "compatible" doesn't also match rows tagged "incompatible".
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    let shown = 0, total = 0;
    document.querySelectorAll('tr.data-row').forEach(row => {
      total++;
      const haystack = ' ' + row.dataset.filter;
      const match = words.every(w => haystack.includes(' ' + w))
        && (!statusFilter || row.dataset.status === statusFilter);
      row.hidden = !match;
      const det = detailOf(row);
      if (det) det.hidden = !match || !row.classList.contains('expanded');
      if (match) shown++;
    });
    // The advance-map rows carry the same filter/status tokens.
    document.querySelectorAll('.chart-row').forEach(row => {
      const haystack = ' ' + row.dataset.filter;
      row.hidden = !(words.every(w => haystack.includes(' ' + w))
        && (!statusFilter || row.dataset.status === statusFilter));
    });
    if (noMatch) noMatch.hidden = shown !== 0;
    if (rowCount) {
      rowCount.textContent = shown === total
        ? `${total} downstream project${total === 1 ? '' : 's'}`
        : `Showing ${shown} of ${total} downstream projects`;
    }
  };

  input.addEventListener('input', applyFilters);

  chips.forEach(chip => {
    chip.addEventListener('click', () => {
      statusFilter = statusFilter === chip.dataset.status ? '' : chip.dataset.status;
      chips.forEach(c => c.classList.toggle('active',
        c.dataset.status === statusFilter && statusFilter !== ''));
      applyFilters();
    });
  });

  // ---- expandable detail rows ------------------------------------------
  document.querySelectorAll('tr.data-row').forEach(row => {
    const det = detailOf(row);
    if (!det) return;
    const btn = row.querySelector('.expander');
    const toggle = () => {
      const open = row.classList.toggle('expanded');
      det.hidden = !open;
      if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    };
    row.addEventListener('click', e => {
      if (e.target.closest('a, .links')) return;
      if (window.getSelection && String(window.getSelection())) return;
      toggle();
    });
  });

  // ---- column sorting (keeps each detail row glued to its data row) ----
  const tbody = document.querySelector('tbody');
  let sortCol = -1, sortAsc = true;
  document.querySelectorAll('th.sortable').forEach(th => {
    th.addEventListener('click', () => {
      const idx = Array.from(th.parentElement.children).indexOf(th);
      sortAsc = sortCol === idx ? !sortAsc : true;
      sortCol = idx;
      document.querySelectorAll('th.sortable').forEach(t => t.classList.remove('sort-asc', 'sort-desc'));
      th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
      const isNumeric = th.dataset.sortType === 'numeric';
      const pairs = Array.from(tbody.querySelectorAll('tr.data-row'))
        .map(row => ({ row, det: detailOf(row) }));
      pairs.sort((a, b) => {
        const av = a.row.children[idx]?.dataset.sortVal ?? '';
        const bv = b.row.children[idx]?.dataset.sortVal ?? '';
        const cmp = isNumeric ? parseFloat(av) - parseFloat(bv) : av.localeCompare(bv);
        return sortAsc ? cmp : -cmp;
      });
      pairs.forEach(p => {
        tbody.appendChild(p.row);
        if (p.det) tbody.appendChild(p.det);
      });
      if (noMatch) tbody.appendChild(noMatch);
    });
  });

  // ---- relative timestamps ----------------------------------------------
  const rel = secs => {
    if (secs < 90) return 'just now';
    const m = Math.floor(secs / 60);
    if (m < 90) return `${m} min ago`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  };
  document.querySelectorAll('[data-epoch]').forEach(el => {
    const t = parseInt(el.dataset.epoch, 10);
    if (!t) return;
    const secs = Math.floor(Date.now() / 1000) - t;
    if (secs < 0) return;
    el.textContent = (el.dataset.relPrefix || '') + rel(secs);
  });

  // ---- staleness warning ---------------------------------------------------
  const stale = document.getElementById('stale-warning');
  if (stale) {
    const t = parseInt(stale.dataset.reportedEpoch, 10);
    const threshold = parseInt(stale.dataset.staleHours, 10) || 36;
    const hours = (Date.now() / 1000 - t) / 3600;
    if (t && hours > threshold) {
      const agoText = hours < 72 ? `${Math.floor(hours)} hours` : `${Math.floor(hours / 24)} days`;
      stale.querySelector('span').textContent =
        `These results are ${agoText} old — the reporting pipeline may be stalled, `
        + 'so the data below may not reflect the current Mathlib.';
      stale.hidden = false;
    }
  }

  // ---- copy-SHA buttons -----------------------------------------------------
  document.querySelectorAll('.copy-sha').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();  // keep the row from toggling its detail panel
      navigator.clipboard.writeText(btn.dataset.sha).then(() => {
        btn.classList.add('copied');
        btn.setAttribute('data-tooltip', 'Copied!');
        setTimeout(() => {
          btn.classList.remove('copied');
          btn.setAttribute('data-tooltip', 'Copy full SHA');
        }, 1200);
      }).catch(() => { /* clipboard unavailable (e.g. non-secure context) */ });
    });
  });

  // ---- advance-map scale toggle (log ↔ linear) ----------------------------
  const chartWrap = document.querySelector('.chart-wrap');
  if (chartWrap) {
    const scaleBtns = chartWrap.querySelectorAll('.chart-scale-toggle button');
    scaleBtns.forEach(btn => btn.addEventListener('click', () => {
      const scale = btn.dataset.scale;
      chartWrap.dataset.scale = scale;
      scaleBtns.forEach(b => b.classList.toggle('active', b === btn));
      chartWrap.querySelectorAll('[data-lin-left]').forEach(el => {
        el.style.left = (scale === 'linear' ? el.dataset.linLeft : el.dataset.logLeft) + '%';
        const w = scale === 'linear' ? el.dataset.linWidth : el.dataset.logWidth;
        if (w !== undefined) el.style.width = w + '%';
      });
    }));
  }

  // ---- theme toggle: auto → light → dark, persisted in localStorage ------
  const themeBtn = document.getElementById('theme-toggle');
  if (themeBtn) {
    const THEMES = ['auto', 'light', 'dark'];
    const ICONS = { auto: '◐', light: '☀︎', dark: '☾' };
    const current = () => document.documentElement.dataset.theme || 'auto';
    const renderThemeBtn = () => {
      const t = current();
      themeBtn.textContent = `${ICONS[t]} ${t}`;
    };
    themeBtn.addEventListener('click', () => {
      const next = THEMES[(THEMES.indexOf(current()) + 1) % THEMES.length];
      if (next === 'auto') {
        delete document.documentElement.dataset.theme;
      } else {
        document.documentElement.dataset.theme = next;
      }
      try {
        if (next === 'auto') localStorage.removeItem('theme');
        else localStorage.setItem('theme', next);
      } catch (e) { /* private browsing: theme just won't persist */ }
      renderThemeBtn();
    });
    renderThemeBtn();
  }

  // ---- ?q= / ?status= deep links -----------------------------------------
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q') || params.get('filter');
  if (q) input.value = q;
  const statusAliases = {
    passed: 'passed', compatible: 'passed',
    failed: 'failed', incompatible: 'failed',
    error: 'error', errors: 'error',
  };
  const st = statusAliases[(params.get('status') || '').toLowerCase()];
  if (st) {
    statusFilter = st;
    chips.forEach(c => c.classList.toggle('active', c.dataset.status === st));
  }
  applyFilters();
})();
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
    cards = [
        ("", "", len(rows), "tracked", "All tracked downstream projects"),
        ("green", "passed", n_passed, "compatible", "Projects building successfully against the target Mathlib revision — click to filter"),
        ("red", "failed", n_failed, "incompatible", "Projects failing to build against the target Mathlib revision — click to filter"),
        ("yellow", "error", n_error, "errors", "Projects whose validation job hit an unexpected error — click to filter"),
    ]
    html = ['<div class="stats">']
    for cls, status, n, label, tip in cards:
        html.append(
            f'<button type="button" class="stat {cls}" data-status="{status}" data-tooltip="{esc(tip)}">'
            f'<div class="n">{n}</div><div class="l">{label}</div>'
            f'</button>'
        )
    html.append("</div>")
    return "".join(html)


def render_run_banner(
    *,
    run_id: str,
    run_url: str,
    upstream_ref: str,
    reported_at: Any,
    generated_at: str,
    target_banner: str,
) -> str:
    reported_epoch = iso_epoch(reported_at)
    reported_html = fmt_dt(reported_at)
    if reported_epoch:
        reported_html += (
            f' <span class="rel-note">(<span data-epoch="{reported_epoch}"></span>)</span>'
        )
    return (
        f'<div class="run-banner">'
        f'<div class="run-banner-title">Mathlib Downstream Report</div>'
        f'<div class="run-banner-meta">'
        f'<span><strong>Upstream ref:</strong>&nbsp;<code>{esc(upstream_ref)}</code></span>'
        f'<span class="divider">|</span>'
        f'<span><strong>Latest run:</strong>&nbsp;<a href="{esc(run_url)}" target="_blank" rel="noopener noreferrer">{esc(run_id)}</a></span>'
        f'<span class="divider">|</span>'
        f'<span><strong>Reported:</strong>&nbsp;{reported_html}</span>'
        f'{target_banner}'
        f'<span style="margin-left:auto;color:var(--grey);font-size:12px;">Generated&nbsp;{esc(generated_at)}</span>'
        f'<button type="button" id="theme-toggle" class="theme-toggle" '
        f'title="Color theme — follows your system by default; click to cycle auto / light / dark" '
        f'aria-label="Toggle color theme">◐ auto</button>'
        f'</div>'
        f'</div>'
    )


def render_window_strip(
    r: dict,
    *,
    ct,
    cd,
    tg,
) -> str:
    """Render a left-to-right strip of the Mathlib commit window for one row.

    Nodes appear in history order (pinned → last known good → first known
    bad → target); consecutive nodes that are the same commit are merged.
    """
    if r.get("search_base_not_ancestor"):
        return ""
    outcome = r.get("outcome")
    if outcome == "error":
        return ""

    pin = r.get("pinned_commit")
    lkg = r.get("last_known_good")
    fkb = r.get("first_known_bad")
    target = r.get("target_commit")

    # (sha, label, kind) in history order; kind drives node/segment colours.
    candidates = [
        (pin, "pinned", "good"),
        (lkg, "last known good", "good"),
        (fkb, "first known bad", "bad"),
        (target, "target", "good" if outcome == "passed" else "bad"),
    ]
    nodes: list[dict] = []
    for sha, label, kind in candidates:
        if not sha:
            continue
        if nodes and nodes[-1]["sha"] == sha:
            nodes[-1]["labels"].append(label)
            if kind == "bad":
                nodes[-1]["kind"] = "bad"
            continue
        nodes.append({"sha": sha, "labels": [label], "kind": kind})
    if len(nodes) < 2:
        return ""

    age = r.get("age_commits")
    bump = r.get("bump_commits")

    def seg_distance(prev: dict, node: dict) -> int | None:
        """Commit count between two strip nodes, derived from the row's
        age/bump counters (LKG and FKB are adjacent, so FKB→target is
        age − bump − 1)."""
        proles, nroles = set(prev["labels"]), set(node["labels"])
        if "pinned" in proles and "last known good" in nroles:
            return bump
        if "first known bad" in proles and "target" in nroles:
            if age is None:
                return None
            d = age - (bump if bump is not None else 0) - 1
            return d if d >= 0 else None
        if "pinned" in proles and "target" in nroles:
            return age
        if "last known good" in proles and "target" in nroles:
            if age is None or bump is None:
                return None
            d = age - bump
            return d if d >= 0 else None
        return None

    def seg_with_note(seg_cls: str, prev: dict, node: dict, extra_note: str | None = None) -> str:
        dist = seg_distance(prev, node)
        notes = [extra_note] if extra_note else []
        if dist is not None:
            notes.append(f"{dist} commit{'s' if dist != 1 else ''}")
        if not notes:
            return f'<div class="ws-seg {seg_cls}"></div>'
        tip = ""
        if dist is not None:
            tip_text = (
                f"{dist} commit{'s' if dist != 1 else ''} between"
                f" {prev['labels'][-1]} and {node['labels'][0]}"
            )
            tip = f' data-tooltip="{esc(tip_text)}"'
        return (
            f'<div class="ws-seg-wrap"{tip}>'
            f'<div class="ws-seg {seg_cls}"></div>'
            f'<div class="ws-seg-note">{esc(" · ".join(notes))}</div>'
            f'</div>'
        )

    parts: list[str] = []
    for i, node in enumerate(nodes):
        if i:
            prev = nodes[i - 1]
            if prev["kind"] == "good" and node["kind"] == "bad":
                if fkb and prev["sha"] == lkg and node["sha"] == fkb:
                    # Last known good / first known bad are always an adjacent
                    # commit pair, so this is a single-commit boundary.
                    parts.append(
                        '<div class="ws-seg-wrap ws-adjacent" '
                        'data-tooltip="Adjacent commits — the break happens exactly at this boundary">'
                        '<div class="ws-seg ws-break"></div>'
                        '</div>'
                    )
                else:
                    # A failed row without bisect endpoints: the break lies
                    # somewhere unidentified inside this segment.
                    parts.append(seg_with_note("ws-unknown", prev, node, "break not yet located"))
            elif node["kind"] == "bad":
                parts.append(seg_with_note("ws-bad", prev, node))
            else:
                parts.append(seg_with_note("ws-good", prev, node))
        sha = node["sha"]
        label = " = ".join(node["labels"])
        link = commit_link(UPSTREAM_REPO, sha, ct(sha), tg(sha), cd(sha))
        parts.append(
            f'<div class="ws-node">'
            f'<span class="ws-dot ws-{node["kind"]}"></span>'
            f'{link}'
            f'<span class="ws-label">{esc(label)}</span>'
            f'</div>'
        )
    return (
        '<div class="window-strip-wrap">'
        '<div class="window-strip-caption">Mathlib commit window (older → newer)</div>'
        f'<div class="window-strip">{"".join(parts)}</div>'
        '</div>'
    )


def render_chart(
    rows: list[dict],
    *,
    commit_titles: dict[str, dict[str, str | None]],
    sha_to_tag: dict[str, str],
) -> str:
    """Render the advance map: one track per downstream on a shared
    commits-behind-target axis (log scale).

    Because the axis is shared and monotonic, projects broken by the same
    Mathlib commit have their first-known-bad markers vertically aligned.
    """

    def ct(sha: str | None) -> str | None:
        info = commit_titles.get(sha) if sha else None
        return info.get("title") if info else None

    def tg(sha: str | None) -> str | None:
        return sha_to_tag.get(sha) if sha else None

    included: list[dict] = []
    excluded: list[tuple[str, str]] = []
    for r in rows:
        name = r.get("downstream", "")
        if r.get("search_base_not_ancestor"):
            excluded.append((name, "pinned revision is not part of the target's history"))
        elif r.get("age_commits") is None or not r.get("pinned_commit"):
            excluded.append((name, "no commit-distance data"))
        else:
            included.append(r)
    if not included:
        return ""

    dmax = max(r["age_commits"] for r in included) or 1

    # Both scales are rendered: log positions inline (the default), linear in
    # data attributes that the scale toggle swaps in client-side.
    def x_log(d: int) -> float:
        return 100.0 * (1.0 - math.log1p(d) / math.log1p(dmax))

    def x_lin(d: int) -> float:
        return 100.0 * (1.0 - d / dmax)

    _TICK_STEPS = (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000)

    # Log ticks: nice round commit counts, kept when they don't crowd.
    log_ticks: list[int] = [0]
    tick_positions = [100.0]
    for c in _TICK_STEPS:
        if c > dmax:
            break
        p = x_log(c)
        if all(abs(p - q) >= 8.0 for q in tick_positions):
            log_ticks.append(c)
            tick_positions.append(p)

    # Linear ticks: evenly spaced at a round step size.
    lin_step = next((c for c in _TICK_STEPS if dmax / c <= 6), max(dmax // 5, 1))
    lin_ticks = list(range(0, dmax + 1, lin_step))

    def axis_span(c: int, pos: float, scale_cls: str) -> str:
        cls = f"{scale_cls} tick-end" if c == 0 else scale_cls
        label = "target" if c == 0 else f"-{c}"
        return f'<span class="{cls}" style="left:{pos:.2f}%">{label}</span>'

    axis_html = (
        '<div class="chart-axis">'
        + "".join(axis_span(c, x_log(c), "scale-log") for c in log_ticks)
        + "".join(axis_span(c, x_lin(c), "scale-linear") for c in lin_ticks)
        + "</div>"
    )
    gridlines = (
        "".join(f'<div class="chart-gridline scale-log" style="left:{x_log(c):.2f}%"></div>' for c in log_ticks)
        + "".join(f'<div class="chart-gridline scale-linear" style="left:{x_lin(c):.2f}%"></div>' for c in lin_ticks)
    )

    def _scale_attrs(d_far: int, d_near: int | None = None) -> str:
        """Inline log position plus data attributes for both scales."""
        attrs = (
            f'style="left:{x_log(d_far):.2f}%'
            + (f';width:{x_log(d_near) - x_log(d_far):.2f}%' if d_near is not None else "")
            + f'" data-log-left="{x_log(d_far):.2f}" data-lin-left="{x_lin(d_far):.2f}"'
        )
        if d_near is not None:
            attrs += (
                f' data-log-width="{x_log(d_near) - x_log(d_far):.2f}"'
                f' data-lin-width="{x_lin(d_near) - x_lin(d_far):.2f}"'
            )
        return attrs

    def marker(role_cls: str, role_label: str, sha: str, d: int) -> str:
        tag = tg(sha)
        ident = (short_sha(sha) or "") + (f" ({tag})" if tag else "")
        parts = [f"{role_label}: {ident}"]
        title = ct(sha)
        if title:
            parts.append(title)
        parts.append(f"{d} commit{'s' if d != 1 else ''} behind target" if d else "= target")
        tip = "&#10;".join(esc(p) for p in parts)
        url = f"{GITHUB}/{UPSTREAM_REPO}/commit/{sha}"
        # The visual shape lives in an inner span so the FKB diamond's
        # rotation can't tilt the tooltip pseudo-elements on the anchor.
        return (
            f'<a class="chart-marker chart-marker-{role_cls}" {_scale_attrs(d)} '
            f'href="{esc(url)}" target="_blank" rel="noopener noreferrer" data-tooltip="{tip}">'
            f'<span class="chart-shape chart-shape-{role_cls}"></span></a>'
        )

    def bar(cls: str, d_far: int, d_near: int) -> str:
        if x_log(d_near) - x_log(d_far) <= 0:
            return ""
        return f'<div class="chart-bar {cls}" {_scale_attrs(d_far, d_near)}></div>'

    # Failures first (these are what the map is for), then errors, then passing.
    group_order = {"failed": 0, "error": 1, "passed": 2}
    included.sort(key=lambda r: (
        group_order.get(r.get("outcome"), 3),
        -(r.get("age_commits") or 0),
        r.get("downstream", "").lower(),
    ))

    row_divs: list[str] = []
    for r in included:
        name = r.get("downstream", "")
        repo = r.get("repo", "")
        outcome = r.get("outcome", "")
        age = r["age_commits"]
        bump = r.get("bump_commits")
        pin = r.get("pinned_commit")
        lkg = r.get("last_known_good")
        fkb = r.get("first_known_bad")
        d_lkg = max(age - bump, 0) if bump is not None else None

        track = ['<div class="chart-baseline"></div>', gridlines]
        if outcome == "passed":
            track.append(bar("chart-bar-good", age, 0))
        elif outcome == "failed" and lkg and fkb and d_lkg is not None:
            d_fkb = max(d_lkg - 1, 0)
            track.append(bar("chart-bar-good", age, d_lkg))
            track.append(bar("chart-bar-bad", d_fkb, 0))
        elif outcome == "failed":
            track.append(bar("chart-bar-unknown", age, 0))
        else:
            track.append(bar("chart-bar-error", age, 0))

        if pin:
            track.append(marker("pin", "pinned", pin, age))
        if lkg and d_lkg:
            track.append(marker("lkg", "last known good", lkg, d_lkg))
        if outcome == "failed" and fkb and d_lkg is not None:
            track.append(marker("fkb", "first known bad", fkb, max(d_lkg - 1, 0)))

        compatibility_search = {"passed": "compatible", "failed": "incompatible"}.get(outcome, outcome)
        filter_tokens = " ".join(filter(None, [
            name.lower(), repo.lower(), compatibility_search,
            short_sha(pin), short_sha(lkg), short_sha(fkb),
        ]))
        row_divs.append(
            f'<div class="chart-row" data-filter="{esc(filter_tokens)}" data-status="{esc(outcome)}">'
            f'<div class="chart-label" title="{esc(repo)}">{esc(name)}</div>'
            f'<div class="chart-track">{"".join(track)}</div>'
            f'</div>'
        )

    legend_html = (
        '<div class="chart-legend">'
        '<span><span class="legend-swatch" style="background:var(--green)"></span>safe to advance (pinned → last known good)</span>'
        '<span><span class="legend-swatch" style="background:var(--red)"></span>incompatible (first known bad → target)</span>'
        '<span><span class="legend-swatch legend-dashed-red"></span>break not yet located</span>'
        '<span><span class="legend-swatch legend-dashed-grey"></span>validation error</span>'
        '<span><span class="chart-marker-demo chart-shape-pin"></span>pinned</span>'
        '<span><span class="chart-marker-demo chart-shape-lkg"></span>last known good</span>'
        '<span><span class="chart-marker-demo chart-shape-fkb"></span>first known bad</span>'
        '</div>'
    )

    callouts: list[str] = []
    fkb_groups: dict[str, list[str]] = defaultdict(list)
    for r in included:
        if r.get("outcome") == "failed" and r.get("first_known_bad"):
            fkb_groups[r["first_known_bad"]].append(r.get("downstream", ""))
    for sha, names in sorted(fkb_groups.items(), key=lambda kv: -len(kv[1])):
        if len(names) < 2:
            continue
        link = commit_link(UPSTREAM_REPO, sha, ct(sha), tg(sha))
        title = ct(sha)
        title_html = f' <span class="detail-note">(“{esc(title)}”)</span>' if title else ""
        callouts.append(
            f"<div>⚠ {len(names)} projects are broken by the same commit "
            f"{link}{title_html}: {esc(', '.join(sorted(names)))}</div>"
        )
    targets = {r.get("target_commit") for r in included if r.get("target_commit")}
    if len(targets) > 1:
        callouts.append(
            "<div>Projects were validated against different target revisions, so "
            "horizontal positions are only approximately comparable across rows.</div>"
        )
    for name, reason in excluded:
        callouts.append(f"<div>Not shown: <strong>{esc(name)}</strong> — {esc(reason)}.</div>")
    callouts_html = f'<div class="chart-callouts">{"".join(callouts)}</div>' if callouts else ""

    scale_toggle = (
        '<div class="chart-scale-toggle">scale:'
        '<button type="button" data-scale="log" class="active" '
        'title="Logarithmic — keeps every project readable when commit distances vary widely">log</button>'
        '<button type="button" data-scale="linear" '
        'title="Linear — bar lengths are proportional to commit counts">linear</button>'
        '</div>'
    )
    return (
        '<details class="chart-section" open>'
        '<summary>Advance map — how far behind each project stands, and how far it can safely move</summary>'
        '<div class="chart-wrap" data-scale="log">'
        '<div class="chart-head">'
        '<div class="chart-caption">Commits behind the target Mathlib revision (the right edge is the target). '
        'Projects broken by the same commit have their first-known-bad markers vertically aligned.</div>'
        f'{scale_toggle}'
        '</div>'
        f'{axis_html}'
        f'<div class="chart-rows">{"".join(row_divs)}</div>'
        f'{legend_html}'
        f'{callouts_html}'
        '</div>'
        '</details>'
    )


def detail_narrative(r: dict, *, ct, cd, tg) -> str:
    """One plain-English paragraph summarising this row for newcomers."""
    name = esc(r.get("downstream", ""))
    outcome = r.get("outcome")
    target = r.get("target_commit")
    lkg = r.get("last_known_good")
    fkb = r.get("first_known_bad")
    bump = r.get("bump_commits")
    age = r.get("age_commits")

    def link(sha):
        return commit_link(UPSTREAM_REPO, sha, ct(sha), tg(sha), cd(sha))

    def titled(sha):
        title = ct(sha)
        out = link(sha)
        if title:
            out += f' <span class="detail-note">(“{esc(title)}”)</span>'
        return out

    if outcome == "passed":
        text = f"<strong>{name}</strong> builds successfully against Mathlib {link(target)}."
        if isinstance(age, int) and age == 0:
            text += " Its dependency pin is fully up to date."
        elif isinstance(bump, int) and bump > 0:
            text += (
                f" Its pin can be safely advanced by {bump} commit{'s' if bump != 1 else ''}"
                f" to the last known good revision {link(lkg)}." if lkg else
                f" Its pin can be safely advanced by {bump} commit{'s' if bump != 1 else ''}."
            )
        return f'<p class="detail-summary">{text}</p>'

    if outcome == "failed":
        text = f"<strong>{name}</strong> fails to build against Mathlib {link(target)}."
        if fkb and lkg and not r.get("commit_window_truncated"):
            # LKG and FKB are always an adjacent commit pair, so FKB is the
            # commit that introduced the break.
            text += (
                f" The incompatibility was introduced by Mathlib commit {titled(fkb)}."
                f" The commit immediately before it, {link(lkg)}, still works and is"
                f" a safe upgrade target."
            )
        else:
            if fkb:
                text += (
                    f" The earliest known incompatible Mathlib commit is {titled(fkb)}."
                )
            if lkg:
                text += f" The most recent revision that still works is {link(lkg)}."
        if not fkb and not lkg:
            if r.get("search_base_not_ancestor"):
                text += (
                    " The pinned revision is not part of the target's history,"
                    " so no commit window could be searched for the breaking commit."
                )
            else:
                text += (
                    " The breaking commit has not been located yet — it lies somewhere"
                    " between the pinned revision and the target."
                )
        return f'<p class="detail-summary">{text}</p>'

    # error
    text = (
        f"The latest validation run for <strong>{name}</strong> hit an unexpected"
        f" error before producing a result. This usually points to an infrastructure"
        f" problem (runner, network, cache) rather than a Mathlib incompatibility."
    )
    return f'<p class="detail-summary">{text}</p>'


def render_detail_row(
    r: dict,
    *,
    n_cols: int,
    ct,
    cd,
    tg,
    btns_html: str,
) -> str:
    """Render the hidden expandable panel that follows each data row."""
    outcome = r.get("outcome")

    blocks: list[str] = [detail_narrative(r, ct=ct, cd=cd, tg=tg)]

    strip = render_window_strip(r, ct=ct, cd=cd, tg=tg)
    if strip:
        blocks.append(strip)

    facts: list[tuple[str, str]] = []

    checked = r.get("row_reported_at")
    if checked:
        checked_html = esc(fmt_dt(checked))
        epoch = iso_epoch(checked)
        if epoch:
            checked_html += f' <span class="rel-note">(<span data-epoch="{epoch}"></span>)</span>'
        facts.append(("Last checked", checked_html))

    duration = fmt_duration(r.get("job_started_at"), r.get("job_finished_at"))
    if duration:
        facts.append(("Validation took", esc(duration)))

    mode = r.get("search_mode")
    if mode:
        facts.append(("How it was checked", esc(SEARCH_MODE_DESC.get(mode, mode))))

    stage = r.get("failure_stage")
    if stage and outcome in ("failed", "error"):
        stage_html = f"<code>{esc(stage)}</code>"
        if stage in FAILURE_STAGE_DESC:
            stage_html += f" — {esc(FAILURE_STAGE_DESC[stage])}"
        facts.append(("Failed during", stage_html))

    if facts:
        facts_html = "".join(
            f'<span class="df-k">{k}</span><span class="df-v">{v}</span>'
            for k, v in facts
        )
        blocks.append(f'<div class="detail-facts">{facts_html}</div>')

    warns: list[str] = []
    if r.get("commit_window_truncated"):
        warns.append(
            "The commit window was too large to search fully, so the boundary"
            " shown may not be the exact breaking commit."
        )
    if r.get("search_base_not_ancestor"):
        warns.append(
            "The pinned revision is not an ancestor of the target, so no commit"
            " window could be searched — only the target itself was validated."
        )
    for w in warns:
        blocks.append(f'<div class="detail-warn">⚠ <span>{esc(w)}</span></div>')

    error_text = r.get("error")
    if error_text and outcome == "error":
        shown = str(error_text)
        if len(shown) > 600:
            shown = shown[:600] + " …"
        blocks.append(f'<pre class="detail-error">{esc(shown)}</pre>')

    if btns_html:
        blocks.append(f'<div class="links">{btns_html}</div>')

    return (
        f'<tr class="detail-row" hidden><td colspan="{n_cols}">'
        f'<div class="detail">{"".join(blocks)}</div>'
        f'</td></tr>'
    )


HIST_CLASS = {
    "passed": "hist-passed",
    "failed": "hist-failed",
    "error":  "hist-error",
}


def render_history_strip(history: list[dict]) -> str:
    """Render the recent-run squares for one downstream (oldest → newest).

    *history* is newest-first, as loaded from storage.  A single entry carries
    no more information than the row itself, so the strip needs at least two.

    Failed runs whose first-known-bad differs from the most recent failure's
    render in orange: the project was incompatible then too, but because of a
    different breaking commit than the current one.
    """
    if len(history) < 2:
        return ""
    current_fkb = next(
        (h.get("first_known_bad") for h in history
         if h.get("outcome") == "failed" and h.get("first_known_bad")),
        None,
    )
    cells = []
    for h in reversed(history[:HISTORY_LIMIT]):
        outcome = h.get("outcome", "")
        cls = HIST_CLASS.get(outcome, "hist-error")
        label = COMPATIBILITY_LABEL.get(outcome, outcome)
        fkb = h.get("first_known_bad")
        if outcome == "failed" and current_fkb and fkb and fkb != current_fkb:
            cls = "hist-failed-other"
            label = f"{label} · different breaking commit ({short_sha(fkb)})"
        when = str(h.get("reported_at") or "")[:16].replace("T", " ")
        tip = esc(f"{label} · {when}" if when else label)
        url = h.get("run_url")
        if url:
            cells.append(
                f'<a class="hist-cell {cls}" href="{esc(url)}" target="_blank" '
                f'rel="noopener noreferrer" data-tooltip="{tip}" aria-label="{tip}"></a>'
            )
        else:
            cells.append(f'<span class="hist-cell {cls}" data-tooltip="{tip}" tabindex="0"></span>')
    n = len(cells)
    return (
        f'<div class="history-strip" role="img" '
        f'aria-label="Outcomes of the last {n} runs, oldest to newest">{"".join(cells)}</div>'
    )


def copy_sha_btn(sha: str | None) -> str:
    if not sha:
        return ""
    return (
        f'<button type="button" class="copy-sha" data-sha="{esc(sha)}" '
        f'data-tooltip="Copy full SHA" aria-label="Copy full commit SHA">⧉</button>'
    )


def render_table_row(
    r: dict,
    *,
    run_url: str,
    commit_titles: dict[str, dict[str, str | None]],
    downstream_commit_titles: dict[str, dict[str, str | None]],
    sha_to_tag: dict[str, str],
    lgr_distances: dict[tuple[str, str], int | None] | None = None,
    n_cols: int = 10,
    history: list[dict] | None = None,
) -> str:
    downstream = r.get("downstream", "")
    repo = r.get("repo", "")
    repo_url = f"{GITHUB}/{repo}" if repo and "/" in repo else None

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
    lgr = r.get("last_good_release_commit")
    lgr_tag = r.get("last_good_release")
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

    name_main = '<div class="name">'
    if repo_url:
        name_main += f'<a href="{esc(repo_url)}" title="{esc(repo)}" target="_blank" rel="noopener noreferrer">{esc(downstream)}</a>'
    else:
        name_main += esc(downstream)
    name_main += "</div>"
    if repo:
        name_main += f'<div class="repo-label">{esc(repo)}</div>'
    name_main += '<div class="episode-label">'
    if ds_commit_link:
        name_main += f'<tt>@</tt> {ds_commit_link}&nbsp;'
    if episode_title and episode_badge:
        name_main += f'<span title="{esc(episode_title)}">{episode_badge}</span>'
    name_main += '</div>'
    name_cell = (
        '<div class="name-wrap">'
        '<button type="button" class="expander" aria-expanded="false" aria-label="Toggle run details">▸</button>'
        f'<div class="name-main">{name_main}</div>'
        '</div>'
    )

    compatibility_cell = badge(r.get("outcome"), COMPATIBILITY_CLASS, label_map=COMPATIBILITY_LABEL, tooltip_map=COMPATIBILITY_TOOLTIP)
    compatibility_cell += render_history_strip(history or [])
    checked_epoch = iso_epoch(r.get("row_reported_at"))
    if checked_epoch:
        _checked_tip = f"This downstream&#39;s latest validation run finished at&#10;{esc(fmt_dt(r.get('row_reported_at')))}"
        compatibility_cell += (
            f'<div class="checked-sub" data-tooltip="{_checked_tip}">'
            f'<span data-epoch="{checked_epoch}" data-rel-prefix="checked ">checked {esc(fmt_dt(r.get("row_reported_at")))}</span>'
            f'</div>'
        )
    target_cell   = commit_link(UPSTREAM_REPO, target, ct(target), tg(target), cd(target))
    lkg_cell      = commit_link(UPSTREAM_REPO, lkg,    ct(lkg),    tg(lkg),    cd(lkg)) + copy_sha_btn(lkg)
    lgr_link      = commit_link(UPSTREAM_REPO, lgr,    ct(lgr),    lgr_tag,    cd(lgr))
    fkb_cell      = commit_link(UPSTREAM_REPO, fkb,    ct(fkb),    tg(fkb),    cd(fkb)) + copy_sha_btn(fkb)
    _pin_link = commit_link(UPSTREAM_REPO, pin, ct(pin), tg(pin), cd(pin))
    if r.get("search_base_not_ancestor"):
        _tip = "Pinned commit is not an ancestor of the current target\n No bisect window available: the validation only performed a HEAD probe"
        pin_cell = (
            f'<div>{_pin_link}</div>'
            f'<div><span class="badge badge-yellow" data-tooltip="{esc(_tip)}">detached</span></div>'
        )
    else:
        pin_cell = _pin_link
    age_days  = days_between(cd(pin), cd(target))
    age_cell  = distance_cell(age_val, age_days)
    bump_cell = distance_cell(bump_val)

    lgr_dist: int | None = None
    if lgr_distances and pin and lgr:
        lgr_dist = lgr_distances.get((pin, lgr))
    if lgr_dist is not None and lgr_dist != 0:
        if lgr_dist > 0:
            tip = f"This tag is newer than the downstream&#39;s pinned revision by {lgr_dist} commit{'s' if lgr_dist != 1 else ''}"
            dist_html = f'<div class="distance-sub" data-tooltip="{tip}">(+{lgr_dist})</div>'
        else:
            count = abs(lgr_dist)
            tip = f"This tag is older than the downstream&#39;s pinned revision by {count} commit{'s' if count != 1 else ''}"
            dist_html = f'<div class="distance-sub" data-tooltip="{tip}">({lgr_dist})</div>'
        lgr_cell = f'<div>{lgr_link}{dist_html}</div>'
    else:
        lgr_cell = lgr_link

    row_run_url = r.get("run_url") or run_url
    btns: list[str] = []
    # One CI link per row: the validation job when its metadata is available,
    # otherwise the full workflow run as a fallback (the validate_job join is
    # best-effort, so job_url can be missing).
    if r.get("job_url"):
        btns.append(f'<a href="{esc(r["job_url"])}" class="btn" target="_blank" rel="noopener noreferrer">Validation job&nbsp;↗</a>')
    elif row_run_url:
        btns.append(f'<a href="{esc(row_run_url)}" class="btn" target="_blank" rel="noopener noreferrer">Full run&nbsp;↗</a>')
    culprit_log_url = r.get("culprit_log_artifact_url")
    if culprit_log_url:
        btns.append(
            f'<a href="{esc(culprit_log_url)}" class="btn" '
            'data-tooltip="Download the failing-commit build log"'
            ' target="_blank" rel="noopener noreferrer">Failure log&nbsp;↗</a>'
        )
    btns_html = "".join(btns)
    links_cell = f'<div class="links">{btns_html}</div>'

    _av = str(age_days) if age_days is not None else (str(age_val) if age_val is not None else "-1")
    _bv = str(bump_val) if bump_val is not None else "-1"
    cells = (
        f'<td data-sort-val="{esc(downstream.lower())}">{name_cell}</td>'
        f'<td data-sort-val="{esc(pin or "")}">{pin_cell}</td>'
        f'<td data-sort-val="{_av}">{age_cell}</td>'
        f'<td data-sort-val="{esc(target or "")}">{target_cell}</td>'
        f'<td data-sort-val="{esc(r.get("outcome", ""))}">{compatibility_cell}</td>'
        f'<td data-sort-val="{esc(lkg or "")}">{lkg_cell}</td>'
        f'<td data-sort-val="{esc(fkb or "")}">{fkb_cell}</td>'
        f'<td data-sort-val="{_bv}">{bump_cell}</td>'
        f'<td data-sort-val="{esc(lgr_tag or "")}">{lgr_cell}</td>'
        f"<td>{links_cell}</td>"
    )
    ep_label = EPISODE_LABEL.get(episode_state, episode_state or "")
    compatibility_search = {"passed": "compatible", "failed": "incompatible"}.get(r.get("outcome", ""), r.get("outcome", ""))
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
        lgr_tag,
        short_sha(lgr),
        short_sha(fkb),
        tg(fkb),
    ]))
    data_row = (
        f'<tr class="data-row" data-filter="{esc(filter_tokens)}" '
        f'data-status="{esc(r.get("outcome", ""))}">{cells}</tr>'
    )
    detail_row = render_detail_row(
        r, n_cols=n_cols, ct=ct, cd=cd, tg=tg, btns_html=btns_html,
    )
    return data_row + "\n" + detail_row


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
    lgr_distances: dict[tuple[str, str], int | None] | None = None,
    history: dict[str, list[dict]] | None = None,
) -> str:
    col_glossary_items = "".join(
        f'<div class="col-glossary-item">'
        f'<span class="col-glossary-key">{label}</span>'
        f'<span class="col-glossary-val">{esc(desc)}</span>'
        f'</div>'
        for label, desc in [
            ("Downstream",        COL_DESC["downstream"].splitlines()[0]),
            ("Target",            COL_DESC["target"]),
            ("Compatibility",     COL_DESC["compatibility"]),
            ("Last known good",   COL_DESC["last_known_good"]),
            ("Last good release", COL_DESC["last_good_release"]),
            ("First known bad",   COL_DESC["first_known_bad"]),
            ("Pinned",            COL_DESC["pinned"]),
            ("Age",               COL_DESC["age"]),
            ("Bump",              COL_DESC["bump"]),
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
    hopscotch_url = f"{GITHUB}/{HOPSCOTCH_REPO}"
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
        f'<a href="{hopscotch_url}" target="_blank" rel="noopener noreferrer">hopscotch</a> '
        f'to scan the mathlib history between the pinned revision and the target, '
        f'to identify the <em>first known bad</em> commit — the earliest Mathlib revision incompatible with the downstream — '
        f'and the <em>last known good</em> commit just before it.</li>'
        f'<li><strong>How much can I safely advance the dependency?</strong> '
        f'The <em>last known good</em> commit is a safe upgrade target. '
        f'The <em>bump</em> column shows the distance between it and the currently pinned revision.</li>'
        f'</ul>'
        f'<p class="report-desc-footer">'
        f'To register a project here or learn how the workflow operates, see the '
        f'<a href="{readme_url}" target="_blank" rel="noopener noreferrer">downstream-reports README</a>.'
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

    n_cols = 10

    table_rows = [
        render_table_row(
            r,
            run_url=run_url,
            commit_titles=commit_titles,
            downstream_commit_titles=downstream_commit_titles,
            sha_to_tag=sha_to_tag,
            lgr_distances=lgr_distances,
            n_cols=n_cols,
            history=(history or {}).get(r.get("downstream", ""), []),
        )
        for r in sorted(rows, key=sort_key)
    ]

    def _th(label: str, key: str | None = None, sortable: bool = False, sort_type: str = "string") -> str:
        cls = ' class="sortable"' if sortable else ""
        stype = f' data-sort-type="{sort_type}"' if sortable else ""
        inner = f'<span data-tooltip="{esc(COL_DESC[key])}" tabindex="0">{label}</span>' if key else label
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
        + _th("Last good release", "last_good_release")
        + _th("Links")
    )

    if not table_rows:
        table_rows = [
            f'<tr><td colspan="{n_cols}" style="text-align:center;padding:20px;color:var(--none);">'
            "No results for this run.</td></tr>"
        ]
    no_match_row = (
        f'<tr id="no-match-row" hidden><td colspan="{n_cols}" '
        'style="text-align:center;padding:20px;color:var(--none);">'
        "No downstream matches the current filters.</td></tr>"
    )

    chart_html = render_chart(rows, commit_titles=commit_titles, sha_to_tag=sha_to_tag)

    # Filled in and unhidden client-side when the data is older than
    # STALE_AFTER_HOURS — guards visitors against a silently stalled pipeline.
    stale_epoch = iso_epoch(reported_at)
    stale_html = (
        f'<div id="stale-warning" class="stale-warning" hidden '
        f'data-reported-epoch="{stale_epoch}" data-stale-hours="{STALE_AFTER_HOURS}">'
        f'⚠ <span></span></div>'
    ) if stale_epoch else ""

    tbody = "\n".join(table_rows) + "\n" + no_match_row
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <meta name="description" content="Compatibility dashboard for Lean projects that depend on mathlib4: which Mathlib revision each project builds against, which commit broke it, and how far its pin can safely advance.">
  <meta property="og:title" content="Mathlib Downstream Status">
  <meta property="og:description" content="Which Mathlib commit broke which downstream project, and how far each pin can safely advance.">
  <meta property="og:type" content="website">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect x='1' y='3' width='6' height='6' rx='1.5' fill='%2322863a'/%3E%3Crect x='9' y='7' width='6' height='6' rx='1.5' fill='%23b31d28'/%3E%3C/svg%3E">
  <title>Mathlib Downstream Status</title>
  <script>
    /* Apply a saved theme override before first paint to avoid flashing. */
    try {{
      const t = localStorage.getItem('theme');
      if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
    }} catch (e) {{}}
  </script>
  <style>
{CSS}  </style>
</head>
<body>
<main>
  {run_banner}
  {stale_html}
  {report_desc}
  {stats_html}
  <div class="tips">
    <strong>Tips:</strong>
    Click a row to expand a plain-English explanation of its latest run. Hover any badge or commit SHA for details. Click the cards above to filter by status, column headers to sort, or use the filter box to find your project.
  </div>
  <div class="filter-bar">
    <input id="filter" type="search" placeholder="Filter by repository, commit, compatibility…" aria-label="Filter by repository, commit, compatibility">
  </div>
  <div class="row-count" id="row-count"></div>
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
  {chart_html}
</main>
<footer>
  Generated {esc(generated_at)}&nbsp;&middot;&nbsp;<a href="{esc(run_url)}" target="_blank" rel="noopener noreferrer">Workflow run {esc(run_id)}</a>&nbsp;&middot;&nbsp;<a href="{readme_url}" target="_blank" rel="noopener noreferrer">About this dashboard</a>&nbsp;&middot;&nbsp;<a href="{hopscotch_url}" target="_blank" rel="noopener noreferrer">hopscotch</a>
  <br>
  Raw data for tooling: <a href="{SNAPSHOT_BASE}/lkg/latest.json" target="_blank" rel="noopener noreferrer">lkg/latest.json</a>&nbsp;&middot;&nbsp;<a href="{SNAPSHOT_BASE}/runs/latest.json" target="_blank" rel="noopener noreferrer">runs/latest.json</a>
</footer>
<script>
{JS}</script>
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
    ap.add_argument(
        "--upstream-dir",
        help="Path to a bare/blobless local clone of the upstream repo; "
             "when provided, commit info, tags, and distances are read locally "
             "instead of via the GitHub API",
    )
    args = ap.parse_args()

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if args.backend == "sql":
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            ap.error("POSTGRES_DSN environment variable is required for --backend sql")

        from scripts.storage import (
            create_sql_engine,
            latest_regression_run_id,
            load_recent_outcomes,
            load_run_for_site,
        )

        engine = create_sql_engine(dsn)

        run_id = args.run_id
        if not run_id:
            run_id = latest_regression_run_id(engine)
            if not run_id:
                ap.error("No regression runs found in the database.")

        run_meta, rows = load_run_for_site(engine, run_id)
        history = load_recent_outcomes(
            engine, run_meta.get("upstream") or UPSTREAM_REPO, limit=HISTORY_LIMIT,
        )

    else:  # filesystem
        if not args.state_root:
            ap.error("--state-root is required for --backend filesystem")
        run_meta, rows = load_from_filesystem(Path(args.state_root))
        history = load_history_from_filesystem(Path(args.state_root), limit=HISTORY_LIMIT)

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

    # Collect every unique Mathlib SHA referenced across all rows.
    sha_fields = ("target_commit", "last_known_good", "first_known_bad", "pinned_commit", "last_good_release_commit")
    unique_shas = {r[f] for r in rows for f in sha_fields if r.get(f)}

    # Collect pinned→last_good_release pairs for distance computation.
    lgr_pairs: set[tuple[str, str]] = set()
    for r in rows:
        pin = r.get("pinned_commit")
        lgr = r.get("last_good_release_commit")
        if pin and lgr and pin != lgr:
            lgr_pairs.add((pin, lgr))

    upstream_dir = Path(args.upstream_dir) if args.upstream_dir else None

    if upstream_dir is not None:
        # --- Local git path: all upstream data from the cloned repo ----------
        print(f"Reading upstream commit info from local clone at {upstream_dir}…")
        commit_titles = git_commit_info(upstream_dir, unique_shas)
        print(f"  {len(commit_titles)} commit(s) resolved.")

        print(f"Reading tags from local clone…")
        sha_to_tag = git_tag_map(upstream_dir)
        print(f"  {len(sha_to_tag)} tag(s) loaded.")

        lgr_distances: dict[tuple[str, str], int | None] = {}
        if lgr_pairs:
            print(f"Computing pinned→last-good-release distances for {len(lgr_pairs)} pair(s)…")
            lgr_distances = {
                (base, head): git_signed_distance(upstream_dir, base, head)
                for base, head in lgr_pairs
            }
    else:
        # --- GitHub API path (fallback when no local clone is available) -----
        print(f"Fetching commit titles for {len(unique_shas)} unique SHA(s)…")
        commit_titles = fetch_commit_titles(unique_shas, UPSTREAM_REPO, args.github_token)

        print(f"Fetching tags for {UPSTREAM_REPO}…")
        sha_to_tag = fetch_tags(UPSTREAM_REPO, args.github_token)
        print(f"  {len(sha_to_tag)} tag(s) loaded.")

        lgr_distances = {}
        if lgr_pairs:
            print(f"Fetching pinned→last-good-release distances for {len(lgr_pairs)} pair(s)…")
            lgr_distances = fetch_commit_distances(lgr_pairs, UPSTREAM_REPO, args.github_token)

    # Downstream commit titles always come from the API (we only clone the upstream).
    ds_by_repo: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("downstream_commit") and r.get("repo") and "/" in r.get("repo", ""):
            ds_by_repo[r["repo"]].add(r["downstream_commit"])
    downstream_commit_titles: dict[str, dict[str, str | None]] = {}
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
        sha_to_tag=sha_to_tag,
        lgr_distances=lgr_distances,
        history=history,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Site written to {out}")


if __name__ == "__main__":
    main()
