#!/usr/bin/env python3
"""Post / update per-downstream result comments on a mathlib4 PR.

For each ``result-<name>/result.json`` produced by the validate matrix, find
an existing comment on the PR identified by the marker

    <!-- pr-check-downstream:result:<name> -->

and edit it in place; otherwise, post a new comment.  Each update prepends the
current run to a hidden history list so the comment accumulates a run log over
the life of the PR.

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
HISTORY_KEY = "pr-check-downstream:history-data"
LOG_MAX_CHARS = 60_000  # GitHub comment limit is 65,536; leave room for wrapper text


def gh_api(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", "api", *args],
        check=True,
        text=True,
        capture_output=True,
        **kwargs,
    )


def find_existing_comment(pr_number: str, marker: str) -> tuple[str, str] | None:
    """Return ``(comment_id, body)`` for the comment matching ``marker``, or None."""
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
            body = comment.get("body", "")
            if marker in body:
                return str(comment["id"]), body
        if len(comments) < 100:
            return None
        page += 1


def post_comment(pr_number: str, body: str) -> None:
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


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def parse_history(name: str, body: str) -> list[dict[str, Any]]:
    """Extract the history list embedded in a previous comment body."""
    prefix = f"<!-- {HISTORY_KEY}:{name}\n"
    start = body.find(prefix)
    if start == -1:
        return []
    json_start = start + len(prefix)
    end = body.find("\n-->", json_start)
    if end == -1:
        return []
    try:
        data = json.loads(body[json_start:end])
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


def encode_history(name: str, entries: list[dict[str, Any]]) -> str:
    return f"<!-- {HISTORY_KEY}:{name}\n{json.dumps(entries)}\n-->"


def make_history_entry(
    head_sha: str | None,
    merge_sha: str,
    status: str,
    run_url: str,
    downstream_sha: str | None,
) -> dict[str, Any]:
    return {
        "head_sha": head_sha or merge_sha,
        "status": status,
        "run_url": run_url,
        "downstream_sha": downstream_sha or None,
    }


def render_history_line(entry: dict[str, Any], repo_slug: str, branch: str) -> str:
    head_sha = entry.get("head_sha", "")
    status = entry.get("status", "")
    run_url = entry.get("run_url", "")
    ds_sha = entry.get("downstream_sha") or ""

    icon = "✅" if status == "pass" else "❌" if status == "fail" else "⚠️"
    verb = "passed" if status == "pass" else "failed" if status == "fail" else "infra failure"

    sha_link = (
        f"[`{short_sha(head_sha)}`](https://github.com/{REPO}/commit/{head_sha})"
        if head_sha
        else "`(unknown)`"
    )
    ds_part = f" [`{short_sha(ds_sha)}`]" if ds_sha else ""
    return (
        f"- {sha_link} {icon} {verb} against"
        f" `{repo_slug}@{branch}`{ds_part} — [run]({run_url})"
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_body(
    name: str,
    repo: str,
    default_branch: str,
    result: dict[str, Any],
    head_sha: str | None,
    merge_sha: str,
    run_url: str,
    log_tail: str,
    history: list[dict[str, Any]],
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
        header = f"### ⚠️ {name} — could not validate (infra: {stage})"

    parts = [
        header,
        "",
        f"Tested {tested_ref} against `{repo_slug}@{branch}`. [run]({run_url})",
        "",
    ]

    if status == "fail" and log_tail:
        parts.extend(
            [
                "<details><summary>failure log</summary>",
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

    # Previous runs (history[0] is the current run; history[1:] are older ones)
    previous = history[1:]
    if previous:
        parts.extend(
            [
                "---",
                "",
                "**Previous runs**",
                "",
                *[render_history_line(e, repo_slug, branch) for e in previous],
                "",
            ]
        )

    parts.append(marker)
    parts.append(encode_history(name, history))
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
        marker = f"{MARKER_PREFIX}{name} -->"

        existing = find_existing_comment(pr_number, marker)
        history = parse_history(name, existing[1] if existing else "")
        history = [
            make_history_entry(
                head_sha=head_sha,
                merge_sha=merge_sha,
                status=result.get("status", "infra_failure"),
                run_url=run_url,
                downstream_sha=result.get("downstream_sha"),
            ),
            *history,
        ]

        body = render_body(
            name=name,
            repo=meta.get("repo", ""),
            default_branch=meta.get("default_branch", ""),
            result=result,
            head_sha=head_sha,
            merge_sha=merge_sha,
            run_url=run_url,
            log_tail=read_log_tail(entry / "build.log", LOG_MAX_CHARS),
            history=history,
        )

        post_comment(pr_number, body)
        posted += 1

    if posted == 0:
        print("warning: no result artifacts found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
