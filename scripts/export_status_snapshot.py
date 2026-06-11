#!/usr/bin/env python3
"""Stage downstream episode state as the status-snapshot artifact.

Runs once in the plan job of the regression and on-demand workflows.  Every
select leg's only database-derived input is the prior episode state — the same
``load_all_statuses`` query with the same answer for all ~30 legs — and a
burst of simultaneous connections on one cron tick is exactly what provokes
the Neon pooler's cold-start timeouts.  This script performs that read once
and writes the result as a single JSON file; the workflow uploads it as the
``status-snapshot`` artifact and each select leg reads it back with
``--status-snapshot <file>``, keeping database credentials and connections
out of the fan-out entirely.

In dry-run mode the workflows invoke this with ``--backend dry-run``, which
yields a snapshot with zero downstreams — the select legs then see the same
empty prior state a dry-run database read would have produced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.models import utc_now
from scripts.storage import add_backend_args, create_backend, write_status_snapshot


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the snapshot-staging step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow", default="regression")
    parser.add_argument("--upstream", default="leanprover-community/mathlib4")
    parser.add_argument(
        "--output", type=Path, required=True,
        help="File to write the status snapshot to.",
    )
    add_backend_args(parser)
    return parser


def main() -> int:
    """Load all statuses from the source backend and write the snapshot."""

    args = build_parser().parse_args()
    backend = create_backend(args.backend, dsn=args.dsn)
    statuses = backend.load_all_statuses(args.workflow, args.upstream)
    # Attach each downstream's most recent fresh-bisect time so the select
    # legs can apply the boundary-revalidation staleness valve without their
    # own database read.
    bisect_times = backend.load_last_fresh_bisect_times(args.workflow, args.upstream)
    for name, status in statuses.items():
        status.last_fresh_bisect_at = bisect_times.get(name)
    path = write_status_snapshot(
        args.output, statuses,
        workflow=args.workflow, upstream=args.upstream, reported_at=utc_now(),
    )
    print(f"[status-snapshot] wrote {len(statuses)} status record(s) to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
