#!/usr/bin/env python3
"""Post a single dispatch-level result comment on a mathlib4 PR.

Reads every ``result-*/result.json`` from the validate matrix and assembles
one Markdown comment that opens with a summary table and then renders a
per-entry section for each requested downstream. One comment per dispatch
keeps the requester's @-mention to a single notification and groups
related verdicts together in the PR conversation.

Inputs (env):
    GH_TOKEN     — token with issues:write on leanprover-community/mathlib4
    PR_NUMBER    — PR number on mathlib4
    MERGE_SHA    — merge SHA we tested against
    RUN_URL      — link to the validation run (for the result body)
    TRIGGERED_BY — optional GitHub login to @-mention at the top

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

# GitHub's PR comment limit is 65,536 chars; we leave a small safety margin
# for our wrapper text and the `gh api -f body=` request envelope.
COMMENT_MAX_CHARS = 60_000
# Largest failure log inlined for any single entry, even when there's room.
# Beyond this the readable signal stops growing — bigger logs go to the
# linked run artifact instead of bloating the comment.
PER_ENTRY_LOG_MAX = 12_000
# Smallest log we'd ever inline; below this we drop the log entirely and
# point at the run artifact. Keeps a stuffed dispatch readable.
PER_ENTRY_LOG_MIN = 1_500


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


def entry_label(name: str, mode: str, rev: str | None = None) -> str:
    """Compact mode-aware entry name that mirrors `!downstream-check` grammar.

    The label round-trips the user's request: a bare ``name`` is LKG mode
    (the default), ``name@rev`` pins the downstream rev, and the
    ``--merge-branch`` suffix marks merge mode. Used by both the dispatch
    summary table and the per-entry section headers so readers see the
    same token end-to-end.
    """
    label = name
    if rev:
        label = f"{name}@{rev}"
    if mode == "merge":
        label = f"{label} --merge-branch"
    return label


def verdict_summary(result: dict[str, Any]) -> str:
    """One-line verdict for the dispatch summary table."""
    status = result.get("status")
    mode = result.get("mode") or "merge"
    stage = result.get("stage")
    fkb = result.get("fkb_commit")
    if status == "pass":
        if mode == "lkg":
            return "✅ builds (rebased onto LKG)"
        return "✅ builds"
    if status == "fail":
        if mode == "lkg":
            # LKG mode rebased the PR onto a known-good mathlib, so a fail
            # here implicates the PR rather than master.
            return "❌ fails (attributable to the PR)"
        if fkb:
            return (
                f"❌ fails (master incompatibility at {commit_link(fkb)})"
            )
        return "❌ fails (attributable to the PR)"
    # infra_failure
    if mode == "lkg" and stage == "rebase_conflict":
        return "⚠️ could not validate (PR conflicts with LKG)"
    if mode == "lkg" and stage == "mathlib_build_at_lkg":
        return "⚠️ could not validate (mathlib build failed at LKG)"
    return f"⚠️ could not validate ({stage})"


def _display_name(result: dict[str, Any]) -> str:
    """Token the user typed for this entry (short name or owner/repo slug).

    Falls back to the canonical ``downstream`` name when the user wrote
    that form already. Used for the rendered entry label only — every
    other place uses the canonical name from ``result["downstream"]``.
    """
    return (
        result.get("requested_name")
        or result.get("downstream")
        or "(unknown)"
    )


def _section_header(result: dict[str, Any]) -> str:
    display = _display_name(result)
    mode = result.get("mode") or "merge"
    status = result.get("status", "infra_failure")
    stage = result.get("stage", "unknown")
    rev = result.get("downstream_rev")
    el = entry_label(display, mode, rev)
    rebased_suffix = " rebased onto LKG" if mode == "lkg" else ""
    if status == "pass":
        return f"## ✅ {el} builds against this PR{rebased_suffix}"
    if status == "fail":
        return f"## ❌ {el} fails against this PR{rebased_suffix}"
    if mode == "lkg" and stage == "rebase_conflict":
        return f"## ⚠️ {el}: could not validate (PR conflicts with LKG)"
    if mode == "lkg" and stage == "mathlib_build_at_lkg":
        return f"## ⚠️ {el}: could not validate (mathlib build failed at LKG)"
    return f"## ⚠️ {el}: could not validate (infra: {stage})"


def _framing_for(
    name: str, mode: str, status: str, fkb: str | None
) -> str:
    """Return the blockquote subtitle for an entry section, or '' to omit it.

    The wording depends on three axes:

    * mode (lkg vs merge) — what the run did
    * status (pass / fail / infra_failure) — whether a build completed
    * fkb (set / null) — whether the LKG snapshot positively records
      that master is currently broken for this downstream

    A clean pass with no master regression on record is unambiguous and
    needs no caveat — the recipe paragraph already says everything. Every
    other combination earns a one-sentence framing so the reader can
    interpret the verdict without scanning back to the dispatch grammar.
    """
    if mode == "lkg":
        if status == "pass" and not fkb:
            # Clean PR-only verdict; the section header + recipe already
            # convey the LKG rebase, no extra caveat needed.
            return ""
        if fkb:
            return (
                f"> This run replayed the PR's changes on top of a"
                f" mathlib revision compatible with {name}. Current"
                f" mathlib master is incompatible with {name} (first"
                f" regression at {commit_link(fkb)}), so this result should"
                f" purely be about the PR's effect on {name}."
            )
        return (
            f"> This run replayed the PR's changes on top of a mathlib"
            f" revision compatible with {name}, so the verdict is"
            f" independent of current mathlib master health."
        )

    # merge mode
    if status == "pass":
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

    return f"**{label}:** {recipe}."


# ---------------------------------------------------------------------------
# Per-entry section rendering
# ---------------------------------------------------------------------------


def render_entry_section(
    *,
    name: str,
    repo: str,
    default_branch: str,
    result: dict[str, Any],
    merge_sha: str,
    run_url: str,
    log_tail: str,
) -> str:
    """Render one downstream entry as a `##` section.

    The section is self-contained: header + optional framing subtitle +
    ``Tested:`` / ``Attempted:`` recipe + optional inline failure log.
    Callers stitch multiple sections under a single dispatch body
    via :func:`render_dispatch_body`.
    """
    status = result.get("status", "infra_failure")
    stage = result.get("stage", "unknown")
    mode = result.get("mode") or "merge"
    repo_slug = repo or "(unknown)"
    branch = default_branch or "(unknown)"

    header = _section_header(result)

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

    parts: list[str] = [header, ""]
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


# Backwards-compat alias for callers (and tests) that import the per-entry
# renderer under its previous name. The signature accepts ``triggered_by`` so
# legacy callers don't break, but the mention is now hoisted to the
# dispatch-level body; ``render_dispatch_body`` is the new entry point.
def render_body(
    *,
    name: str,
    repo: str,
    default_branch: str,
    result: dict[str, Any],
    merge_sha: str,
    run_url: str,
    log_tail: str,
    triggered_by: str = "",
) -> str:
    section = render_entry_section(
        name=name,
        repo=repo,
        default_branch=default_branch,
        result=result,
        merge_sha=merge_sha,
        run_url=run_url,
        log_tail=log_tail,
    )
    if not triggered_by:
        return section
    return f"_Requested by @{triggered_by}._\n\n{section}"


# ---------------------------------------------------------------------------
# Dispatch-level body rendering
# ---------------------------------------------------------------------------


def _summary_row(
    *,
    result: dict[str, Any],
    repo: str,
) -> tuple[str, str]:
    """Return ``(entry_cell, verdict_cell)`` for a single table row.

    Both cells are inline Markdown — the entry name is wrapped in backticks
    so the user-grammar token stands out, the verdict carries the icon and
    a one-line gloss with linked SHAs when meaningful.
    """
    display = _display_name(result)
    mode = result.get("mode") or "merge"
    rev = result.get("downstream_rev")
    el = entry_label(display, mode, rev)
    return f"`{el}`", verdict_summary(result)


def _render_summary_table(rows: list[tuple[str, str]]) -> str:
    """Two-column GitHub-flavored Markdown table of (entry, verdict)."""
    lines = ["| Entry | Verdict |", "|---|---|"]
    for entry_cell, verdict_cell in rows:
        lines.append(f"| {entry_cell} | {verdict_cell} |")
    return "\n".join(lines)


def render_dispatch_body(
    *,
    entries: list[dict[str, Any]],
    merge_sha: str,
    run_url: str,
    triggered_by: str = "",
) -> str:
    """Assemble the single dispatch-level comment body.

    ``entries`` is a list of dicts in the order to display, each carrying:

    * ``name``           — downstream name
    * ``repo``           — `owner/repo` for the downstream
    * ``default_branch`` — fallback rev for link rendering
    * ``result``         — parsed ``result.json``
    * ``log_tail``       — pre-truncated log slice for inlining

    The body opens with an optional @-mention, then the dispatch title,
    a summary table (when there are at least two entries), and one
    ``##`` section per entry.
    """
    parts: list[str] = []
    if triggered_by:
        parts.append(f"_Requested by @{triggered_by}._")
        parts.append("")
    parts.append(
        f"# Downstream validation against PR merge {commit_link(merge_sha)}"
        f" · [run]({run_url})"
    )
    parts.append("")

    if len(entries) >= 2:
        rows = [
            _summary_row(result=e["result"], repo=e.get("repo", ""))
            for e in entries
        ]
        parts.append(_render_summary_table(rows))
        parts.append("")

    for entry in entries:
        section = render_entry_section(
            name=entry["name"],
            repo=entry.get("repo", ""),
            default_branch=entry.get("default_branch", ""),
            result=entry["result"],
            merge_sha=merge_sha,
            run_url=run_url,
            log_tail=entry.get("log_tail", ""),
        )
        parts.append(section.rstrip())
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Size budgeting
# ---------------------------------------------------------------------------


def _has_inlined_log(result: dict[str, Any]) -> bool:
    """True if this result would carry an inline failure log."""
    status = result.get("status")
    if status == "fail":
        return True
    if status == "infra_failure" and result.get("stage") == "mathlib_build_at_lkg":
        return True
    return False


def _per_entry_log_budget(entries: list[dict[str, Any]]) -> int:
    """Per-entry log char cap, scaled to the number of failing entries.

    Reserve a slab of the comment budget for fixed scaffolding (title,
    table, section headers, recipe paragraphs) and split the rest among
    the entries that actually carry logs. Caps at ``PER_ENTRY_LOG_MAX``
    so a lone failure in an otherwise-passing dispatch doesn't drown the
    body; floors at ``PER_ENTRY_LOG_MIN`` so a stuffed dispatch still
    shows a usable tail per entry.
    """
    n_with_logs = sum(1 for e in entries if _has_inlined_log(e["result"]))
    if n_with_logs == 0:
        return PER_ENTRY_LOG_MAX
    # ~70% of the budget for logs; the other ~30% covers all scaffolding
    # even on the largest dispatches we expect (10+ entries).
    log_pool = (COMMENT_MAX_CHARS * 7) // 10
    return max(PER_ENTRY_LOG_MIN, min(PER_ENTRY_LOG_MAX, log_pool // n_with_logs))


def _shrink_to_fit(
    *,
    entries: list[dict[str, Any]],
    merge_sha: str,
    run_url: str,
    triggered_by: str,
    run_artifacts_url: str | None = None,
) -> str:
    """Render the dispatch body and progressively trim logs to fit the limit.

    Strategy: keep the largest inline log; if total exceeds
    ``COMMENT_MAX_CHARS`` after first render, halve the longest
    ``log_tail`` and re-render. Stop once we fit or every log has hit
    the floor. The final fallback drops all logs and prints a single
    "logs truncated — see run artifact" line.
    """
    body = render_dispatch_body(
        entries=entries,
        merge_sha=merge_sha,
        run_url=run_url,
        triggered_by=triggered_by,
    )
    if len(body) <= COMMENT_MAX_CHARS:
        return body

    # Halve the longest log repeatedly until we fit or hit the floor.
    while len(body) > COMMENT_MAX_CHARS:
        # Find the entry with the longest inlined log.
        candidates = [e for e in entries if _has_inlined_log(e["result"])]
        if not candidates:
            break
        biggest = max(candidates, key=lambda e: len(e.get("log_tail", "")))
        tail = biggest.get("log_tail", "")
        if len(tail) <= PER_ENTRY_LOG_MIN:
            # Already at the floor; can't shrink this one further.
            # Drop this entry's log entirely so the next iteration moves on.
            biggest["log_tail"] = ""
        else:
            new_len = max(PER_ENTRY_LOG_MIN, len(tail) // 2)
            biggest["log_tail"] = tail[-new_len:]
        body = render_dispatch_body(
            entries=entries,
            merge_sha=merge_sha,
            run_url=run_url,
            triggered_by=triggered_by,
        )

    if len(body) > COMMENT_MAX_CHARS:
        # Last resort: drop every log.
        for e in entries:
            e["log_tail"] = ""
        body = render_dispatch_body(
            entries=entries,
            merge_sha=merge_sha,
            run_url=run_url,
            triggered_by=triggered_by,
        )

    return body


# ---------------------------------------------------------------------------
# Result loading + main
# ---------------------------------------------------------------------------


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


def _entry_sort_key(result: dict[str, Any]) -> tuple[str, str, str]:
    """Stable ordering for the summary table + sections.

    Sort by (name, rev_slug, mode) so two entries that differ only in mode
    sit next to each other in the table and the reader can compare verdicts
    at a glance.
    """
    name = (result.get("downstream") or "").lower()
    rev = result.get("downstream_rev") or ""
    mode = result.get("mode") or "merge"
    return (name, rev, mode)


def collect_entries(
    results_dir: Path, inventory: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Walk the results dir, parse each ``result.json``, and assemble entries."""
    entries: list[dict[str, Any]] = []
    for sub in sorted(results_dir.iterdir()):
        if not sub.is_dir() or not sub.name.startswith("result-"):
            continue
        result_path = sub / "result.json"
        if not result_path.exists():
            print(
                f"warning: missing result.json in {sub}; skipping",
                file=sys.stderr,
            )
            continue
        with result_path.open() as handle:
            result = json.load(handle)

        name = result.get("downstream") or sub.name[len("result-"):]
        meta = inventory.get(name, {})
        log_tail = read_log_tail(sub / "build.log", PER_ENTRY_LOG_MAX)
        entries.append(
            {
                "name": name,
                "repo": meta.get("repo", ""),
                "default_branch": meta.get("default_branch", ""),
                "result": result,
                "log_tail": log_tail,
            }
        )

    entries.sort(key=lambda e: _entry_sort_key(e["result"]))
    return entries


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
    # Optional: the GitHub login of whoever dispatched this validation.
    # When the trigger is a `!downstream-check` comment, this is the
    # commenter; when the trigger is a manual `gh workflow run`, the
    # caller can pass `-f triggered_by=<login>` to opt in. Falsy when
    # unset, in which case the body skips the mention line.
    triggered_by = os.environ.get("TRIGGERED_BY", "").strip()

    inventory = load_inventory_lookup()

    if not args.results_dir.exists():
        print(f"no results directory at {args.results_dir}", file=sys.stderr)
        return 1

    entries = collect_entries(args.results_dir, inventory)
    if not entries:
        print("warning: no result artifacts found", file=sys.stderr)
        return 1

    # Pre-trim each log to the per-entry budget before assembly, so the
    # shrink-to-fit loop only has to handle pathological overruns.
    per_entry_max = _per_entry_log_budget(entries)
    for e in entries:
        tail = e.get("log_tail", "")
        if len(tail) > per_entry_max:
            e["log_tail"] = tail[-per_entry_max:]

    body = _shrink_to_fit(
        entries=entries,
        merge_sha=merge_sha,
        run_url=run_url,
        triggered_by=triggered_by,
    )

    post_comment(pr_number, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
