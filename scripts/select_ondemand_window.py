#!/usr/bin/env python3
"""Select the bisect window for a downstream on-demand probe.

This script handles only git/database work and never invokes hopscotch.
All hopscotch invocations (HEAD probe and bisect) live in the probe step
(probe_downstream_regression_window.py), which runs in a secret-free job.

Outputs:
  <output-dir>/selection.json     — always written; consumed by the probe step.
  <output-dir>/selection-summary.md — human-readable summary for the job log.
  <output-dir>/tested-commits.txt   — full commit list (when a window exists).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.git_ops import (
    build_commit_window,
    clone_downstream,
    clone_upstream,
    describe_commits,
    parent_commit,
    pinned_commit_from_manifest,
    resolve_upstream_target,
    select_search_base_from_candidates,
)
from scripts.models import WindowSelection, load_inventory
from scripts.storage import add_backend_args, create_backend
from scripts.validation import (
    append_commit_plan_artifact,
    build_error_result,
    commit_plan_artifact_path,
    print_commit_plan_summary,
    render_selection_summary,
    selection_summary_path,
    write_result,
    write_selection,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the on-demand window-selection step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--workflow", default="ondemand")
    parser.add_argument("--downstream", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-commits", type=int, default=100000)
    parser.add_argument(
        "--branch-override",
        default="",
        help=(
            "Explicit branch to test instead of the downstream's configured "
            "bumping_branch. Used by on-demand dispatch."
        ),
    )
    add_backend_args(parser)
    return parser


def main() -> int:
    """Resolve the upstream target, clone repos, compute the bisect window."""

    args = build_parser().parse_args()
    inventory = load_inventory(args.inventory)
    if args.downstream not in inventory:
        raise SystemExit(f"unknown downstream '{args.downstream}'")

    config = inventory[args.downstream]

    # Allow an explicit branch override for on-demand dispatch.
    effective_branch = args.branch_override or config.bumping_branch

    # Downstreams without a bumping_branch (and no override) are silently
    # skipped: the plan job should never include them in the matrix, but if
    # this script is called directly for such a downstream we write a no-op
    # selection and exit cleanly.
    if not effective_branch:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        selection = WindowSelection(
            downstream=config.name,
            repo=config.repo,
            default_branch=config.default_branch,
            dependency_name=config.dependency_name,
            decision_reason=(
                f"downstream '{config.name}' has no bumping_branch configured; skipping."
            ),
            next_action="No action required.",
        )
        err_result = build_error_result(
            config,
            "unknown",
            f"downstream '{config.name}' has no bumping_branch configured",
        )
        selection.pre_resolved_result = err_result.to_json()
        write_selection(args.output_dir / "selection.json", selection)
        summary = render_selection_summary(selection)
        selection_summary_path(args.output_dir).write_text(summary)
        print(summary, end="")
        return 0

    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)

    status = backend.load_all_statuses(args.workflow, "leanprover-community/mathlib4")
    previous = status.get(config.name)
    stored_last_known_good = previous.last_known_good_commit if previous else None

    # Clone the target branch instead of default_branch.
    ondemand_config = dataclasses.replace(config, default_branch=effective_branch)

    # selection.default_branch is set to the target branch so that the probe
    # step (which reconstructs DownstreamConfig from selection.json) also
    # clones the correct branch when running the bisect.
    selection = WindowSelection(
        downstream=config.name,
        repo=config.repo,
        default_branch=effective_branch,
        dependency_name=config.dependency_name,
        skip_known_bad_bisect=config.skip_known_bad_bisect,
    )

    # Embed prior episode state so the probe step can apply skip heuristics
    # without a database connection.
    if previous is not None:
        selection.previous_first_known_bad_commit = previous.first_known_bad_commit
        selection.previous_downstream_commit = previous.downstream_commit
        selection.previous_last_known_good_commit = previous.last_known_good_commit

    args.output_dir.mkdir(parents=True, exist_ok=True)

    def finalize(*, error: str | None = None) -> int:
        """Write selection.json and the human-readable summary, then return 0."""
        if error is not None:
            err_result = build_error_result(
                config,
                selection.upstream_ref or "unknown",
                error,
            )
            selection.pre_resolved_result = err_result.to_json()
            selection.decision_reason = f"Window selection failed: {error}"
            selection.next_action = "Probe step will write the error result directly."
        summary = render_selection_summary(selection)
        selection_summary_path(args.output_dir).write_text(summary)
        print(summary, end="")
        write_selection(args.output_dir / "selection.json", selection)
        return 0

    try:
        upstream_dir = args.workdir / "mathlib4.git"
        downstream_dir = args.workdir / "downstreams" / config.name

        clone_upstream("leanprover-community/mathlib4", upstream_dir)
        downstream_commit = clone_downstream(ondemand_config, downstream_dir)
        selection.pinned_commit = pinned_commit_from_manifest(
            downstream_dir, config.dependency_name
        )

        # TODO: Derive the mathlib target from a parameter
        target_commit = resolve_upstream_target(upstream_dir, "master")

        selection.upstream_ref = target_commit
        selection.downstream_commit = downstream_commit
        selection.target_commit = target_commit

        # Prefer the lake-manifest.json pin as the lower bound — it records the
        # exact mathlib SHA the downstream last fetched, which predates any
        # regression on the bumping branch.  Fall back to the stored
        # last-known-good when no manifest pin is available.
        # LKG verification requires hopscotch and is deferred to the probe step.
        search_base_commit = select_search_base_from_candidates(
            upstream_dir=upstream_dir,
            pinned_commit=selection.pinned_commit,
            last_known_good=stored_last_known_good,
            verify_last_known_good=None,
        )
        selection.selected_lower_bound_commit = search_base_commit

        commit_window, truncated = build_commit_window(
            upstream_dir, target_commit, search_base_commit, args.max_commits,
        )
        commit_plan_path = commit_plan_artifact_path(args.output_dir)

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
                output_dir=args.output_dir,
                label="bisect window (oldest to newest)",
                commits=tested_commit_details,
                truncated=truncated,
                bisect_window=True,
            )
            print_commit_plan_summary(
                downstream=config.name,
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
                output_dir=args.output_dir,
                label="head probe commit",
                commits=head_probe_detail,
            )
            print_commit_plan_summary(
                downstream=config.name,
                label="head probe commit",
                commits=head_probe_detail,
                artifact_path=commit_plan_path,
            )

    except Exception as error:  # pragma: no cover
        return finalize(error=str(error))

    return finalize()


if __name__ == "__main__":
    raise SystemExit(main())
