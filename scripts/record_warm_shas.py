#!/usr/bin/env python3
"""Record terminal warming statuses from the cache-warming workflow into ``cache_warmth``.

Reads the summary JSON produced by ``warm-mathlib-cache.yml``'s ``finalize``
job and upserts every entry whose ``status`` is in ``TERMINAL_STATUSES``,
so that future ``plan_cache_warm_jobs.py`` invocations can skip verified-warm
SHAs and schedule backoff retries for failed ones.

A *terminal* status is the outcome of a warming attempt that ran this
tick: the job verified the cache (``already_warm`` / ``warmed``), or the
job tried twice and stopped (``build_failed`` / ``push_failed`` /
``verify_failed``). Mathlib master always builds, so the failure statuses
are infrastructure trouble, not properties of the SHA; the planner
re-attempts them with backoff until its retry budget runs out.
``no_result`` (runner died, no signal) is excluded, so those SHAs get a
fresh attempt next tick without consuming retry budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.storage import add_backend_args, create_backend

TERMINAL_STATUSES = frozenset({
    "already_warm",
    "warmed",
    "build_failed",
    "push_failed",
    "verify_failed",
})


def collect_terminal_results(summary: list[dict]) -> dict[str, str]:
    """Return ``{sha: status}`` for entries whose status is in ``TERMINAL_STATUSES``.

    Terminal statuses include both the verified-warm path (``already_warm``,
    ``warmed``) and the failed-attempt path (``build_failed``,
    ``push_failed``, ``verify_failed``). Each recorded row carries its
    status verbatim and increments the SHA's attempt counter, which
    drives the planner's backoff retry schedule.

    Non-terminal statuses are excluded. ``no_result`` in particular MUST
    be dropped: it means the runner died and the SHA's state is unknown,
    distinct from a completed attempt that failed. The planner's own
    skip statuses (``cache_warmth_hit``, ``retry_backoff``,
    ``retry_exhausted``) appear in the summary for reporting but
    describe SHAs that did NOT run this tick; recording them would
    inflate attempt counters for attempts that never happened.

    Entries whose ``sha`` field is missing, ``None``, or empty are
    silently skipped.  This is defensive against malformed summary
    rows: a bad row from the ``warm-mathlib-cache.yml`` shell-level
    summary builder is skipped, so the ``finalize`` job records the
    rest instead of crashing.

    Dedup is "first occurrence wins": preserved input order keeps
    the recorded mapping deterministic for log readability.

    Pinned end-to-end by ``test_record_warm_shas.py``.
    """
    out: dict[str, str] = {}
    for entry in summary:
        sha = entry.get("sha")
        status = entry.get("status")
        if not sha or status not in TERMINAL_STATUSES:
            continue
        if sha in out:
            continue
        out[sha] = status
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record warming statuses from a cache-warming summary into the database."
    )
    add_backend_args(parser)
    parser.add_argument(
        "--upstream",
        default="leanprover-community/mathlib4",
        help="Upstream repository slug (default: leanprover-community/mathlib4).",
    )
    parser.add_argument(
        "--summary",
        required=True,
        help="Path to summary.json emitted by warm-mathlib-cache.yml's finalize job.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    summary_path = Path(args.summary)
    if not summary_path.exists():
        print(f"No summary at {summary_path}; nothing to record.", file=sys.stderr)
        return 0

    summary = json.loads(summary_path.read_text())
    if not isinstance(summary, list):
        raise SystemExit(f"Expected a JSON list at {summary_path}, got {type(summary).__name__}")

    results = collect_terminal_results(summary)
    if not results:
        print("No terminal statuses to record.", file=sys.stderr)
        return 0

    backend = create_backend(args.backend, dsn=args.dsn)
    backend.record_warmth_results(args.upstream, results)
    print(
        f"Recorded {len(results)} terminal status(es) for {args.upstream}: "
        + ", ".join(f"{sha[:7]}={status}" for sha, status in results.items()),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
