#!/usr/bin/env python3
"""Select the downstream regression bisect window before any probe run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cache import cache_env, downstream_cache_dir, warm_downstream_cache
from scripts.git_ops import (
    build_commit_window,
    clone_downstream,
    clone_upstream,
    describe_commits,
    is_strict_ancestor,
    parent_commit,
    resolve_search_base_commit,
    resolve_upstream_target,
    select_search_base_from_candidates,
    should_run_boundary_search,
)
from scripts.models import DownstreamConfig, WindowSelection, load_inventory
from scripts.storage import add_backend_args, create_backend
from scripts.validation import (
    append_commit_plan_artifact,
    build_error_result,
    build_result_from_tool,
    classify_exit_code,
    commit_plan_artifact_path,
    print_commit_plan_summary,
    render_selection_summary,
    run_validation_attempt,
    selection_summary_path,
    tool_summary_text,
    write_result,
    write_selection,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the window-selection step."""

    parser = argparse.ArgumentParser()
    # Inventory mode: look up a named downstream from a JSON file.
    parser.add_argument("--inventory", type=Path, default=None)
    parser.add_argument("--workflow", default="regression")
    parser.add_argument("--downstream", default=None)
    # Inline mode: specify the downstream directly without an inventory file.
    parser.add_argument("--upstream-repo", default="leanprover-community/mathlib4")
    parser.add_argument("--upstream", default="leanprover-community/mathlib4")
    parser.add_argument("--downstream-repo", default=None)
    parser.add_argument("--downstream-branch", default=None)
    parser.add_argument("--dependency-name", default=None)
    parser.add_argument("--downstream-name", default=None)
    parser.add_argument("--upstream-ref", default="master")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-commits", type=int, default=100000)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--tool-exe", type=Path)
    add_backend_args(parser)
    return parser


def main() -> int:
    """Run HEAD validation and write either a final result or a probe selection."""

    args = build_parser().parse_args()

    if args.inventory is not None:
        inventory = load_inventory(args.inventory)
        if args.downstream not in inventory:
            raise SystemExit(f"unknown downstream '{args.downstream}'")
        config = inventory[args.downstream]
    else:
        if not (args.downstream_repo and args.downstream_branch and args.dependency_name):
            raise SystemExit(
                "provide either --inventory/--downstream or "
                "--downstream-repo/--downstream-branch/--dependency-name"
            )
        name = args.downstream_name or (
            args.downstream_repo.rstrip("/").split("/")[-1].removesuffix(".git")
        )
        config = DownstreamConfig(
            name=name,
            repo=args.downstream_repo,
            default_branch=args.downstream_branch,
            dependency_name=args.dependency_name,
        )

    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)
    status = backend.load_all_statuses(args.workflow, args.upstream)
    previous = status.get(config.name)
    selection = WindowSelection(
        downstream=config.name,
        repo=config.repo,
        default_branch=config.default_branch,
        dependency_name=config.dependency_name,
        upstream_ref=args.upstream_ref,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def finalize_selection() -> None:
        summary = render_selection_summary(selection)
        selection_summary_file = selection_summary_path(args.output_dir)
        selection_summary_file.write_text(summary)
        print(summary, end="")

    try:
        upstream_dir = args.workdir / "mathlib4.git"
        downstream_dir = args.workdir / "downstreams" / config.name
        search_dir = args.workdir / "downstreams-search" / config.name
        cache_dir = downstream_cache_dir(args.workdir, config.name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        env = cache_env(cache_dir)

        clone_upstream(args.upstream_repo, upstream_dir)
        target_commit = resolve_upstream_target(upstream_dir, args.upstream_ref)
        downstream_commit = clone_downstream(config, downstream_dir)
        pinned_search_base_commit = resolve_search_base_commit(
            project_dir=downstream_dir,
            dependency_name=config.dependency_name,
            upstream_dir=upstream_dir,
            last_known_good=None,
        )
        selection.downstream_commit = downstream_commit
        selection.target_commit = target_commit
        selection.pinned_commit = pinned_search_base_commit

        head_probe_details = describe_commits(upstream_dir, [target_commit])
        selection.tested_commits = [target_commit]
        selection.tested_commit_details = head_probe_details
        commit_plan_path = commit_plan_artifact_path(args.output_dir)
        append_commit_plan_artifact(
            output_dir=args.output_dir,
            label="head probe commit",
            commits=head_probe_details,
        )
        print_commit_plan_summary(
            downstream=config.name,
            label="head probe commit",
            commits=head_probe_details,
            artifact_path=commit_plan_path,
        )

        warm_downstream_cache(config, project_dir=downstream_dir, output_dir=args.output_dir, env=env)
        head_probe_run, head_probe_state, head_probe_summary_text = run_validation_attempt(
            config=config,
            from_ref=parent_commit(upstream_dir, target_commit),
            to_ref=target_commit,
            project_dir=downstream_dir,
            output_dir=args.output_dir / "head-probe",
            tested_commits=[target_commit],
            env=env,
            tool_exe=args.tool_exe,
            quiet=args.quiet,
        )
        selection.head_probe_outcome = classify_exit_code(head_probe_run.returncode).value
        selection.head_probe_failure_stage = head_probe_state.get("stage")
        selection.head_probe_summary = tool_summary_text(head_probe_run, head_probe_summary_text)

        if head_probe_run.returncode != 1:
            if selection.head_probe_outcome == "passed":
                selection.decision_reason = "The upper endpoint passed, so there is no failing window to bisect."
                selection.next_action = (
                    "Skip the probe task. Report the passing head-only result and store this target as the "
                    "last-known-good."
                )
            else:
                selection.decision_reason = "The head probe did not produce a bisectable failure."
                selection.next_action = "Skip the probe task and report the current head-only result."
            result = build_result_from_tool(
                config=config,
                downstream_commit=downstream_commit,
                upstream_ref=args.upstream_ref,
                target_commit=target_commit,
                search_mode="head-only",
                tested_commits=[target_commit],
                tested_commit_details=head_probe_details,
                truncated=False,
                tool_run=head_probe_run,
                state=head_probe_state,
                tool_summary=head_probe_summary_text,
                head_probe_outcome=selection.head_probe_outcome,
                head_probe_failure_stage=selection.head_probe_failure_stage,
                head_probe_summary=selection.head_probe_summary,
                pinned_commit=pinned_search_base_commit,
            )
            write_selection(args.output_dir / "selection.json", selection)
            write_result(args.output_dir / "result.json", result)
            finalize_selection()
            return 0

        stored_last_known_good = previous.last_known_good_commit if previous else None

        def verify_last_known_good(candidate_commit: str) -> bool:
            verification_output_dir = args.output_dir / "stored-last-known-good-check"
            print(
                f"[{config.name}] verifying stored last-known-good "
                f"{candidate_commit[:12]} before extending bisect window"
            )
            clone_downstream(config, search_dir, clone_source=str(downstream_dir))
            tool_run, _, _ = run_validation_attempt(
                config=config,
                from_ref=parent_commit(upstream_dir, candidate_commit),
                to_ref=candidate_commit,
                project_dir=search_dir,
                output_dir=verification_output_dir,
                tested_commits=[candidate_commit],
                env=env,
                tool_exe=args.tool_exe,
                quiet=args.quiet,
            )
            return tool_run.returncode == 0

        search_base_commit = select_search_base_from_candidates(
            upstream_dir=upstream_dir,
            pinned_commit=pinned_search_base_commit,
            last_known_good=stored_last_known_good,
            verify_last_known_good=verify_last_known_good,
        )
        selection.selected_lower_bound_commit = search_base_commit
        commit_window, truncated = build_commit_window(
            upstream_dir,
            target_commit,
            search_base_commit,
            args.max_commits,
        )
        if should_run_boundary_search(head_probe_run.returncode, commit_window):
            if search_base_commit is None:
                raise RuntimeError("boundary search requested without a known-good base commit")
            bisect_commits = [search_base_commit, *commit_window]
            tested_commit_details = describe_commits(upstream_dir, bisect_commits)
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
            selection.needs_probe = True
            selection.search_mode = "bisect"
            selection.tested_commits = bisect_commits
            selection.tested_commit_details = tested_commit_details
            selection.commit_window_truncated = truncated
            # Store the --from/--to refs for the probe step, which has no
            # local mathlib clone and cannot recompute them independently.
            # from_ref = parent of bisect_commits[0] (= search_base_commit) so
            # that the tool fetches exactly bisect_commits via the GitHub API.
            selection.probe_from_ref = parent_commit(upstream_dir, bisect_commits[0])
            selection.probe_to_ref = bisect_commits[-1]
            selection.decision_reason = (
                "The upper endpoint failed and a multi-commit good-to-bad window was found."
            )
            selection.next_action = (
                f"Run the probe task in bisect mode on {len(bisect_commits)} commits from "
                f"`{search_base_commit[:12]}` to `{target_commit[:12]}`."
            )
            write_selection(args.output_dir / "selection.json", selection)
            finalize_selection()
            return 0

        if search_base_commit is None:
            selection.decision_reason = (
                "The upper endpoint failed, but no known-good lower bound was available to define a window."
            )
        elif search_base_commit == target_commit:
            selection.decision_reason = (
                "The upper endpoint failed, but the selected lower bound equals the target commit, so "
                "there is no search interval."
            )
        elif not is_strict_ancestor(upstream_dir, search_base_commit, target_commit):
            selection.decision_reason = (
                "The upper endpoint failed, but the selected lower bound is not an ancestor of the target "
                "commit."
            )
        else:
            selection.decision_reason = (
                "The upper endpoint failed, but the computed window still collapsed to a single-commit check."
            )
        selection.next_action = "Skip the probe task and report the failing head-only result."
        result = build_result_from_tool(
            config=config,
            downstream_commit=downstream_commit,
            upstream_ref=args.upstream_ref,
            target_commit=target_commit,
            search_mode="head-only",
            tested_commits=[target_commit],
            tested_commit_details=head_probe_details,
            truncated=False,
            tool_run=head_probe_run,
            state=head_probe_state,
            tool_summary=head_probe_summary_text,
            head_probe_outcome=selection.head_probe_outcome,
            head_probe_failure_stage=selection.head_probe_failure_stage,
            head_probe_summary=selection.head_probe_summary,
            pinned_commit=pinned_search_base_commit,
        )
    except Exception as error:  # pragma: no cover - exercised via workflow, not unit tests.
        selection.decision_reason = f"Window selection failed before a probe plan could be produced: {error}"
        selection.next_action = "Skip the probe task and report the setup error."
        result = build_error_result(config, args.upstream_ref, str(error))

    write_selection(args.output_dir / "selection.json", selection)
    write_result(args.output_dir / "result.json", result)
    finalize_selection()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
