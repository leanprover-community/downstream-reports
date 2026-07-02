"""Shared window-construction core for the select scripts.

Both select legs (``select_downstream_regression_window.py`` and
``select_ondemand_window.py``) resolve their target differently but then
run the same pipeline: pick a search base, build the commit window,
classify it as bisect vs head-only, and write the selection artifacts.
That pipeline lives here so a change to window construction lands in one
place and both workflows pick it up.

This module handles only git work and never invokes hopscotch — the same
contract as the select scripts that call it.
"""

from __future__ import annotations

from pathlib import Path

from scripts.git_ops import (
    build_commit_window,
    describe_commits,
    is_strict_ancestor,
    parent_commit,
    select_search_base_from_candidates,
)
from scripts.models import DownstreamConfig, WindowSelection
from scripts.storage import DownstreamStatusRecord
from scripts.validation import (
    append_commit_plan_artifact,
    build_error_result,
    commit_plan_artifact_path,
    print_commit_plan_summary,
    render_selection_summary,
    selection_summary_path,
    write_selection,
)


def embed_prior_state(
    selection: WindowSelection, previous: DownstreamStatusRecord | None
) -> None:
    """Embed prior episode state so the probe step can apply skip heuristics
    (``try_skip_known_bad_bisect``) without a database connection."""

    if previous is None:
        return
    selection.previous_first_known_bad_commit = previous.first_known_bad_commit
    selection.previous_downstream_commit = previous.downstream_commit
    selection.previous_last_known_good_commit = previous.last_known_good_commit


def finalize_selection(
    *,
    selection: WindowSelection,
    config: DownstreamConfig,
    output_dir: Path,
    upstream_ref: str,
    error: str | None = None,
) -> int:
    """Write selection.json and the human-readable summary, then return 0.

    With ``error`` set, an error ``ValidationResult`` is embedded as the
    selection's ``pre_resolved_result`` so the probe step writes it directly
    instead of probing.
    """

    if error is not None:
        err_result = build_error_result(config, upstream_ref, error)
        selection.pre_resolved_result = err_result.to_json()
        selection.decision_reason = f"Window selection failed: {error}"
        selection.next_action = "Probe step will write the error result directly."
    summary = render_selection_summary(selection)
    selection_summary_path(output_dir).write_text(summary)
    print(summary, end="")
    write_selection(output_dir / "selection.json", selection)
    return 0


def populate_selection_window(
    *,
    selection: WindowSelection,
    downstream_name: str,
    upstream_dir: Path,
    stored_last_known_good: str | None,
    max_commits: int,
    output_dir: Path,
) -> None:
    """Select the search base and fill ``selection`` with the probe plan.

    Requires ``selection.target_commit`` (and ``selection.pinned_commit``
    when a manifest pin exists) to be resolved already.  Picks the lower
    bound from the pin/LKG candidates, builds the commit window, and sets
    the bisect vs head-only plan on ``selection`` — writing the
    commit-plan artifact and step-log summary along the way.  LKG
    verification requires hopscotch, so it is deferred to the probe step
    (which re-derives the window with a verified LKG if that turns out to
    be needed and valid).
    """

    target_commit = selection.target_commit

    search_base_commit = select_search_base_from_candidates(
        upstream_dir=upstream_dir,
        pinned_commit=selection.pinned_commit,
        last_known_good=stored_last_known_good,
        verify_last_known_good=None,
    )
    selection.selected_lower_bound_commit = search_base_commit

    commit_window, truncated = build_commit_window(
        upstream_dir, target_commit, search_base_commit, max_commits,
    )

    if (
        search_base_commit is not None
        and search_base_commit != target_commit
        and len(commit_window) <= 1
        and not is_strict_ancestor(upstream_dir, search_base_commit, target_commit)
    ):
        selection.search_base_not_ancestor = True
        print(
            f"::warning title=search-base-not-ancestor::[{downstream_name}] "
            f"pinned commit {search_base_commit[:12]} is not an ancestor of "
            f"target {target_commit[:12]} — no bisect window is available; "
            f"falling back to head-only probe"
        )

    commit_plan_path = commit_plan_artifact_path(output_dir)

    if search_base_commit is not None and len(commit_window) > 1:
        # A multi-commit window is available; set up for bisect.
        bisect_commits = [search_base_commit, *commit_window]
        tested_commit_details = describe_commits(upstream_dir, bisect_commits)
        selection.has_bisect_window = True
        selection.search_mode = "bisect"
        selection.tested_commits = bisect_commits
        selection.tested_commit_details = tested_commit_details
        selection.commit_window_truncated = truncated
        selection.probe_from_ref = parent_commit(upstream_dir, bisect_commits[0])
        selection.probe_to_ref = bisect_commits[-1]
        selection.decision_reason = (
            "A multi-commit window was found between the search base and the target. "
            "The probe step will run the HEAD validation first; if it fails, bisect will follow."
        )
        selection.next_action = (
            f"Run the probe task on {len(bisect_commits)} commits from "
            f"`{search_base_commit[:12]}` to `{target_commit[:12]}`."
        )
        append_commit_plan_artifact(
            output_dir=output_dir,
            label="bisect window (oldest to newest)",
            commits=tested_commit_details,
            truncated=truncated,
            bisect_window=True,
        )
        print_commit_plan_summary(
            downstream=downstream_name,
            label="bisect window (oldest to newest)",
            commits=tested_commit_details,
            artifact_path=commit_plan_path,
        )
    else:
        # Single-commit or no window; HEAD probe only.
        head_probe_detail = describe_commits(upstream_dir, [target_commit])
        selection.has_bisect_window = False
        selection.search_mode = "head-only"
        selection.tested_commits = [target_commit]
        selection.tested_commit_details = head_probe_detail
        selection.decision_reason = (
            "No multi-commit window is available (no usable search base, or window "
            "collapsed to a single commit).  The probe step will check HEAD only."
        )
        selection.next_action = "Run the probe task for HEAD validation only."
        append_commit_plan_artifact(
            output_dir=output_dir,
            label="head probe commit",
            commits=head_probe_detail,
        )
        print_commit_plan_summary(
            downstream=downstream_name,
            label="head probe commit",
            commits=head_probe_detail,
            artifact_path=commit_plan_path,
        )
