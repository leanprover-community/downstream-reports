#!/usr/bin/env python3
"""Select the bisect window for a downstream on-demand probe.

This script handles only git work and never invokes hopscotch.  Prior
episode state comes from the status-snapshot file staged by the plan job
(``--status-snapshot``), never from a database connection.
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
    clone_downstream,
    clone_upstream,
    pinned_commit_from_manifest,
    resolve_upstream_target,
)
from scripts.models import WindowSelection, apply_config_forwarding, load_inventory
from scripts.select_window import (
    embed_prior_state,
    finalize_selection,
    populate_selection_window,
)
from scripts.storage import DownstreamStatusRecord, read_status_snapshot
from scripts.validation import build_error_result


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
    parser.add_argument(
        "--status-snapshot", type=Path, default=None,
        help=(
            "Status-snapshot file staged by the plan job; prior episode state "
            "is read from it instead of any database.  Omit to run with no "
            "prior state (every downstream is treated as first-run)."
        ),
    )
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
        return finalize_selection(
            selection=selection,
            config=config,
            output_dir=args.output_dir,
            upstream_ref="unknown",
        )

    status: dict[str, DownstreamStatusRecord] = {}
    if args.status_snapshot is not None:
        status = read_status_snapshot(
            args.status_snapshot,
            workflow=args.workflow,
            upstream="leanprover-community/mathlib4",
        )
    previous = status.get(config.name)
    stored_last_known_good = previous.last_known_good_commit if previous else None

    # Clone the target branch instead of default_branch.
    ondemand_config = dataclasses.replace(config, default_branch=effective_branch)

    # selection.default_branch is set to the target branch so that the probe
    # step (which reconstructs DownstreamConfig from selection.json) also
    # clones the correct branch when running the bisect.
    # revalidate_boundary stays at its False default: the bumping
    # branch exists to move lake-manifest.json, so the manifest-unchanged
    # guard behind boundary revalidation would reject nearly every run.
    selection = WindowSelection(
        downstream=config.name,
        repo=config.repo,
        default_branch=effective_branch,
        dependency_name=config.dependency_name,
    )
    apply_config_forwarding(selection, config, exclude={"revalidate_boundary"})
    embed_prior_state(selection, previous)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    def finalize(*, error: str | None = None) -> int:
        return finalize_selection(
            selection=selection,
            config=config,
            output_dir=args.output_dir,
            upstream_ref=selection.upstream_ref or "unknown",
            error=error,
        )

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

        # The lake-manifest.json pin is preferred as the lower bound — it
        # records the exact mathlib SHA the downstream last fetched, which
        # predates any regression on the bumping branch.  The stored
        # last-known-good is the fallback when no manifest pin is available.
        populate_selection_window(
            selection=selection,
            downstream_name=config.name,
            upstream_dir=upstream_dir,
            stored_last_known_good=stored_last_known_good,
            max_commits=args.max_commits,
            output_dir=args.output_dir,
        )

    except Exception as error:  # pragma: no cover
        return finalize(error=str(error))

    return finalize()


if __name__ == "__main__":
    raise SystemExit(main())
