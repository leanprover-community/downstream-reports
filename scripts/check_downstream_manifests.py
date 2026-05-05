#!/usr/bin/env python3
"""Detect downstream `lake update`s and dispatch targeted regression runs.

This script runs every 15 minutes from ``.github/workflows/manifest-watcher.yml``.
For each enabled downstream it cheaply checks whether ``lake-manifest.json``
on the default branch now points at a different mathlib SHA than the one we
last validated *and* whether that new pin lands at or past the downstream's
``first_known_bad_commit`` — i.e. the downstream is actively bumping into a
known regression and we want to find the new breaking point promptly rather
than waiting for the 12h scheduled run.

The check is layered to short-circuit as early as possible:

1.  ``GET /repos/{repo}/git/ref/heads/{branch}`` — one tiny call (singular
    ``ref`` endpoint, returns a single object); if the branch tip is unchanged
    from the recorded ``downstream_commit`` the manifest cannot have moved.
2.  ``GET https://raw.githubusercontent.com/{repo}/{sha}/lake-manifest.json``
    — fetch only when the branch moved.
3.  ``GET /repos/{upstream}/compare/{fkb}...{current_pin}`` — only when the
    pin actually changed; ``"behind"`` and ``"diverged"`` outcomes mean the
    downstream is bumping safely within the LKG range and we let the next
    scheduled run pick it up instead of flooding the queue.  The triple-dot
    form is intentional: it computes the merge-base diff, which on mathlib's
    straight-line ``master`` is equivalent to two-dot but degrades more
    safely (returning ``"diverged"`` rather than an error) if a downstream
    ever pins to a non-master branch.

Three independent dedup mechanisms guard against re-dispatching the same
``(downstream, pin)``:

*   The watcher's own ``manifest_watcher_ledger`` table, with a 4h TTL fallback
    (configurable via ``--ledger-ttl-hours``) in case a dispatch is silently
    cancelled or fails partway through.
*   The set of downstreams whose ``select: <name>`` / ``probe: <name>`` jobs
    are currently in-flight in any queued or in-progress regression-report
    run.  ``downstream_status.pinned_commit`` is only written when the report
    workflow's final ``report`` job runs, so during a 2h scheduled run the DB
    state lags behind reality; this check closes that hole.
*   The branch-head short-circuit (step 1) means an unchanged downstream
    isn't even compared in the first place.

Reads ``GITHUB_TOKEN`` (or ``GH_TOKEN``) and, when ``--backend sql``,
``POSTGRES_DSN`` from the environment.  Never invokes hopscotch and never
writes ``downstream_status`` or ``run_result`` — only reads them.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.git_ops import pinned_from_manifest_payload
from scripts.models import DownstreamConfig, load_inventory, utc_now
from scripts.storage import (
    DownstreamStatusRecord,
    ManifestWatcherLedgerRow,
    StorageBackend,
    add_backend_args,
    create_backend,
)


_GITHUB_API = "https://api.github.com"
_RAW_BASE = "https://raw.githubusercontent.com"
_JOB_NAME_RE = re.compile(r"^(?:select|probe): (.+)$")


# ---------------------------------------------------------------------------
# HTTP helpers (thin wrappers around urllib so tests can monkeypatch)
# ---------------------------------------------------------------------------


def _gh_request(method: str, path_or_url: str, token: str, *, body: bytes | None = None) -> tuple[int, bytes]:
    """Issue a GitHub API request and return ``(status, body_bytes)``.

    Accepts both bare API paths ("repos/..") and full URLs (used for
    raw.githubusercontent.com fetches).  Returns the response body even on
    HTTP errors so callers can decide how to react (404 is normal for the
    "manifest does not exist" case).
    """

    url = path_or_url if "://" in path_or_url else f"{_GITHUB_API}/{path_or_url.lstrip('/')}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "downstream-reports/manifest-watcher",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def gh_get_json(path: str, token: str) -> Any | None:
    """GET an API path and return parsed JSON, or None on non-2xx."""

    status, body = _gh_request("GET", path, token)
    if status >= 300:
        print(f"[watcher] HTTP {status} for GET {path}: {body[:200].decode(errors='replace')}", file=sys.stderr)
        return None
    return json.loads(body)


def gh_get_branch_head(repo: str, branch: str, token: str) -> str | None:
    """Return the HEAD SHA for ``{repo}@{branch}`` or None on lookup failure."""

    payload = gh_get_json(f"repos/{repo}/git/ref/heads/{branch}", token)
    if not isinstance(payload, dict):
        return None
    obj = payload.get("object")
    if not isinstance(obj, dict):
        return None
    sha = obj.get("sha")
    return sha if isinstance(sha, str) else None


def gh_get_raw_manifest(repo: str, sha: str, token: str) -> Any | None:
    """Fetch ``lake-manifest.json`` at ``{repo}@{sha}`` and return the parsed JSON.

    Returns None when the file is absent (404), unparseable, or the request
    failed.  raw.githubusercontent.com accepts the GH App token for higher
    rate limits even on public repos.
    """

    url = f"{_RAW_BASE}/{repo}/{sha}/lake-manifest.json"
    status, body = _gh_request("GET", url, token)
    if status >= 300:
        print(f"[watcher] HTTP {status} for {url}", file=sys.stderr)
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def gh_compare_status(upstream_repo: str, base: str, head: str, token: str) -> str | None:
    """Return the ``status`` field of the GitHub compare API or None on error.

    Possible values: ``"ahead"``, ``"behind"``, ``"identical"``, ``"diverged"``.
    """

    payload = gh_get_json(f"repos/{upstream_repo}/compare/{base}...{head}", token)
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    return status if isinstance(status, str) else None


def gh_in_flight_downstreams(repo: str, workflow_file: str, token: str) -> set[str]:
    """Return the set of downstream names with a live ``select:``/``probe:`` job.

    Looks at queued and in-progress runs of *workflow_file* and unions the
    matrix-job names, stripping the ``select:`` / ``probe:`` prefix so the
    result is just the bare downstream name.

    First-page-only by design: with ~50 downstreams and `cancel-in-progress: false`
    we expect 0–2 runs in flight, so 50 runs / 100 jobs cover normal operation
    several times over.  If a backlog builds up (e.g. after an Actions outage)
    and total_count exceeds the page size we log a warning — the worst-case
    failure mode is a false-negative on the in-flight check, which the ledger
    catches on the next 15-minute tick.  Following Link headers would be the
    fix if this becomes a real problem.
    """

    _RUN_PAGE_SIZE = 50
    _JOB_PAGE_SIZE = 100
    found: set[str] = set()
    for status in ("in_progress", "queued"):
        runs_payload = gh_get_json(
            f"repos/{repo}/actions/workflows/{workflow_file}/runs"
            f"?status={status}&per_page={_RUN_PAGE_SIZE}",
            token,
        )
        if not isinstance(runs_payload, dict):
            continue
        runs = runs_payload.get("workflow_runs", []) or []
        total = runs_payload.get("total_count")
        if isinstance(total, int) and total > len(runs):
            print(
                f"[watcher] WARNING: {total} {status} runs of {workflow_file} but only "
                f"the first {len(runs)} were inspected; in-flight set may be incomplete",
                file=sys.stderr,
            )
        for run in runs:
            run_id = run.get("id")
            if run_id is None:
                continue
            jobs_payload = gh_get_json(
                f"repos/{repo}/actions/runs/{run_id}/jobs?per_page={_JOB_PAGE_SIZE}",
                token,
            )
            if not isinstance(jobs_payload, dict):
                continue
            jobs = jobs_payload.get("jobs", []) or []
            jobs_total = jobs_payload.get("total_count")
            if isinstance(jobs_total, int) and jobs_total > len(jobs):
                # 50 downstreams × (select + probe) + plan/report/alert ≈ 103 — already
                # close to the 100-per-page max.  Warn rather than silently miss names.
                print(
                    f"[watcher] WARNING: run {run_id} has {jobs_total} jobs but only "
                    f"the first {len(jobs)} were inspected",
                    file=sys.stderr,
                )
            for job in jobs:
                m = _JOB_NAME_RE.match(job.get("name", ""))
                if m:
                    found.add(m.group(1))
    return found


def gh_dispatch_workflow(
    repo: str, workflow_file: str, ref: str, inputs: dict[str, Any], token: str
) -> bool:
    """POST a ``workflow_dispatch`` and return True on HTTP 204."""

    body = json.dumps({"ref": ref, "inputs": inputs}).encode()
    status, payload = _gh_request(
        "POST",
        f"repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        token,
        body=body,
    )
    if status == 204:
        return True
    print(
        f"[watcher] dispatch failed: HTTP {status}: {payload[:300].decode(errors='replace')}",
        file=sys.stderr,
    )
    return False


# ---------------------------------------------------------------------------
# Candidate building
# ---------------------------------------------------------------------------


@dataclass
class Candidate:
    """One downstream that survived every skip filter and should be dispatched."""

    name: str
    branch_head: str
    current_pin: str
    fkb: str


def _short(sha: str | None) -> str:
    return sha[:12] if sha else "(none)"


def _ledger_is_fresh(
    row: ManifestWatcherLedgerRow | None, current_pin: str, ttl: timedelta, now: datetime
) -> bool:
    if row is None:
        return False
    if row.observed_pin != current_pin:
        return False
    try:
        dispatched_at = datetime.fromisoformat(row.dispatched_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - dispatched_at) < ttl


def evaluate_downstream(
    config: DownstreamConfig,
    *,
    upstream_repo: str,
    status: DownstreamStatusRecord | None,
    ledger_row: ManifestWatcherLedgerRow | None,
    in_flight: set[str],
    ttl: timedelta,
    now: datetime,
    fetch_branch_head: Callable[[str, str], str | None],
    fetch_manifest: Callable[[str, str], Any | None],
    fetch_compare: Callable[[str, str, str], str | None],
) -> Candidate | None:
    """Decide whether *config* should be dispatched. Returns a Candidate or None.

    Logs the reason for every skip so the watcher's run summary is debuggable.
    Pure decision function; all I/O is injected so tests can drive it directly.
    """

    name = config.name
    prev_downstream_commit = status.downstream_commit if status else None
    prev_pinned = status.pinned_commit if status else None
    fkb = status.first_known_bad_commit if status else None

    branch_head = fetch_branch_head(config.repo, config.default_branch)
    if branch_head is None:
        print(f"[watcher] [skip] {name}: could not resolve branch head")
        return None

    if prev_downstream_commit and branch_head == prev_downstream_commit:
        # Branch tip unchanged ⇒ manifest cannot have moved.  No further calls.
        return None

    manifest_payload = fetch_manifest(config.repo, branch_head)
    current_pin = pinned_from_manifest_payload(manifest_payload, config.dependency_name)
    if current_pin is None:
        print(f"[watcher] [skip] {name}: no '{config.dependency_name}' entry in manifest at {_short(branch_head)}")
        return None

    if prev_pinned and current_pin == prev_pinned:
        return None

    if fkb is None:
        print(
            f"[watcher] [skip] {name}: pin moved {_short(prev_pinned)}→{_short(current_pin)} "
            "but no active regression (FKB unset); leaving good bumps to the scheduled run"
        )
        return None

    compare = fetch_compare(upstream_repo, fkb, current_pin)
    if compare is None:
        print(f"[watcher] [skip] {name}: compare API failed for FKB {_short(fkb)}...{_short(current_pin)}")
        return None
    if compare in ("behind", "diverged"):
        print(
            f"[watcher] [skip] {name}: pin {_short(current_pin)} is {compare} of FKB "
            f"{_short(fkb)}; safe bump or diverged history"
        )
        return None
    # "ahead" and "identical" both fall through: downstream has reached or
    # passed the breaking point and is worth re-validating.

    if name in in_flight:
        print(f"[watcher] [skip] {name}: live report run already validating it")
        return None

    if _ledger_is_fresh(ledger_row, current_pin, ttl, now):
        print(f"[watcher] [skip] {name}: dispatch already in flight for pin {_short(current_pin)}")
        return None

    print(
        f"[watcher] [dispatch] {name}: bumped past FKB {_short(fkb)} "
        f"from {_short(prev_pinned)} to {_short(current_pin)} (compare={compare})"
    )
    return Candidate(name=name, branch_head=branch_head, current_pin=current_pin, fkb=fkb)


def build_candidates(
    inventory: dict[str, DownstreamConfig],
    statuses: dict[str, DownstreamStatusRecord],
    ledger: dict[str, ManifestWatcherLedgerRow],
    in_flight: set[str],
    *,
    upstream_repo: str,
    ttl: timedelta,
    now: datetime,
    fetch_branch_head: Callable[[str, str], str | None],
    fetch_manifest: Callable[[str, str], Any | None],
    fetch_compare: Callable[[str, str, str], str | None],
    max_workers: int = 16,
) -> list[Candidate]:
    """Run :func:`evaluate_downstream` over the candidate inventory in parallel.

    Two pre-filters short-circuit before any HTTP call:

    1.  **Opt-in.** Only ``enabled=True`` + ``watch_manifest=True`` entries
        are considered — the watcher is a hot path (50 entries × 4 ticks/h)
        so projects that don't actively bump-track don't pay the cost.
    2.  **Active regression.** Skip entries whose ``first_known_bad_commit``
        is unset.  The watcher's whole purpose is detecting bumps *past*
        the known breaking point; with no FKB there's nothing for it to
        catch early, and the next 12h scheduled run will pick up any pin
        change.  This filter also lives inside :func:`evaluate_downstream`
        as a defensive contract — moving it here just avoids the API
        round-trip in the common steady-state case.
    """

    enabled = [
        c
        for c in inventory.values()
        if c.enabled
        and c.watch_manifest
        and (statuses.get(c.name) is not None
             and statuses[c.name].first_known_bad_commit is not None)
    ]

    def _eval(config: DownstreamConfig) -> Candidate | None:
        return evaluate_downstream(
            config,
            upstream_repo=upstream_repo,
            status=statuses.get(config.name),
            ledger_row=ledger.get(config.name),
            in_flight=in_flight,
            ttl=ttl,
            now=now,
            fetch_branch_head=fetch_branch_head,
            fetch_manifest=fetch_manifest,
            fetch_compare=fetch_compare,
        )

    candidates: list[Candidate] = []
    if not enabled:
        return candidates
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for result in pool.map(_eval, enabled):
            if result is not None:
                candidates.append(result)
    # Stable order so logs / dispatch payloads are deterministic.
    candidates.sort(key=lambda c: c.name)
    return candidates


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect downstream lake-manifest pin changes and dispatch report runs."
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--upstream",
        default="leanprover-community/mathlib4",
        help="`owner/repo` for the upstream project (used by the compare API).",
    )
    parser.add_argument(
        "--workflow-file",
        default="mathlib-downstream-report.yml",
        help="Workflow file name to dispatch and to scan for in-flight runs.",
    )
    parser.add_argument(
        "--ref",
        default="main",
        help="Ref to dispatch the workflow on.",
    )
    parser.add_argument(
        "--ledger-ttl-hours",
        type=float,
        default=4.0,
        help="Ledger TTL in hours; rows older than this are ignored for dedup.",
    )
    parser.add_argument(
        "--dry-run-dispatch",
        action="store_true",
        help="Build candidates and log them, but do not dispatch or write the ledger.",
    )
    add_backend_args(parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required")

    gh_repo = os.environ.get("GH_REPO")
    if not gh_repo:
        raise SystemExit("GH_REPO environment variable is required (e.g. 'leanprover-community/downstream-reports')")

    backend: StorageBackend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)
    inventory = load_inventory(args.inventory)
    statuses = backend.load_all_statuses("regression", args.upstream)
    ledger = backend.load_manifest_watcher_ledger(args.upstream)

    watched = sum(1 for c in inventory.values() if c.enabled and c.watch_manifest)
    active = sum(
        1
        for c in inventory.values()
        if c.enabled and c.watch_manifest
        and (statuses.get(c.name) is not None
             and statuses[c.name].first_known_bad_commit is not None)
    )
    print(
        f"[watcher] inventory: {active} active (FKB set) / {watched} watched / "
        f"{len(inventory)} enabled downstream(s)"
    )
    print(f"[watcher] statuses : {len(statuses)} record(s) loaded")
    print(f"[watcher] ledger   : {len(ledger)} row(s) loaded")

    in_flight = gh_in_flight_downstreams(gh_repo, args.workflow_file, token)
    if in_flight:
        print(f"[watcher] in-flight: {sorted(in_flight)}")
    else:
        print("[watcher] in-flight: (none)")

    candidates = build_candidates(
        inventory,
        statuses,
        ledger,
        in_flight,
        upstream_repo=args.upstream,
        ttl=timedelta(hours=args.ledger_ttl_hours),
        now=datetime.now(timezone.utc),
        fetch_branch_head=lambda repo, branch: gh_get_branch_head(repo, branch, token),
        fetch_manifest=lambda repo, sha: gh_get_raw_manifest(repo, sha, token),
        fetch_compare=lambda upstream, base, head: gh_compare_status(upstream, base, head, token),
    )

    if not candidates:
        print("[watcher] no dispatch needed")
        return 0

    csv = ",".join(c.name for c in candidates)
    print(f"[watcher] dispatching {len(candidates)} downstream(s): {csv}")

    if args.dry_run_dispatch:
        print("[watcher] --dry-run-dispatch set; not calling the API")
        return 0

    if not gh_dispatch_workflow(gh_repo, args.workflow_file, args.ref, {"downstream": csv}, token):
        return 1

    timestamp = utc_now()
    backend.upsert_manifest_watcher_ledger(
        args.upstream,
        [
            ManifestWatcherLedgerRow(
                downstream=c.name,
                observed_pin=c.current_pin,
                dispatched_at=timestamp,
                run_url=None,  # GH does not return the run id from a dispatch call
            )
            for c in candidates
        ],
    )
    print(f"[watcher] ledger updated for {len(candidates)} downstream(s) at {timestamp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
