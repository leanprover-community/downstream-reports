#!/usr/bin/env python3
"""Select the downstream regression bisect window before any probe run.

This script handles only git/database work and never invokes hopscotch.
All hopscotch invocations (HEAD probe and bisect) live in the probe step
(probe_downstream_regression_window.py), which runs in a secret-free job.

Outputs:
  <output-dir>/selection.json   — always written; consumed by the probe step.
  <output-dir>/selection-summary.md — human-readable summary for the job log.
  <output-dir>/tested-commits.txt   — full commit list (when a window exists).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.git_ops import (
    build_commit_window,
    clone_downstream,
    clone_upstream,
    describe_commits,
    parent_commit,
    resolve_search_base_commit,
    resolve_upstream_target,
    select_search_base_from_candidates,
)
from scripts.models import DownstreamConfig, Outcome, ValidationResult, WindowSelection, load_inventory
from scripts.storage import DownstreamStatusRecord, add_backend_args, create_backend
from scripts.validation import (
    append_commit_plan_artifact,
    build_error_result,
    build_skip_result,
    commit_plan_artifact_path,
    print_commit_plan_summary,
    render_selection_summary,
    selection_summary_path,
    write_selection,
)


# ---------------------------------------------------------------------------
# Skip heuristic: already-good
# ---------------------------------------------------------------------------


def try_skip_already_good(
    *,
    skip_enabled: bool,
    selection: WindowSelection,
    previous: DownstreamStatusRecord | None,
    config: DownstreamConfig,
    upstream_ref: str,
) -> ValidationResult | None:
    """Skip if target == last-known-good and the downstream hasn't changed.

    Unlikely in practice given mathlib's high churn, but handles re-runs and
    quiet periods cheaply.
    """

    if not skip_enabled:
        return None
    if previous is None or previous.last_known_good_commit is None:
        return None
    if selection.target_commit != previous.last_known_good_commit:
        return None
    if selection.downstream_commit != previous.downstream_commit:
        return None

    print(
        f"[{config.name}] target {selection.target_commit[:12]} == last_known_good "
        f"and downstream unchanged; skipping"
    )
    selection.decision_reason = (
        "Target commit matches the stored last-known-good and the downstream "
        "has not changed.  This exact combination was already verified as passing."
    )
    selection.next_action = "Skip all probes and report the cached passing result."
    return build_skip_result(
        config=config,
        downstream_commit=selection.downstream_commit,
        upstream_ref=upstream_ref,
        target_commit=selection.target_commit,
        search_mode="skipped-already-good",
        outcome=Outcome.PASSED,
        last_successful_commit=selection.target_commit,
        summary="Skipped: target and downstream unchanged since last passing validation.",
        pinned_commit=selection.pinned_commit,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
    # Skip-optimisation flag (on by default; pass --no-skip-already-good to force).
    # --skip-known-bad-bisect lives in the probe parser since it is applied there.
    parser.add_argument(
        "--skip-already-good", action=argparse.BooleanOptionalAction, default=True,
        help="Skip validation when target == last-known-good and downstream unchanged.",
    )
    add_backend_args(parser)
    return parser


def main() -> int:
    """Resolve the upstream target, clone repos, compute the bisect window."""

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
        skip_known_bad_bisect=config.skip_known_bad_bisect,
    )

    # Embed prior episode state so the probe step can apply skip heuristics
    # (try_skip_known_bad_bisect) without a database connection.
    if previous is not None:
        selection.previous_first_known_bad_commit = previous.first_known_bad_commit
        selection.previous_downstream_commit = previous.downstream_commit
        selection.previous_last_known_good_commit = previous.last_known_good_commit

    args.output_dir.mkdir(parents=True, exist_ok=True)

    def finalize(*, error: str | None = None) -> int:
        """Write selection.json and the human-readable summary, then return 0."""
        if error is not None:
            err_result = build_error_result(config, args.upstream_ref, error)
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

        clone_upstream(args.upstream_repo, upstream_dir)
        selection.target_commit = resolve_upstream_target(upstream_dir, args.upstream_ref)
        selection.downstream_commit = clone_downstream(config, downstream_dir)
        selection.pinned_commit = resolve_search_base_commit(
            project_dir=downstream_dir,
            dependency_name=config.dependency_name,
            upstream_dir=upstream_dir,
            last_known_good=None,
        )

        # Fast-path: if the target and downstream are identical to the last
        # verified-passing run, skip all probes and embed the result directly.
        skip_result = try_skip_already_good(
            skip_enabled=args.skip_already_good and config.skip_already_good,
            selection=selection,
            previous=previous,
            config=config,
            upstream_ref=args.upstream_ref,
        )
        if skip_result is not None:
            selection.pre_resolved_result = skip_result.to_json()
            return finalize()

        target_commit = selection.target_commit
        stored_last_known_good = previous.last_known_good_commit if previous else None

        # Compute a conservative candidate bisect window using the pinned commit
        # as the lower bound.  LKG verification requires hopscotch, so it runs
        # in the probe step (which re-derives the window with a verified LKG if
        # it turns out to be needed and valid).
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
