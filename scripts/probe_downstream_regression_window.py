#!/usr/bin/env python3
"""Run the downstream regression HEAD probe and optional bisect for a preselected window.

This script is intentionally free of CI secrets.  It reads all the information
it needs from the selection.json artifact produced by the select step, clones
public repositories without authentication, and invokes hopscotch with an
environment that has had all known secrets stripped (see cache.cache_env).

Inputs:
  <selection>   — path to selection.json written by the select step.

Outputs:
  <output-dir>/result.json  — always written; consumed by the report step.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cache import cache_env, downstream_cache_dir, warm_downstream_cache
from scripts.git_ops import (
    build_commit_window,
    clone_downstream,
    clone_upstream,
    describe_commits,
    is_strict_ancestor,
    parent_commit,
    select_search_base_from_candidates,
)
from scripts.models import DownstreamConfig, Outcome, ValidationResult, WindowSelection
from scripts.storage import DownstreamStatusRecord
from scripts.validation import (
    append_commit_plan_artifact,
    build_result_from_tool,
    build_selection_error_result,
    classify_exit_code,
    commit_plan_artifact_path,
    load_selection,
    print_commit_plan_summary,
    run_validation_attempt,
    tool_summary_text,
    write_result,
)


# ---------------------------------------------------------------------------
# Skip heuristic: known-bad bisect
# ---------------------------------------------------------------------------


def try_skip_known_bad_bisect(
    *,
    skip_enabled: bool,
    selection: WindowSelection,
    previous: DownstreamStatusRecord | None,
    config: DownstreamConfig,
    upstream_ref: str,
    upstream_dir: Path,
    head_probe_run: subprocess.CompletedProcess[str],
    head_probe_state: dict[str, Any],
    head_probe_summary_text: str | None,
) -> ValidationResult | None:
    """Skip bisect when already failing with a known culprit and downstream unchanged.

    When the stored first-known-bad commit is an ancestor of the current target
    and the downstream source hasn't changed, re-bisecting would identify the
    same culprit.
    """
    if not skip_enabled:
        return None
    if previous is None or previous.first_known_bad_commit is None:
        return None
    if selection.downstream_commit != previous.downstream_commit:
        return None
    if not is_strict_ancestor(upstream_dir, previous.first_known_bad_commit, selection.target_commit):
        return None

    print(
        f"[{config.name}] HEAD probe failed; first_known_bad "
        f"{previous.first_known_bad_commit[:12]} is ancestor of target "
        f"and downstream unchanged; skipping bisect"
    )
    selection.decision_reason = (
        "The HEAD probe failed, the stored first-known-bad commit is an ancestor of "
        "the target, and the downstream has not changed.  Re-bisecting would identify "
        "the same culprit."
    )
    selection.next_action = "Skip the bisect probe and report the known failing result."
    return build_result_from_tool(
        config=config,
        downstream_commit=selection.downstream_commit,
        upstream_ref=upstream_ref,
        target_commit=selection.target_commit,
        search_mode="head-only-known-bad",
        tested_commits=[selection.target_commit],
        tested_commit_details=selection.tested_commit_details,
        truncated=False,
        tool_run=head_probe_run,
        state=head_probe_state,
        tool_summary=head_probe_summary_text,
        head_probe_outcome=selection.head_probe_outcome,
        head_probe_failure_stage=selection.head_probe_failure_stage,
        head_probe_summary=selection.head_probe_summary,
        pinned_commit=selection.pinned_commit,
        search_base_not_ancestor=selection.search_base_not_ancestor,
    )


# ---------------------------------------------------------------------------
# Culprit re-probe
# ---------------------------------------------------------------------------


def run_culprit_probe(
    *,
    config: DownstreamConfig,
    culprit_commit: str,
    upstream_dir: Path,
    project_dir: Path,
    output_dir: Path,
    env: dict[str, str],
    tool_exe: Path | None,
    quiet: bool = False,
) -> None:
    """Run the known culprit commit to capture fresh failure logs.

    Called after try_skip_known_bad_bisect fires. Runs hopscotch on
    first_known_bad_commit so the culprit log is present in this job's
    artifacts rather than requiring a back-link to an older run.

    Artifacts are written to output_dir/culprit-probe/tool-state/logs/culprit/.
    """
    try:
        run_validation_attempt(
            config=config,
            from_ref=parent_commit(upstream_dir, culprit_commit),
            to_ref=culprit_commit,
            project_dir=project_dir,
            output_dir=output_dir / "culprit-probe",
            tested_commits=[culprit_commit],
            env=env,
            tool_exe=tool_exe,
            quiet=quiet,
        )
    except Exception as exc:
        print(f"[{config.name}] warning: culprit probe failed: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the probe step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--tool-exe", type=Path)
    parser.add_argument("--max-commits", type=int, default=100000)
    parser.add_argument(
        "--upstream-repo", default="leanprover-community/mathlib4",
        help="Upstream repository to clone for git operations.",
    )
    parser.add_argument(
        "--skip-known-bad-bisect", action=argparse.BooleanOptionalAction, default=True,
        help="Skip bisect when the downstream is already failing with a known culprit "
             "that is an ancestor of the target and the downstream is unchanged.",
    )
    return parser


def main() -> int:
    """Execute the HEAD probe and optional bisect described by the selection payload."""

    args = build_parser().parse_args()
    selection = load_selection(args.selection)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Fast-path: the select step already resolved the result (e.g. skip-already-good
    # fired, or setup failed).  Write it directly without invoking hopscotch.
    if selection.pre_resolved_result is not None:
        payload = selection.pre_resolved_result
        # Sanity-check: the dict must look like a serialised ValidationResult.
        if "schema_version" not in payload or "outcome" not in payload:
            raise SystemExit(
                f"pre_resolved_result is missing required keys "
                f"(has: {sorted(payload.keys())})"
            )
        Outcome(payload["outcome"])  # raises ValueError on unknown outcome
        result_path = args.output_dir / "result.json"
        result_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        downstream_label = selection.downstream or "unknown"
        print(f"[{downstream_label}] pre-resolved result written; skipping all probes.")
        return 0

    if selection.downstream is None or selection.repo is None or selection.default_branch is None:
        raise SystemExit(f"selection is missing downstream metadata: {args.selection}")

    config = DownstreamConfig(
        name=selection.downstream,
        repo=selection.repo,
        default_branch=selection.default_branch,
        dependency_name=selection.dependency_name,
    )

    # Reconstruct prior episode state from the fields the select step embedded.
    # Used by try_skip_known_bad_bisect to avoid re-bisecting a known failure.
    previous: DownstreamStatusRecord | None = None
    if (
        selection.previous_first_known_bad_commit is not None
        or selection.previous_downstream_commit is not None
        or selection.previous_last_known_good_commit is not None
    ):
        previous = DownstreamStatusRecord(
            first_known_bad_commit=selection.previous_first_known_bad_commit,
            downstream_commit=selection.previous_downstream_commit,
            last_known_good_commit=selection.previous_last_known_good_commit,
        )

    try:
        upstream_dir = args.workdir / "mathlib4.git"
        downstream_dir = args.workdir / "downstreams" / config.name
        search_dir = args.workdir / "downstreams-search" / config.name
        cache_dir = downstream_cache_dir(args.workdir, config.name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        # cache_env() strips CI secrets — hopscotch and lake build see only
        # environment variables that are safe to expose to arbitrary code.
        env = cache_env(cache_dir)

        # Clone upstream for git operations (ancestor checks, parent-commit
        # lookups).  Public repo — no authentication required.
        clone_upstream(args.upstream_repo, upstream_dir)

        # Clone downstream for builds.  Public repo — no authentication required.
        clone_downstream(config, downstream_dir)

        warm_downstream_cache(config, project_dir=downstream_dir, output_dir=args.output_dir, env=env)

        target_commit = selection.target_commit
        if target_commit is None:
            raise ValueError("selection is missing target_commit")

        # --- HEAD probe ---
        head_from_ref = parent_commit(upstream_dir, target_commit)
        commit_plan_path = commit_plan_artifact_path(args.output_dir)
        append_commit_plan_artifact(
            output_dir=args.output_dir,
            label="head probe commit",
            commits=selection.tested_commit_details[:1],  # just the target
        )
        print_commit_plan_summary(
            downstream=config.name,
            label="head probe commit",
            commits=selection.tested_commit_details[:1],
            artifact_path=commit_plan_path,
        )

        head_probe_run, head_probe_state, head_probe_summary_text = run_validation_attempt(
            config=config,
            from_ref=head_from_ref,
            to_ref=target_commit,
            project_dir=downstream_dir,
            output_dir=args.output_dir / "head-probe",
            tested_commits=[target_commit],
            env=env,
            tool_exe=args.tool_exe,
            quiet=args.quiet,
        )

        selection.head_probe_outcome = classify_exit_code(head_probe_run.returncode).value
        selection.head_probe_failure_stage = head_probe_state.get("failureStage")
        selection.head_probe_summary = tool_summary_text(head_probe_run, head_probe_summary_text)

        head_probe_kwargs: dict = dict(
            head_probe_outcome=selection.head_probe_outcome,
            head_probe_failure_stage=selection.head_probe_failure_stage,
            head_probe_summary=selection.head_probe_summary,
        )

        upstream_ref = selection.upstream_ref or "master"

        def _build_result(
            *,
            search_mode: str,
            tested_commits: list[str],
            tested_commit_details: list,
            truncated: bool,
            tool_run: subprocess.CompletedProcess[str],
            state: dict[str, Any],
            tool_summary: str | None,
        ) -> ValidationResult:
            return build_result_from_tool(
                config=config,
                downstream_commit=selection.downstream_commit,
                upstream_ref=upstream_ref,
                target_commit=target_commit,
                search_mode=search_mode,
                tested_commits=tested_commits,
                tested_commit_details=tested_commit_details,
                truncated=truncated,
                tool_run=tool_run,
                state=state,
                tool_summary=tool_summary,
                pinned_commit=selection.pinned_commit,
                search_base_not_ancestor=selection.search_base_not_ancestor,
                **head_probe_kwargs,
            )

        if head_probe_run.returncode != 1:
            # Passed or error — no bisect needed.
            if selection.head_probe_outcome == "passed":
                selection.decision_reason = (
                    "The upper endpoint passed, so there is no failing window to bisect."
                )
                selection.next_action = (
                    "Skip the bisect. Report the passing head-only result and store "
                    "this target as the last-known-good."
                )
            else:
                selection.decision_reason = "The head probe did not produce a bisectable failure."
                selection.next_action = "Skip the bisect and report the current head-only result."
            result = _build_result(
                search_mode="head-only",
                tested_commits=[target_commit],
                tested_commit_details=selection.tested_commit_details[:1],
                truncated=False,
                tool_run=head_probe_run,
                state=head_probe_state,
                tool_summary=head_probe_summary_text,
            )
            write_result(args.output_dir / "result.json", result)
            return 0

        # HEAD probe failed — try the known-bad bisect skip before committing
        # to a full bisect run.
        result = try_skip_known_bad_bisect(
            skip_enabled=args.skip_known_bad_bisect and selection.skip_known_bad_bisect,
            selection=selection,
            previous=previous,
            config=config,
            upstream_ref=upstream_ref,
            upstream_dir=upstream_dir,
            head_probe_run=head_probe_run,
            head_probe_state=head_probe_state,
            head_probe_summary_text=head_probe_summary_text,
        )
        if result is not None:
            assert previous is not None  # guaranteed by try_skip_known_bad_bisect contract
            assert previous.first_known_bad_commit is not None
            run_culprit_probe(
                config=config,
                culprit_commit=previous.first_known_bad_commit,
                upstream_dir=upstream_dir,
                project_dir=downstream_dir,
                output_dir=args.output_dir,
                env=env,
                tool_exe=args.tool_exe,
                quiet=args.quiet,
            )
            write_result(args.output_dir / "result.json", result)
            return 0

        # --- Bisect probe ---
        # Try to widen the bisect window by verifying the stored LKG.  The
        # select step used the pinned commit as a conservative lower bound
        # because it cannot run hopscotch.  If the stored LKG is more recent
        # and still passes, we get a tighter window.
        stored_lkg = previous.last_known_good_commit if previous else None

        def verify_last_known_good(candidate: str) -> bool:
            """Run hopscotch against the stored LKG to confirm it still passes."""
            print(
                f"[{config.name}] verifying stored last-known-good "
                f"{candidate[:12]} before extending bisect window"
            )
            lkg_check_dir = args.workdir / "downstreams-lkg-check" / config.name
            lkg_clone_source = str(downstream_dir) if downstream_dir.exists() else None
            clone_downstream(config, lkg_check_dir, clone_source=lkg_clone_source)
            lkg_run, _, _ = run_validation_attempt(
                config=config,
                from_ref=parent_commit(upstream_dir, candidate),
                to_ref=candidate,
                project_dir=lkg_check_dir,
                output_dir=args.output_dir / "stored-last-known-good-check",
                tested_commits=[candidate],
                env=env,
                tool_exe=args.tool_exe,
                quiet=args.quiet,
            )
            return lkg_run.returncode == 0

        search_base_commit = select_search_base_from_candidates(
            upstream_dir=upstream_dir,
            pinned_commit=selection.pinned_commit,
            last_known_good=stored_lkg,
            verify_last_known_good=verify_last_known_good,
        )

        commit_window, truncated = build_commit_window(
            upstream_dir, target_commit, search_base_commit, max_commits=args.max_commits,
        )

        if search_base_commit is not None and len(commit_window) > 1:
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

            clone_source = str(downstream_dir) if downstream_dir.exists() else None
            clone_downstream(config, search_dir, clone_source=clone_source)

            probe_from_ref = parent_commit(upstream_dir, bisect_commits[0])
            probe_to_ref = bisect_commits[-1]
            tool_run, state, tool_summary = run_validation_attempt(
                config=config,
                from_ref=probe_from_ref,
                to_ref=probe_to_ref,
                project_dir=search_dir,
                output_dir=args.output_dir / "bisect",
                tested_commits=bisect_commits,
                env=env,
                tool_exe=args.tool_exe,
                bisect=True,
                quiet=args.quiet,
            )
            result = _build_result(
                search_mode="bisect",
                tested_commits=bisect_commits,
                tested_commit_details=tested_commit_details,
                truncated=truncated,
                tool_run=tool_run,
                state=state,
                tool_summary=tool_summary,
            )
        else:
            # No bisect window available — report a head-only failure.
            selection.decision_reason = (
                selection.decision_reason
                or "The upper endpoint failed but no bisect window could be derived."
            )
            selection.next_action = "Report the failing head-only result."
            result = _build_result(
                search_mode="head-only",
                tested_commits=[target_commit],
                tested_commit_details=selection.tested_commit_details[:1],
                truncated=False,
                tool_run=head_probe_run,
                state=head_probe_state,
                tool_summary=head_probe_summary_text,
            )

    except Exception as error:  # pragma: no cover - exercised via workflow, not unit tests.
        result = build_selection_error_result(selection, str(error))

    write_result(args.output_dir / "result.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
