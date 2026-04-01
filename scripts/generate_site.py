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
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UPSTREAM_REPO = "leanprover-community/mathlib4"
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
    html = f'<a href="{esc(url)}" class="sha" title="{tooltip}">{esc(s)}</a>'
    if title:
        html += f'&nbsp;<span class="commit-title">{esc(title)}</span>'
    return html


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
    maps to None so callers can fall back gracefully.  All unique SHAs are
    fetched exactly once (the dict IS the memo cache — pass the same dict
    across multiple calls to extend it without re-fetching).
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


def _gh_get(url: str, headers: dict[str, str]) -> dict | None:
    """Perform a single GitHub API GET and return the parsed JSON, or None on error."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  warning: GitHub API request failed ({url}): {exc}")
        return None


def fetch_commit_distances(
    pairs: set[tuple[str, str]],
    repo: str,
    token: str | None,
) -> dict[tuple[str, str], int | None]:
    """Return {(base_sha, head_sha): ahead_by} for every pair, using the GitHub compare API.

    `ahead_by` is the number of commits reachable from head but not from base —
    i.e. how many commits head is ahead of base.  Returns None for a pair when
    the API call fails.  Each unique pair is fetched exactly once.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hopscotch-reports/generate_site",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    cache: dict[tuple[str, str], int | None] = {}
    for base, head in sorted(pairs):
        if base == head:
            cache[(base, head)] = 0
            continue
        url = f"{GITHUB_API}/repos/{repo}/compare/{base}...{head}"
        data = _gh_get(url, headers)
        cache[(base, head)] = data.get("ahead_by") if data is not None else None
    return cache


OUTCOME_CLASS = {
    "passed": "badge-green",
    "failed": "badge-red",
    "error":  "badge-yellow",
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


def badge(value: str | None, cls_map: dict, label_map: dict | None = None) -> str:
    if not value:
        return "<span class='none'>—</span>"
    cls = cls_map.get(value, "badge-grey")
    label = (label_map or {}).get(value, value)
    return f'<span class="badge {esc(cls)}">{esc(label)}</span>'


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_from_sql(dsn: str, run_id: str) -> tuple[dict, list[dict]]:
    """Return (run_meta, rows) for the given run_id from a SQL database.

    Row keys use the SQL column names as-is:
      last_known_good, first_known_bad, target_commit, etc.
    Extra keys added: job_url, job_started_at, job_finished_at, job_conclusion.
    """
    import sqlalchemy as sa

    engine = sa.create_engine(dsn)
    with engine.connect() as conn:
        run_row = conn.execute(
            sa.text(
                "SELECT run_id, workflow, upstream_ref, run_url, started_at, reported_at "
                "FROM run WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).mappings().one()

        results = conn.execute(
            sa.text("SELECT * FROM run_result WHERE run_id = :run_id ORDER BY downstream"),
            {"run_id": run_id},
        ).mappings().all()

        jobs = conn.execute(
            sa.text(
                "SELECT downstream, job_id, job_url, started_at, finished_at, conclusion "
                "FROM validate_job WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        ).mappings().all()

    job_map = {j["downstream"]: dict(j) for j in jobs}

    rows = []
    for r in results:
        row = dict(r)
        job = job_map.get(r["downstream"], {})
        row["job_url"] = job.get("job_url")
        row["job_started_at"] = job.get("started_at")
        row["job_finished_at"] = job.get("finished_at")
        row["job_conclusion"] = job.get("conclusion")
        rows.append(row)

    return dict(run_row), rows


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
header {
  background: #24292e; color: #fff;
  padding: 14px 24px; display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
}
header h1 { margin: 0; font-size: 18px; font-weight: 600; }
header .gen-time { font-size: 12px; color: #959da5; margin-left: auto; }
main { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
.run-banner {
  background: #fff; border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 16px; margin-bottom: 16px;
  display: flex; gap: 20px; flex-wrap: wrap; align-items: center; font-size: 13px;
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
table {
  width: 100%; border-collapse: collapse;
  background: #fff; border: 1px solid var(--border); border-radius: 6px; overflow: hidden;
}
th {
  background: #f1f3f5; border-bottom: 1px solid var(--border);
  padding: 8px 12px; text-align: left; font-size: 11px;
  font-weight: 600; text-transform: uppercase; letter-spacing: .05em; color: var(--grey);
}
td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tr:last-child td { border-bottom: none; }
tbody tr.data-row:hover td { background: #f6f8fa; }
.name { font-weight: 600; font-size: 13px; }
.name a { color: #24292e; text-decoration: none; }
.name a:hover { color: #0366d6; }
.repo-label { font-size: 11px; color: var(--grey); }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: 11px; font-weight: 600; white-space: nowrap;
}
.badge-green  { background: #dcffe4; color: var(--green); }
.badge-red    { background: #ffdce0; color: var(--red); }
.badge-yellow { background: #fff8c5; color: var(--yellow); }
.badge-grey   { background: #f1f3f5; color: var(--grey); }
.sha {
  font-family: "SFMono-Regular", Consolas, monospace; font-size: 12px;
  color: #0366d6; text-decoration: none;
  background: #f1f3f5; padding: 1px 4px; border-radius: 3px;
}
.sha:hover { background: #e1e4e8; }
.commit-title { font-size: 12px; color: var(--grey); }
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


def render(
    *,
    run_id: str,
    run_url: str,
    upstream_ref: str,
    reported_at: str | None,
    generated_at: str,
    rows: list[dict],
    commit_titles: dict[str, str | None],
    commit_distances: dict[tuple[str, str], int | None],
) -> str:
    n_passed = sum(1 for r in rows if r.get("outcome") == "passed")
    n_failed = sum(1 for r in rows if r.get("outcome") == "failed")
    n_error  = sum(1 for r in rows if r.get("outcome") == "error")

    stats_html = (
        f'<div class="stats">'
        f'<div class="stat green"><div class="n">{n_passed}</div><div class="l">passing</div></div>'
        f'<div class="stat red"><div class="n">{n_failed}</div><div class="l">failing</div></div>'
        f'<div class="stat yellow"><div class="n">{n_error}</div><div class="l">errors</div></div>'
        f'</div>'
    )

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

    run_banner = (
        f'<div class="run-banner">'
        f'<span><strong>Upstream ref:</strong>&nbsp;<code>{esc(upstream_ref)}</code></span>'
        f'<span class="divider">|</span>'
        f'<span><strong>Run:</strong>&nbsp;<a href="{esc(run_url)}">{esc(run_id)}</a></span>'
        f'<span class="divider">|</span>'
        f'<span><strong>Reported:</strong>&nbsp;{fmt_dt(reported_at)}</span>'
        f'{target_banner}'
        f'<span style="margin-left:auto;color:var(--grey);font-size:12px;">Generated&nbsp;{esc(generated_at)}</span>'
        f'</div>'
    )

    # Sort: failing first, then errors, then passing; alpha within each group
    def sort_key(r: dict) -> tuple:
        order = {"failed": 0, "error": 1, "passed": 2}.get(r.get("outcome", ""), 3)
        return (order, r.get("downstream", "").lower())

    table_rows: list[str] = []
    for r in sorted(rows, key=sort_key):
        downstream = r.get("downstream", "")
        repo = r.get("repo", "")
        repo_url = f"{GITHUB}/{repo}" if repo and "/" in repo else None

        name_cell = f'<div class="name">'
        if repo_url:
            name_cell += f'<a href="{esc(repo_url)}" title="{esc(repo)}">{esc(downstream)}</a>'
        else:
            name_cell += esc(downstream)
        name_cell += "</div>"
        if repo:
            name_cell += f'<div class="repo-label">{esc(repo)}</div>'

        def ct(sha: str | None) -> str | None:
            return commit_titles.get(sha) if sha else None

        def dist(base: str | None, head: str | None) -> int | None:
            if not base or not head:
                return None
            return commit_distances.get((base, head))

        pin = r.get("pinned_commit")
        target = r.get("target_commit")
        lkg = r.get("last_known_good")

        outcome_cell  = badge(r.get("outcome"), OUTCOME_CLASS)
        episode_cell  = badge(r.get("episode_state"), EPISODE_CLASS, EPISODE_LABEL)
        target_cell   = commit_link(UPSTREAM_REPO, target, ct(target)) if show_target_column else None
        lkg_cell      = commit_link(UPSTREAM_REPO, lkg,    ct(lkg))
        fkb_cell      = commit_link(UPSTREAM_REPO, r.get("first_known_bad"), ct(r.get("first_known_bad")))
        pin_cell      = commit_link(UPSTREAM_REPO, pin,    ct(pin))
        age_cell      = distance_cell(dist(pin, target))
        bump_cell     = distance_cell(dist(pin, lkg))

        btns: list[str] = []
        if r.get("job_url"):
            btns.append(f'<a href="{esc(r["job_url"])}" class="btn">CI job&nbsp;↗</a>')
        if run_url:
            btns.append(f'<a href="{esc(run_url)}" class="btn">Run&nbsp;↗</a>')
        links_cell = f'<div class="links">{"".join(btns)}</div>'

        cells = (
            f"<td>{name_cell}</td>"
            f"<td>{outcome_cell}</td>"
            f"<td>{episode_cell}</td>"
            + (f"<td>{target_cell}</td>" if show_target_column else "")
            + f"<td>{lkg_cell}</td>"
            f"<td>{fkb_cell}</td>"
            f"<td>{pin_cell}</td>"
            f"<td>{age_cell}</td>"
            f"<td>{bump_cell}</td>"
            f"<td>{links_cell}</td>"
        )
        table_rows.append(
            f'<tr class="data-row" data-downstream="{esc(downstream.lower())}">{cells}</tr>'
        )


    n_cols = 10 if show_target_column else 9
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
<header>
  <h1>Mathlib Downstream Status</h1>
  <span class="gen-time">Generated {esc(generated_at)}</span>
</header>
<main>
  {run_banner}
  {stats_html}
  <div class="filter-bar">
    <input id="filter" type="search" placeholder="Filter downstreams…" aria-label="Filter downstreams">
  </div>
  <table>
    <thead>
      <tr>
        <th>Downstream</th>
        <th>Outcome</th>
        <th>Episode</th>
        {"<th>Target</th>" if show_target_column else ""}
        <th>Last known good</th>
        <th>First known bad</th>
        <th>Pinned</th>
        <th title="Commits between pin and Mathlib target">Age</th>
        <th title="Commits the pin can safely advance (pin → last known good)">Bump</th>
        <th>Links</th>
      </tr>
    </thead>
    <tbody>
{tbody}
    </tbody>
  </table>
</main>
<footer>
  Generated {esc(generated_at)}&nbsp;&middot;&nbsp;<a href="{esc(run_url)}">Workflow run {esc(run_id)}</a>
</footer>
<script>
  const input = document.getElementById('filter');
  input.addEventListener('input', () => {{
    const q = input.value.toLowerCase();
    document.querySelectorAll('tr.data-row').forEach(row => {{
      const match = !q || row.dataset.downstream.includes(q);
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

        import sqlalchemy as sa

        engine = sa.create_engine(dsn)

        run_id = args.run_id
        if not run_id:
            with engine.connect() as conn:
                run_id = conn.execute(
                    sa.text(
                        "SELECT run_id FROM run WHERE workflow = 'regression' "
                        "ORDER BY reported_at DESC LIMIT 1"
                    )
                ).scalar()
            if not run_id:
                ap.error("No regression runs found in the database.")

        run_meta, rows = load_from_sql(dsn, run_id)

    else:  # filesystem
        if not args.state_root:
            ap.error("--state-root is required for --backend filesystem")
        run_meta, rows = load_from_filesystem(Path(args.state_root))

    run_id = run_meta.get("run_id", "unknown")
    run_url = run_meta.get("run_url", "") or ""
    upstream_ref = run_meta.get("upstream_ref", "unknown") or "unknown"
    reported_at = str(run_meta.get("reported_at") or "") or None

    # Collect every unique Mathlib SHA referenced across all rows, then fetch
    # their commit titles in one pass (memoized — each SHA fetched at most once).
    sha_fields = ("target_commit", "last_known_good", "first_known_bad", "pinned_commit")
    unique_shas = {r[f] for r in rows for f in sha_fields if r.get(f)}
    print(f"Fetching commit titles for {len(unique_shas)} unique SHA(s)…")
    commit_titles = fetch_commit_titles(unique_shas, UPSTREAM_REPO, args.github_token)

    # Collect (base, head) pairs needed for age and bump, then fetch distances.
    compare_pairs: set[tuple[str, str]] = set()
    for r in rows:
        pin = r.get("pinned_commit")
        target = r.get("target_commit")
        lkg = r.get("last_known_good")
        if pin and target and pin != target:
            compare_pairs.add((pin, target))
        if pin and lkg and pin != lkg:
            compare_pairs.add((pin, lkg))
    print(f"Fetching commit distances for {len(compare_pairs)} unique pair(s)…")
    commit_distances = fetch_commit_distances(compare_pairs, UPSTREAM_REPO, args.github_token)

    html = render(
        run_id=run_id,
        run_url=run_url,
        upstream_ref=upstream_ref,
        reported_at=reported_at,
        generated_at=generated_at,
        rows=rows,
        commit_titles=commit_titles,
        commit_distances=commit_distances,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Site written to {out}")


if __name__ == "__main__":
    main()
