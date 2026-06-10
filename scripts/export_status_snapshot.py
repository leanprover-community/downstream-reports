#!/usr/bin/env python3
"""Stage downstream episode state as a filesystem-backend snapshot artifact.

Runs once in the plan job of the regression and on-demand workflows.  Every
select leg's only database read is ``load_all_statuses`` — the same query
with the same answer for all ~30 legs — and a burst of simultaneous
connections on one cron tick is exactly what provokes the Neon pooler's
cold-start timeouts.  This script performs that read once and writes the
result in the ``FilesystemBackend`` on-disk layout; the workflow uploads the
directory as the ``status-snapshot`` artifact and each select leg reads it
back with ``--backend filesystem --state-root <download dir>``, keeping
database credentials and connections out of the fan-out entirely.

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
        "--output-root", type=Path, required=True,
        help="Directory to populate as a filesystem-backend state root.",
    )
    add_backend_args(parser)
    return parser


def main() -> int:
    """Load all statuses from the source backend and write the snapshot."""

    args = build_parser().parse_args()
    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)
    statuses = backend.load_all_statuses(args.workflow, args.upstream)
    path = write_status_snapshot(
        args.output_root, args.workflow, statuses, reported_at=utc_now(),
    )
    print(f"[status-snapshot] wrote {len(statuses)} status record(s) to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
