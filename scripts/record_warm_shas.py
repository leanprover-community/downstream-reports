#!/usr/bin/env python3
"""Record terminal-status SHAs from the cache-warming workflow into ``cache_warmth``.

Reads the summary JSON produced by ``warm-mathlib-cache.yml``'s ``finalize``
job and upserts every entry whose ``status`` is in ``TERMINAL_STATUSES`` so that
future ``plan_cache_warm_jobs.py`` invocations skip them.

A *terminal* status is one where we don't want the next planning tick to
re-attempt the SHA: either we succeeded (``already_warm`` / ``warmed``)
or we tried twice in-job and gave up (``build_failed`` / ``push_failed`` /
``verify_failed``).  ``no_result`` (runner died, no signal) is excluded so
those SHAs get a fresh attempt next tick.
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


def collect_terminal_shas(summary: list[dict]) -> list[str]:
    """Return the deduplicated SHAs whose status is in ``TERMINAL_STATUSES``.

    Terminal statuses include both the success path (``already_warm``,
    ``warmed``) and the post-retry give-up path (``build_failed``,
    ``push_failed``, ``verify_failed``).  Recording both classes makes
    the warm-cache workflow best-effort: a SHA that fails build/push/
    verify twice gets entered into ``cache_warmth`` and will not be
    re-attempted on the next 6h tick.

    Non-terminal statuses (``no_result``, intermediate states like
    ``staged``) are excluded.  ``no_result`` in particular MUST be
    retried — it means the runner died mid-flight and we have no
    information about the SHA, distinct from "we tried and gave up."

    Entries whose ``sha`` field is missing, ``None``, or empty are
    silently skipped.  This is defensive against malformed summary
    rows: the schema *should* always carry a SHA, but if the
    ``warm-mathlib-cache.yml`` shell-level summary builder ever emits
    a bad row we prefer to skip it rather than crash the
    ``finalize`` job and lose the rest of the recording.

    Dedup is "first occurrence wins" — preserving input order keeps
    the recorded list deterministic for log readability.

    Pinned end-to-end by ``test_record_warm_shas.py``.
    """
    seen: set[str] = set()
    out: list[str] = []
    for entry in summary:
        sha = entry.get("sha")
        if not sha or entry.get("status") not in TERMINAL_STATUSES:
            continue
        if sha in seen:
            continue
        seen.add(sha)
        out.append(sha)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record warm SHAs from a cache-warming summary into the database."
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

    terminal_shas = collect_terminal_shas(summary)
    if not terminal_shas:
        print("No terminal SHAs to record.", file=sys.stderr)
        return 0

    backend = create_backend(args.backend, dsn=args.dsn)
    backend.record_warm_shas(args.upstream, terminal_shas)
    print(
        f"Recorded {len(terminal_shas)} terminal SHA(s) for {args.upstream}: "
        f"{', '.join(sha[:7] for sha in terminal_shas)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
