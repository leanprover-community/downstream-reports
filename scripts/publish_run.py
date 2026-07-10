#!/usr/bin/env python3
"""Persist a staged run payload — the write half of the regression/on-demand report.

The report job computes the full set of run results and updated episode state
from real prior state, but it never writes them: that job runs on every branch
(including non-main) and must not mutate persisted state.  It serialises the
write via ``aggregate_results.py --persist-output`` into the ``persist-payload``
artifact.

This script is the only step of the environment-gated ``publish`` job.  It
reads the payload back and replays a single ``backend.save_run`` against the
write-capable database.  Two independent guards keep state off every branch:
the ``publish`` job declares ``environment: publish`` (restricted to ``main`` in
repository settings) and is skipped in dry-run mode, and the read-only DSN
handed to the report job cannot write even if this script were somehow invoked
there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.storage import add_backend_args, create_backend, read_run_payload


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the publish step."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--persist-input", type=Path, required=True,
        help="Persist payload staged by aggregate_results.py --persist-output.",
    )
    add_backend_args(parser)
    return parser


def main() -> int:
    """Replay the staged run payload as a single save_run against the backend."""

    args = build_parser().parse_args()
    payload = read_run_payload(args.persist_input)
    if not payload["results"]:
        print("[publish] payload has no result records — nothing to persist.")
        return 0
    backend = create_backend(args.backend, dsn=args.dsn)
    backend.save_run(**payload)
    print(
        f"[publish] persisted {len(payload['results'])} result record(s) "
        f"for run {payload['run_id']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
