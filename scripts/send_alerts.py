#!/usr/bin/env python3
"""CLI entry point for the alert job.

Reads the alert payload written by ``aggregate_results.py --alert-output``,
decides which downstreams need a Zulip notification (new regression or
recovery), and sends the messages.

Exits 0 in all cases — alert failures should not block the CI workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from notifications import (
    DryRunSender,
    ZulipSender,
    compute_alert_actions,
    execute_alerts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send Zulip alerts for downstream status changes.",
    )
    parser.add_argument(
        "--alert-payload", type=Path, required=True,
        help="Path to the alert-payload.json written by aggregate_results.py.",
    )
    parser.add_argument(
        "--run-url", required=True,
        help="URL of the GitHub Actions run (included in alert messages).",
    )
    parser.add_argument(
        "--stream", default="Hopscotch",
        help="Zulip stream to post alerts to (default: Hopscotch).",
    )
    parser.add_argument(
        "--topic", default="Downstream alerts",
        help="Zulip topic to post alerts to (default: Downstream alerts).",
    )
    parser.add_argument(
        "--backend", choices=["zulip", "dry-run"], default="dry-run",
        help="Message sender backend (default: dry-run).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.alert_payload.exists():
        print("Alert payload not found; skipping alerts.")
        return 0

    raw = args.alert_payload.read_text().strip()
    if not raw:
        print("Alert payload is empty; skipping alerts.")
        return 0

    payload = json.loads(raw)
    records = payload.get("results", [])
    run_url = payload.get("run_url", args.run_url)

    actions = compute_alert_actions(records, run_url, args.stream, args.topic)
    if not actions:
        print("No alertable status changes detected.")
        return 0

    print(f"{len(actions)} alert(s) to send.")

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

    execute_alerts(actions, sender)
    return 0


if __name__ == "__main__":
    sys.exit(main())
