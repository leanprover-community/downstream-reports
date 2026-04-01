#!/usr/bin/env python3
"""Generate the bumping-bisect job matrix.

Reads bumping-seen state from the configured storage backend and queries the
GitHub API to determine which downstreams have new commits on their bumping
branch since the last run.  Writes ``{"include": [...]}`` as JSON to
``--output`` (default: ``matrix.json``).

This script replaces the inline Python block in the ``plan`` job of
``downstream-bump-bisect.yml``.  The surrounding shell in that job reads the
output file and sets the ``matrix`` and ``has_downstreams`` step outputs.

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
        description="Generate the bumping-bisect job matrix."
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

    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)

    seen_map = backend.load_bumping_seen()  # {downstream: last_seen_sha}

    payload = json.loads(args.inventory.read_text())
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

        last_seen = seen_map.get(item["name"])
        if not args.force and head_sha == last_seen:
            print(f"[plan] {item['name']}: no new commits on {bumping_branch}, skipping")
            continue

        lakefile_data = _gh_api(
            f"repos/{owner_repo}/contents/lakefile.toml?ref={bumping_branch}",
            token,
        )
        if lakefile_data is None:
            print(f"[plan] skipping {item['name']}: could not fetch lakefile.toml", file=sys.stderr)
            continue
        try:
            lakefile_text = base64.b64decode(lakefile_data["content"]).decode()
            lakefile_parsed = tomllib.loads(lakefile_text)
        except Exception as exc:
            print(
                f"[plan] skipping {item['name']}: failed to parse lakefile.toml: {exc}",
                file=sys.stderr,
            )
            continue

        dep_name = item.get("dependency_name", "mathlib")
        mathlib_pin = None
        for req in lakefile_parsed.get("require", []):
            if req.get("name") == dep_name:
                mathlib_pin = req.get("rev")
                break
        if not mathlib_pin:
            print(
                f"[plan] skipping {item['name']}: no {dep_name} pin found in bumping lakefile.toml",
                file=sys.stderr,
            )
            continue

        print(
            f"[plan] {item['name']}: bumping branch {bumping_branch}"
            f" head={head_sha[:12]} pin={mathlib_pin[:12]}"
        )
        entry = dict(item)
        entry["bumping_branch_head_commit"] = head_sha
        entry["bumping_mathlib_pin"] = mathlib_pin
        include.append(entry)

    args.output.write_text(json.dumps({"include": include}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
