#!/usr/bin/env python3
"""CLI entry point for the summary workflow.

Loads the latest per-downstream state from the database (the same data
source used by the status webpage) and sends a compact markdown summary
to a Zulip stream/topic.

Exits 0 in all cases — summary failures should not block CI.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from models import load_inventory
from notifications import (
    DryRunSender,
    ZulipSender,
    fetch_commit_titles,
    format_summary_message,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send a Zulip summary of the latest downstream state.",
    )
    parser.add_argument(
        "--inventory", type=Path, required=True,
        help="Path to ci/inventory/downstreams.json.",
    )
    parser.add_argument(
        "--stream", required=True,
        help="Zulip stream to post the summary to.",
    )
    parser.add_argument(
        "--topic", required=True,
        help="Zulip topic to post the summary to.",
    )
    parser.add_argument(
        "--backend", choices=["zulip", "dry-run"], default="dry-run",
        help="Message sender backend (default: dry-run).",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("POSTGRES_DSN"),
        help="Postgres DSN (default: $POSTGRES_DSN).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.dsn:
        print("No database DSN provided; cannot load state.", file=sys.stderr)
        return 0

    # Lazy import — avoid hard dependency on sqlalchemy when running tests
    # or in environments that only need the dry-run sender.
    from storage import latest_regression_run_id, load_run_for_site

    try:
        import sqlalchemy as sa
        engine = sa.create_engine(args.dsn)
    except Exception as exc:
        print(f"Failed to connect to database: {exc}", file=sys.stderr)
        return 0

    run_id = latest_regression_run_id(engine)
    if run_id is None:
        print("No regression runs found in the database.")
        return 0

    run_meta, rows = load_run_for_site(engine, run_id)

    # Filter to enabled downstreams from inventory.
    inventory = load_inventory(args.inventory)
    rows = [r for r in rows if r.get("downstream") in inventory]

    if not rows:
        print("No downstream results to report.")
        return 0

    bad_shas = [r["first_known_bad"] for r in rows if r.get("first_known_bad")]
    github_token = os.environ.get("GITHUB_TOKEN")
    titles = fetch_commit_titles(bad_shas, token=github_token) if bad_shas else {}

    message = format_summary_message(run_meta, rows, commit_titles=titles)

    if args.backend == "zulip":
        email = os.environ.get("ZULIP_EMAIL", "")
        api_key = os.environ.get("ZULIP_API_KEY", "")
        if not email or not api_key:
            print("ZULIP_EMAIL or ZULIP_API_KEY not set; falling back to dry-run.")
            sender = DryRunSender()
        else:
            sender = ZulipSender(email=email, api_key=api_key)
    else:
        sender = DryRunSender()

    try:
        sender.send_message(args.stream, args.topic, message)
        print(f"Summary sent to #{args.stream} > {args.topic}")
    except Exception as exc:
        print(f"Failed to send summary: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
