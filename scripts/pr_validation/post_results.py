#!/usr/bin/env python3
"""Post / update per-downstream result comments on a mathlib4 PR.

For each ``result-<name>/result.json`` produced by the validate matrix, find
an existing comment on the PR identified by the marker

    <!-- pr-check-downstream:result:<name> -->

and edit it in place; otherwise, post a new comment.

Inputs (env):
    GH_TOKEN  — token with issues:write on leanprover-community/mathlib4
    PR_NUMBER — PR number on mathlib4
    MERGE_SHA — merge SHA we tested against (used to derive the PR head SHA)
    RUN_URL   — link to the validation run (for the result body)

Inputs (CLI):
    --results-dir  — directory containing ``result-<name>/result.json``
                     and ``result-<name>/build.log`` artifact directories
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from log_filter import read_log_tail

REPO = "leanprover-community/mathlib4"
MARKER_PREFIX = "<!-- pr-check-downstream:result:"
LOG_MAX_CHARS = 60_000  # GitHub comment limit is 65,536; leave room for wrapper text


def gh_api(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", "api", *args],
        check=True,
        text=True,
        capture_output=True,
        **kwargs,
    )


def find_existing_comment(pr_number: str, marker: str) -> str | None:
    """Return the comment id matching ``marker``, or None."""
    page = 1
    while True:
        result = gh_api(
            [
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{REPO}/issues/{pr_number}/comments?per_page=100&page={page}",
            ]
        )
        comments = json.loads(result.stdout)
        if not comments:
            return None
        for comment in comments:
            if marker in comment.get("body", ""):
                return str(comment["id"])
        if len(comments) < 100:
            return None
        page += 1


def upsert_comment(pr_number: str, marker: str, body: str) -> None:
    existing = find_existing_comment(pr_number, marker)
    if existing is None:
        gh_api(
            [
                "-X",
                "POST",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{REPO}/issues/{pr_number}/comments",
                "-f",
                f"body={body}",
            ]
        )
    else:
        gh_api(
            [
                "-X",
                "PATCH",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{REPO}/issues/comments/{existing}",
                "-f",
                f"body={body}",
            ]
        )


def get_pr_head_sha(merge_sha: str) -> str | None:
    """Return the PR head SHA from the merge commit's second parent, or None on failure."""
    try:
        result = gh_api(
            [
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{REPO}/commits/{merge_sha}",
            ]
        )
        parents = json.loads(result.stdout).get("parents", [])
        if len(parents) >= 2:
            return parents[1]["sha"]
    except Exception as exc:
        print(f"warning: could not fetch PR head SHA: {exc}", file=sys.stderr)
    return None


def short_sha(sha: str) -> str:
    return sha[:7] if sha else "(unknown)"


def render_body(
    name: str,
    repo: str,
    default_branch: str,
    result: dict[str, Any],
    head_sha: str | None,
    merge_sha: str,
    run_url: str,
    log_tail: str,
) -> str:
    status = result.get("status", "infra_failure")
    marker = f"{MARKER_PREFIX}{name} -->"
    repo_slug = repo or "(unknown)"
    branch = default_branch or "(unknown)"

    if head_sha:
        tested_ref = (
            f"[`{short_sha(head_sha)}`]"
            f"(https://github.com/{REPO}/commit/{head_sha})"
        )
    else:
        tested_ref = f"`{short_sha(merge_sha)}`"

    if status == "pass":
        header = f"### ✅ {name} — builds against this PR"
    elif status == "fail":
        header = f"### ❌ {name} — fails against this PR"
    else:  # infra_failure
        stage = result.get("stage", "unknown")
        header = (
            f"### ⚠️ {name} — could not validate (infra: {stage})"
        )

    parts = [
        header,
        "",
        f"Tested {tested_ref} against `{repo_slug}@{branch}`.",
        f"[run]({run_url})",
        "",
    ]

    if status == "fail" and log_tail:
        parts.extend(
            [
                "<details><summary>last build log lines</summary>",
                "",
                "```",
                log_tail,
                "```",
                "",
                "</details>",
                "",
            ]
        )
    elif status == "infra_failure":
        message = result.get("message")
        if message:
            parts.extend([f"_{message}_", ""])
        parts.extend(
            [
                "This is an infrastructure failure; it does not imply anything"
                " about the PR.",
                "",
            ]
        )

    if status in {"pass", "fail"}:
        parts.extend(
            [
                "> ⚠️ This run did not baseline against master. If master is"
                " currently broken for this downstream, the failure may not be"
                " attributable to this PR. See the latest downstream report"
                " for downstream health.",
                "",
            ]
        )

    parts.append(marker)
    return "\n".join(parts)


def load_inventory_lookup() -> dict[str, dict[str, Any]]:
    """Look up downstream metadata from the local inventory."""
    inventory_path = (
        Path(__file__).resolve().parents[2]
        / "ci"
        / "inventory"
        / "downstreams.json"
    )
    with inventory_path.open() as handle:
        data = json.load(handle)
    return {entry["name"]: entry for entry in data["downstreams"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        required=True,
        type=Path,
        help="Directory containing the downloaded result-* artifacts.",
    )
    args = parser.parse_args()

    pr_number = os.environ["PR_NUMBER"]
    merge_sha = os.environ["MERGE_SHA"]
    run_url = os.environ["RUN_URL"]

    head_sha = get_pr_head_sha(merge_sha)

    inventory = load_inventory_lookup()

    if not args.results_dir.exists():
        print(f"no results directory at {args.results_dir}", file=sys.stderr)
        return 1

    posted = 0
    for entry in sorted(args.results_dir.iterdir()):
        if not entry.is_dir():
            continue
        if not entry.name.startswith("result-"):
            continue
        result_path = entry / "result.json"
        if not result_path.exists():
            print(
                f"warning: missing result.json in {entry}; skipping",
                file=sys.stderr,
            )
            continue
        with result_path.open() as handle:
            result = json.load(handle)

        name = result.get("downstream") or entry.name[len("result-"):]
        meta = inventory.get(name, {})
        body = render_body(
            name=name,
            repo=meta.get("repo", ""),
            default_branch=meta.get("default_branch", ""),
            result=result,
            head_sha=head_sha,
            merge_sha=merge_sha,
            run_url=run_url,
            log_tail=read_log_tail(entry / "build.log", LOG_MAX_CHARS),
        )
        marker = f"{MARKER_PREFIX}{name} -->"
        upsert_comment(pr_number, marker, body)
        posted += 1

    if posted == 0:
        print("warning: no result artifacts found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
