#!/usr/bin/env python3
"""Select the downstream regression bisect window before any probe run.

This script handles only git work and never invokes hopscotch.  Prior
episode state comes from the status-snapshot file staged by the plan job
(``--status-snapshot``), never from a database connection.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.git_ops import (
    clone_downstream,
    clone_upstream,
    dependency_files_changed_between,
    next_release_tag_after,
    resolve_search_base_commit,
    resolve_tag,
    resolve_upstream_target,
)
from scripts.models import (
    DownstreamConfig,
    Outcome,
    ValidationResult,
    WindowSelection,
    apply_config_forwarding,
    load_inventory,
)
from scripts.select_window import (
    embed_prior_state,
    finalize_selection,
    populate_selection_window,
)
from scripts.storage import DownstreamStatusRecord, read_status_snapshot
from scripts.validation import build_skip_result

# ---------------------------------------------------------------------------
# Boundary-revalidation staleness valve
# ---------------------------------------------------------------------------


def boundary_bisect_overdue(
    last_fresh_bisect_at: str | None,
    *,
    max_age_days: int,
    now: datetime | None = None,
) -> bool:
    """Return whether the stored boundary is due a scheduled fresh bisect.

    Boundary revalidation can keep confirming a (LKG, FKB) pair indefinitely
    — including a pair that is literally true but no longer the commit that
    actually blocks HEAD (e.g. the original breakage was fixed upstream while
    new downstream code broke against a later commit).  Forcing a real bisect
    once the last one is older than *max_age_days* caps how long such a pair
    can persist.  A missing timestamp (no fresh bisect recorded) counts as
    overdue.
    """
    if last_fresh_bisect_at is None:
        return True
    moment = datetime.fromisoformat(last_fresh_bisect_at.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    current = now if now is not None else datetime.now(timezone.utc)
    return current - moment > timedelta(days=max_age_days)


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
    parser.add_argument(
        "--max-boundary-age-days", type=int, default=7,
        help=(
            "Staleness valve for boundary revalidation: when the downstream's "
            "most recent fresh bisect is older than this many days (or none is "
            "recorded), the probe runs a full bisect instead of revalidating "
            "the stored boundary."
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

    status: dict[str, DownstreamStatusRecord] = {}
    if args.status_snapshot is not None:
        status = read_status_snapshot(
            args.status_snapshot, workflow=args.workflow, upstream=args.upstream,
        )
    previous = status.get(config.name)

    selection = WindowSelection(
        downstream=config.name,
        repo=config.repo,
        default_branch=config.default_branch,
        dependency_name=config.dependency_name,
        upstream_ref=args.upstream_ref,
    )
    apply_config_forwarding(selection, config)
    embed_prior_state(selection, previous)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    def finalize(*, error: str | None = None) -> int:
        return finalize_selection(
            selection=selection,
            config=config,
            output_dir=args.output_dir,
            upstream_ref=args.upstream_ref,
            error=error,
        )

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

        # next-release (the default) bounds the target at the next release tag
        # after the current pin, so the downstream steps through releases and
        # never jumps over one.  When the pin is already at/past the newest tag,
        # the target stays at the upstream tip resolved above (track master until
        # a new tag lands).  The fresh bare clone above carries mathlib's tags.
        if config.target_mode == "next-release" and selection.pinned_commit:
            next_tag = next_release_tag_after(upstream_dir, selection.pinned_commit)
            if next_tag is not None:
                selection.target_commit = resolve_tag(upstream_dir, next_tag)
                print(
                    f"target_mode=next-release: bounding target at {next_tag} "
                    f"({selection.target_commit[:9]}) instead of {args.upstream_ref} HEAD."
                )
            else:
                print(
                    "target_mode=next-release: no release tag after the pin; "
                    f"tracking {args.upstream_ref} HEAD."
                )

        # Boundary revalidation (probe step) is only sound while the
        # downstream's dependency context is unchanged; compare the
        # dependency-file blobs (lake-manifest.json, lean-toolchain) against
        # the previously-validated downstream commit so the probe can gate on
        # it.  Skipped (left None — revalidation stays off) when the heuristic
        # is not enabled for this downstream or there is no prior commit to
        # compare against.
        if (
            config.revalidate_boundary
            and previous is not None
            and previous.downstream_commit is not None
            and selection.downstream_commit is not None
        ):
            selection.dependency_files_changed_since_last_run = dependency_files_changed_between(
                downstream_dir,
                previous.downstream_commit,
                selection.downstream_commit,
            )
            selection.boundary_bisect_overdue = boundary_bisect_overdue(
                previous.last_fresh_bisect_at,
                max_age_days=args.max_boundary_age_days,
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

        populate_selection_window(
            selection=selection,
            downstream_name=config.name,
            upstream_dir=upstream_dir,
            stored_last_known_good=previous.last_known_good_commit if previous else None,
            max_commits=args.max_commits,
            output_dir=args.output_dir,
        )

    except Exception as error:  # pragma: no cover
        return finalize(error=str(error))

    return finalize()


if __name__ == "__main__":
    raise SystemExit(main())
