#!/usr/bin/env python3
"""Post per-entry result comments on a mathlib4 PR.

For each ``result-*/result.json`` produced by the validate matrix, render
a self-contained Markdown comment and POST it to the PR. Each comment
body carries the full verdict (header + subtitle + Tested recipe +
optional failure log), so the most recent comment for a given matrix
entry is always the authoritative one for that dispatch.

Inputs (env):
    GH_TOKEN  — token with issues:write on leanprover-community/mathlib4
    PR_NUMBER — PR number on mathlib4
    MERGE_SHA — merge SHA we tested against
    RUN_URL   — link to the validation run (for the result body)

Inputs (CLI):
    --results-dir  — directory containing ``result-<name>-<slug>-<mode>/``
                     artifact directories.
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

# GitHub comment limit is 65,536; leave room for wrapper text.
LOG_MAX_CHARS = 60_000


def gh_api(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", "api", *args],
        check=True,
        text=True,
        capture_output=True,
        **kwargs,
    )


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


def short_sha(sha: str) -> str:
    return sha[:7] if sha else "(unknown)"


# ---------------------------------------------------------------------------
# Link / recipe rendering helpers
# ---------------------------------------------------------------------------


def commit_link(sha: str | None, repo: str = REPO) -> str:
    """Render a commit SHA as a backticked + linked Markdown reference."""
    if not sha:
        return "`(unknown)`"
    return f"[`{short_sha(sha)}`](https://github.com/{repo}/commit/{sha})"


def compare_link(base: str, head: str, repo: str = REPO) -> str:
    """Render a `base..head` commit-range link to GitHub's compare view."""
    return (
        f"[`{short_sha(base)}..{short_sha(head)}`]"
        f"(https://github.com/{repo}/compare/{base}..{head})"
    )


def downstream_link(
    repo_slug: str,
    sha: str | None,
    rev: str | None = None,
) -> str:
    """Render `repo@<rev-or-short>` linked to that commit on the downstream.

    When ``rev`` is provided (the user-supplied refspec — a branch / tag /
    SHA), the link text shows that rev for clarity; otherwise we fall back
    to a 7-char short SHA. The URL always points at the resolved SHA so
    the reader gets the exact tested tree.
    """
    if not sha:
        if rev:
            return f"`{repo_slug}@{rev}`"
        return f"`{repo_slug}`"
    label = rev if rev else short_sha(sha)
    return f"[`{repo_slug}@{label}`](https://github.com/{repo_slug}/commit/{sha})"


def _framing_for(
    name: str, mode: str, status: str, fkb: str | None
) -> str:
    """Return the blockquote subtitle for a comment, or '' to omit it.

    The wording depends on three axes:

    * mode (lkg vs merge) — what the run did
    * status (pass / fail / infra_failure) — whether a build completed
    * fkb (set / null) — whether the LKG snapshot positively records
      that master is currently broken for this downstream

    LKG-mode runs always frame the verdict as independent of master
    health; when fkb is set we additionally name why that matters.

    Merge-mode runs lean on fkb to be definitive: a fail with fkb set
    points at the existing regression and recommends LKG mode; a fail
    with fkb null reports master as healthy so the verdict implicates
    the PR. Merge-mode passes need no subtitle — the recipe is enough.
    """
    if mode == "lkg":
        if fkb:
            return (
                f"> This run replayed the PR's changes on top of a"
                f" mathlib revision compatible with {name}. Current"
                f" mathlib master is incompatible with {name} (first"
                f" regression at {commit_link(fkb)}), so this verdict is"
                f" purely about the PR's effect on {name}."
            )
        return (
            f"> This run replayed the PR's changes on top of a mathlib"
            f" revision compatible with {name}, so the verdict is"
            f" independent of current mathlib master health."
        )

    # merge mode
    if status == "pass":
        # A clean build against master+PR is unambiguous; no caveat.
        return ""
    if status == "fail":
        if fkb:
            return (
                f"> mathlib master is currently incompatible with {name}"
                f" — the regression was first observed at"
                f" {commit_link(fkb)}. This failure may reflect that"
                f" existing incompatibility rather than the PR itself."
                f" Drop `--merge-branch` to re-run against {name}'s"
                f" last-known-good mathlib instead."
            )
        return (
            f"> mathlib master is currently known to build with {name},"
            f" so this failure is attributable to the PR."
        )
    # merge-mode infra_failure: the per-stage explainer below carries
    # the actionable message; an extra master-baseline note here just
    # adds noise.
    return ""


def render_test_tree_paragraph(
    *,
    name: str,
    repo_slug: str,
    branch: str,
    result: dict[str, Any],
    merge_sha: str,
    run_url: str,
) -> str:
    """One-paragraph recipe of what this run built (pass / fail) or attempted (infra failure).

    Pass / fail bodies describe what was tested in the past tense:

        **Tested:** N PR commit(s) cherry-picked onto …, built against …

    Infra-failure bodies describe what the run was doing when it stopped,
    in gerund form, so the reader knows the build did not complete:

        **Attempted:** cherry-picking N PR commit(s) onto … and building
        against …
    """
    status = result.get("status", "infra_failure")
    mode = result.get("mode") or "merge"
    pr_base = result.get("pr_base_sha")
    pr_head = result.get("pr_head_sha")
    n_commits = result.get("commits_replayed")
    lkg = result.get("lkg_commit")
    ds_sha = result.get("downstream_sha")
    # `downstream_rev` is set only when the user requested a specific rev
    # (i.e. it differs from the inventory's default_branch). When present
    # we want the link text to show that rev rather than just the short
    # resolved SHA, so the reader sees what was asked for.
    ds_rev = result.get("downstream_rev")

    attempted = status not in {"pass", "fail"}
    label = "Attempted" if attempted else "Tested"

    # Downstream-side link / label. The link form is reachable only when
    # the clone+checkout step actually populated downstream_sha; on early
    # infra failures we fall back to a backticked rev (or default branch).
    if ds_sha:
        ds = downstream_link(repo_slug, ds_sha, ds_rev)
    elif ds_rev:
        ds = f"`{repo_slug}@{ds_rev}`"
    else:
        ds = f"`{repo_slug}@{branch}`"

    if mode == "lkg":
        if lkg and pr_base and pr_head and pr_base != pr_head:
            count = n_commits if n_commits is not None else "?"
            commits = f"{count} PR commit(s) ({compare_link(pr_base, pr_head)})"
            lkg_phrase = (
                f"{name}'s last-known-good mathlib commit {commit_link(lkg)}"
            )
            if attempted:
                recipe = (
                    f"cherry-picking {commits} onto {lkg_phrase}"
                    f" and building against {ds}"
                )
            else:
                recipe = (
                    f"{commits} cherry-picked onto {lkg_phrase},"
                    f" built against {ds}"
                )
        elif lkg:
            # Fast-forward merge or pre-cherry-pick infra failure: still
            # surface the LKG anchor.
            lkg_phrase = (
                f"{name}'s last-known-good mathlib commit {commit_link(lkg)}"
            )
            if attempted:
                recipe = (
                    f"putting the PR's tree on top of {lkg_phrase}"
                    f" and building against {ds}"
                )
            else:
                recipe = (
                    f"the PR's tree on top of {lkg_phrase},"
                    f" built against {ds}"
                )
        else:
            recipe = "(LKG commit not recorded)"
    else:
        # Merge mode: the PR's would-be-merged tree.
        if pr_base and pr_head and pr_base != pr_head:
            count = n_commits if n_commits is not None else "?"
            tree = (
                f"the PR's merge tree {commit_link(merge_sha)}"
                f" (head {commit_link(pr_head)}, {count} commit(s) over base"
                f" {commit_link(pr_base)})"
            )
        else:
            tree = f"the PR's merge tree {commit_link(merge_sha)}"
        if attempted:
            recipe = f"building {ds} against {tree}"
        else:
            recipe = f"{tree}, built against {ds}"

    return f"**{label}:** {recipe}. [run]({run_url})"


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------


def render_body(
    *,
    name: str,
    repo: str,
    default_branch: str,
    result: dict[str, Any],
    merge_sha: str,
    run_url: str,
    log_tail: str,
) -> str:
    status = result.get("status", "infra_failure")
    stage = result.get("stage", "unknown")
    mode = result.get("mode") or "merge"
    repo_slug = repo or "(unknown)"
    branch = default_branch or "(unknown)"
    rebased_suffix = " rebased onto LKG" if mode == "lkg" else ""

    if status == "pass":
        header = f"### ✅ {name} builds against this PR{rebased_suffix}"
    elif status == "fail":
        header = f"### ❌ {name} fails against this PR{rebased_suffix}"
    elif mode == "lkg" and stage == "rebase_conflict":
        header = f"### ⚠️ {name}: could not validate (PR conflicts with LKG)"
    elif mode == "lkg" and stage == "mathlib_build_at_lkg":
        header = (
            f"### ⚠️ {name}: could not validate (mathlib build failed at LKG)"
        )
    else:  # generic infra_failure
        header = f"### ⚠️ {name}: could not validate (infra: {stage})"

    test_tree = render_test_tree_paragraph(
        name=name,
        repo_slug=repo_slug,
        branch=branch,
        result=result,
        merge_sha=merge_sha,
        run_url=run_url,
    )

    # The framing/explainer reads as a subtitle right under the header so a
    # skimmer learns "what this verdict means" before scanning the recipe.
    # `fkb_commit` (when present) lets us state master health definitively
    # instead of hedging — the LKG snapshot has positively recorded a
    # regression on master for this downstream.
    fkb = result.get("fkb_commit")
    framing = _framing_for(name, mode, status, fkb)
    parts = [header, ""]
    if framing:
        parts.extend([framing, ""])
    parts.extend([test_tree, ""])

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
        if mode == "lkg" and stage == "rebase_conflict":
            parts.extend(
                [
                    "The PR's commits do not apply cleanly on top of"
                    f" {name}'s last-known-good mathlib commit. The changes"
                    " in this PR likely depend on later mathlib commits, so"
                    " we cannot test them in isolation against an older"
                    " mathlib.",
                    "",
                ]
            )
        elif mode == "lkg" and stage == "mathlib_build_at_lkg":
            parts.extend(
                [
                    "Mathlib failed to build with this PR's commits"
                    f" cherry-picked onto {name}'s last-known-good mathlib"
                    " commit. The PR likely relies on post-LKG mathlib"
                    " changes; we cannot validate it against an older"
                    " mathlib.",
                    "",
                ]
            )
            if log_tail:
                parts.extend(
                    [
                        "<details><summary>mathlib build log</summary>",
                        "",
                        "```",
                        log_tail,
                        "```",
                        "",
                        "</details>",
                        "",
                    ]
                )
        else:
            if message:
                parts.extend([f"_{message}_", ""])
            parts.extend(
                [
                    "This is an infrastructure failure; it does not imply"
                    " anything about the PR.",
                    "",
                ]
            )

    return "\n".join(parts).rstrip() + "\n"


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
            merge_sha=merge_sha,
            run_url=run_url,
            log_tail=read_log_tail(entry / "build.log", LOG_MAX_CHARS),
        )

        post_comment(pr_number, body)
        posted += 1

    if posted == 0:
        print("warning: no result artifacts found", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
