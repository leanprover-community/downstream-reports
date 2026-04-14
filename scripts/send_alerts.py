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
    ALERTABLE_STATES,
    DryRunSender,
    ZulipSender,
    compute_alert_actions,
    execute_alerts,
    fetch_commit_titles,
    fetch_tags,
    format_error_notice_message,
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

    github_token = os.environ.get("GITHUB_TOKEN") or None

    # Collect mathlib commit SHAs that need title lookups (only for alertable records).
    shas_to_fetch: set[str] = set()
    for r in records:
        state = r.get("episode_state", "")
        if state not in ALERTABLE_STATES:
            continue
        if r.get("target_commit"):
            shas_to_fetch.add(r["target_commit"])
        if state == "new_failure" and r.get("first_known_bad"):
            shas_to_fetch.add(r["first_known_bad"])
        elif state == "recovered" and r.get("previous_first_known_bad"):
            shas_to_fetch.add(r["previous_first_known_bad"])

    commit_titles: dict[str, str] = {}
    if shas_to_fetch:
        print(f"Fetching commit titles for {len(shas_to_fetch)} SHA(s)…")
        commit_titles = fetch_commit_titles(list(shas_to_fetch), token=github_token)

    # Also fetch downstream commit titles (used in the alert message header).
    downstream_shas_by_repo: dict[str, list[str]] = {}
    for r in records:
        if r.get("episode_state", "") not in ALERTABLE_STATES:
            continue
        repo = r.get("repo", "")
        ds_commit = r.get("downstream_commit")
        if repo and ds_commit and ds_commit not in downstream_shas_by_repo.get(repo, []):
            downstream_shas_by_repo.setdefault(repo, []).append(ds_commit)
    if downstream_shas_by_repo:
        print(f"Fetching downstream commit titles for {len(downstream_shas_by_repo)} repo(s)…")
        for repo, shas in downstream_shas_by_repo.items():
            commit_titles.update(fetch_commit_titles(shas, repo=repo, token=github_token))

    print("Fetching mathlib tags…")
    sha_to_tag = fetch_tags(token=github_token)
    print(f"  {len(sha_to_tag)} tag(s) loaded.")

    actions = compute_alert_actions(records, run_url, args.stream, args.topic, commit_titles=commit_titles, sha_to_tag=sha_to_tag)
    n_errors = sum(1 for r in records if r.get("outcome") == "error")

    if not actions and not n_errors:
        print("No alertable status changes detected.")
        return 0

    if actions:
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

    if actions:
        execute_alerts(actions, sender)

    if n_errors:
        noun = "build" if n_errors == 1 else "builds"
        print(f"Sending error notice for {n_errors} {noun}.")
        error_msg = format_error_notice_message(n_errors, run_url)
        try:
            sender.send_message(args.stream, args.topic, error_msg)
        except Exception as exc:
            print(f"Failed to send error notice: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
