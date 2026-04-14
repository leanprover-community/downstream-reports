#!/usr/bin/env python3
"""Generate the on-demand validation job matrix.

Reads tested-commit state from the configured storage backend and queries the
GitHub API to determine which downstreams have new commits on their bumping
branch since the last run.  Writes ``{"include": [...]}`` as JSON to
``--output`` (default: ``matrix.json``).

This script is called by the ``plan`` job of
``mathlib-downstream-ondemand.yml``.  The surrounding shell in that job reads
the output file and sets the ``matrix`` and ``has_downstreams`` step outputs.

Reads ``GITHUB_TOKEN`` from the environment.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.storage import add_backend_args, create_backend


def _gh_api(path: str, token: str) -> dict | None:
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        print(
            f"[plan] GitHub API error {exc.code} for {url}: {exc.read().decode()}",
            file=sys.stderr,
        )
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate the on-demand validation job matrix."
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("matrix.json"),
        help="Path to write the matrix JSON (default: matrix.json)",
    )
    parser.add_argument(
        "--downstream",
        default="",
        help="Limit to a single downstream name; empty means all",
    )
    parser.add_argument(
        "--branch",
        default="",
        help=(
            "Explicit branch to test (on-demand mode). Requires --downstream. "
            "Overrides the bumping_branch from inventory."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Include all matching downstreams even if no new bumping-branch commits",
    )
    add_backend_args(parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required")

    if args.branch and not args.downstream:
        raise SystemExit("--branch requires --downstream")

    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)

    seen = backend.load_tested_downstream_commits("ondemand")

    payload = json.loads(args.inventory.read_text())

    # On-demand mode: explicit --downstream + --branch bypasses inventory
    # bumping_branch filtering — any branch on any downstream is allowed.
    if args.branch:
        candidates = [
            item for item in payload.get("downstreams", [])
            if item.get("enabled", True) and item["name"] == args.downstream
        ]
        # Inject the explicit branch so downstream processing uses it.
        for item in candidates:
            item["bumping_branch"] = args.branch
    else:
        candidates = [
            item for item in payload.get("downstreams", [])
            if item.get("enabled", True)
            and item.get("bumping_branch")
            and (not args.downstream or item["name"] == args.downstream)
        ]

    include = []
    for item in candidates:
        owner_repo = item["repo"]
        bumping_branch = item["bumping_branch"]

        ref_data = _gh_api(f"repos/{owner_repo}/git/ref/heads/{bumping_branch}", token)
        if ref_data is None:
            print(f"[plan] skipping {item['name']}: could not fetch branch ref", file=sys.stderr)
            continue
        head_sha = ref_data["object"]["sha"]

        if not args.force and (item["name"], head_sha) in seen:
            print(f"[plan] {item['name']}: no new commits on {bumping_branch}, skipping")
            continue

        print(
            f"[plan] {item['name']}: bumping branch {bumping_branch}"
            f" head={head_sha[:12]}"
        )

        entry = dict(item)
        include.append(entry)

    args.output.write_text(json.dumps({"include": include}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
