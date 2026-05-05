#!/usr/bin/env python3
"""Record SHAs the cache-warming workflow confirmed warm into ``cache_warmth``.

Reads the summary JSON produced by ``warm-mathlib-cache.yml``'s ``finalize``
job and upserts every entry whose ``status`` is in ``WARM_STATUSES`` so that
future ``plan_cache_warm_jobs.py`` invocations skip them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.storage import add_backend_args, create_backend

WARM_STATUSES = frozenset({"already_warm", "warmed"})


def collect_warm_shas(summary: list[dict]) -> list[str]:
    """Return the deduplicated SHAs whose status is terminal-warm."""
    seen: set[str] = set()
    out: list[str] = []
    for entry in summary:
        sha = entry.get("sha")
        if not sha or entry.get("status") not in WARM_STATUSES:
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

    warm_shas = collect_warm_shas(summary)
    if not warm_shas:
        print("No warm SHAs to record.", file=sys.stderr)
        return 0

    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)
    backend.record_warm_shas(args.upstream, warm_shas)
    print(
        f"Recorded {len(warm_shas)} warm SHA(s) for {args.upstream}: "
        f"{', '.join(sha[:7] for sha in warm_shas)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
