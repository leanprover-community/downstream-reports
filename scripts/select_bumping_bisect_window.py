#!/usr/bin/env python3
"""Select the bisect window for a downstream bumping-branch probe."""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_downstream_regression import (
    WindowSelection,
    append_commit_plan_artifact,
    build_commit_window,
    build_error_result,
    build_result_from_tool,
    cache_env,
    classify_exit_code,
    clone_downstream,
    clone_upstream,
    commit_plan_artifact_path,
    describe_commits,
    downstream_cache_dir,
    load_inventory,
    parent_commit,
    pinned_dependency_rev,
    print_commit_plan_summary,
    render_selection_summary,
    resolve_upstream_target,
    run_validation_attempt,
    selection_summary_path,
    should_run_boundary_search,
    tool_summary_text,
    warm_downstream_cache,
    write_result,
    write_selection,
)
from scripts.storage import FilesystemBackend


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the bumping-branch window-selection step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--workflow", default="bumping")
    parser.add_argument("--downstream", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-commits", type=int, default=100000)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--tool-exe", type=Path)
    parser.add_argument(
        "--backend", choices=["filesystem", "sql"], default="filesystem",
    )
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument("--dsn", default=None)
    return parser


def main() -> int:
    """Run HEAD validation against the bumping branch and write a probe selection."""

    args = build_parser().parse_args()
    inventory = load_inventory(args.inventory)
    if args.downstream not in inventory:
        raise SystemExit(f"unknown downstream '{args.downstream}'")

    config = inventory[args.downstream]

    # Downstreams without a bumping_branch are silently skipped: the plan job
    # should never include them in the matrix, but if this script is called
    # directly for such a downstream we write a no-op selection and exit cleanly.
    if not config.bumping_branch:
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
        write_selection(args.output_dir / "selection.json", selection)
        write_result(
            args.output_dir / "result.json",
            build_error_result(
                config,
                "unknown",
                f"downstream '{config.name}' has no bumping_branch configured",
            ),
        )
        summary = render_selection_summary(selection)
        selection_summary_path(args.output_dir).write_text(summary)
        print(summary, end="")
        return 0

    if args.backend == "sql":
        dsn = args.dsn or os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise SystemExit("--dsn or POSTGRES_DSN environment variable is required when --backend=sql")
        from sqlalchemy import create_engine
        from scripts.storage import SqlBackend
        backend = SqlBackend(create_engine(dsn))
    else:
        if not args.state_root:
            raise SystemExit("--state-root is required when --backend=filesystem")
        backend = FilesystemBackend(args.state_root)

    status = backend.load_all_statuses(args.workflow, "leanprover-community/mathlib4")
    previous = status.get(config.name)
    stored_last_known_good = previous.last_known_good_commit if previous else None

    # Clone the bumping branch instead of default_branch.
    bumping_config = dataclasses.replace(config, default_branch=config.bumping_branch)

    # selection.default_branch is set to bumping_branch so that the probe step
    # (which reconstructs DownstreamConfig from selection.json) also clones the
    # bumping branch when running the bisect.
    selection = WindowSelection(
        downstream=config.name,
        repo=config.repo,
        default_branch=config.bumping_branch,
        dependency_name=config.dependency_name,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def finalize_selection() -> None:
        summary = render_selection_summary(selection)
        selection_summary_path(args.output_dir).write_text(summary)
        print(summary, end="")

    try:
        upstream_dir = args.workdir / "mathlib4.git"
        downstream_dir = args.workdir / "downstreams" / config.name
        cache_dir = downstream_cache_dir(args.workdir, config.name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        env = cache_env(cache_dir)

        clone_upstream("leanprover-community/mathlib4", upstream_dir)
        downstream_commit = clone_downstream(bumping_config, downstream_dir)

        # Derive the mathlib target from the pin in the bumping branch's lakefile.
        pinned_rev = pinned_dependency_rev(downstream_dir, config.dependency_name)
        if pinned_rev is None:
            raise RuntimeError(
                f"could not read {config.dependency_name} pin from bumping branch "
                f"'{config.bumping_branch}' lakefile.toml"
            )
        target_commit = resolve_upstream_target(upstream_dir, pinned_rev)

        selection.upstream_ref = pinned_rev
        selection.downstream_commit = downstream_commit
        selection.target_commit = target_commit

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

        warm_downstream_cache(
            config, project_dir=downstream_dir, output_dir=args.output_dir, env=env
        )
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
                selection.decision_reason = (
                    "The upper endpoint passed, so there is no failing window to bisect."
                )
                selection.next_action = (
                    "Skip the probe task. Report the passing head-only result and store "
                    "this target as the last-known-good."
                )
            else:
                selection.decision_reason = "The head probe did not produce a bisectable failure."
                selection.next_action = "Skip the probe task and report the current head-only result."
            result = build_result_from_tool(
                config=config,
                downstream_commit=downstream_commit,
                upstream_ref=pinned_rev,
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
            )
            write_selection(args.output_dir / "selection.json", selection)
            write_result(args.output_dir / "result.json", result)
            finalize_selection()
            return 0

        # The bumping branch's pin equals the target, so we never use it as the
        # lower bound (that would collapse the window to a single commit).  Use
        # only the stored last-known-good from the bumping regression state.
        commit_window, truncated = build_commit_window(
            upstream_dir,
            target_commit,
            stored_last_known_good,
            args.max_commits,
        )
        if should_run_boundary_search(head_probe_run.returncode, commit_window):
            if stored_last_known_good is None:
                raise RuntimeError("boundary search requested without a known-good base commit")
            bisect_commits = [stored_last_known_good, *commit_window]
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
            selection.selected_lower_bound_commit = stored_last_known_good
            selection.probe_from_ref = parent_commit(upstream_dir, bisect_commits[0])
            selection.probe_to_ref = bisect_commits[-1]
            selection.decision_reason = (
                "The upper endpoint failed and a multi-commit good-to-bad window was found."
            )
            selection.next_action = (
                f"Run the probe task in bisect mode on {len(bisect_commits)} commits from "
                f"`{stored_last_known_good[:12]}` to `{target_commit[:12]}`."
            )
            write_selection(args.output_dir / "selection.json", selection)
            finalize_selection()
            return 0

        if stored_last_known_good is None:
            selection.decision_reason = (
                "The upper endpoint failed, but no known-good lower bound was available to "
                "define a window. (This is expected on the first run for this downstream.)"
            )
        else:
            selection.decision_reason = (
                "The upper endpoint failed, but the stored lower bound did not produce a "
                "multi-commit window."
            )
        selection.next_action = "Skip the probe task and report the failing head-only result."
        result = build_result_from_tool(
            config=config,
            downstream_commit=downstream_commit,
            upstream_ref=pinned_rev,
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
        )
    except Exception as error:  # pragma: no cover - exercised via workflow, not unit tests.
        selection.decision_reason = (
            f"Window selection failed before a probe plan could be produced: {error}"
        )
        selection.next_action = "Skip the probe task and report the setup error."
        result = build_error_result(
            config,
            selection.upstream_ref or "unknown",
            str(error),
        )

    write_selection(args.output_dir / "selection.json", selection)
    write_result(args.output_dir / "result.json", result)
    finalize_selection()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
