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
    build_commit_window,
    clone_downstream,
    clone_upstream,
    describe_commits,
    is_strict_ancestor,
    dependency_files_changed_between,
    next_release_tag_after,
    parent_commit,
    resolve_search_base_commit,
    resolve_tag,
    resolve_upstream_target,
    select_search_base_from_candidates,
)
from scripts.models import DownstreamConfig, Outcome, ValidationResult, WindowSelection, load_inventory
from scripts.storage import DownstreamStatusRecord, read_status_snapshot
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


def should_release_step(
    *,
    target_mode: str,
    pinned_commit: str | None,
    previous: DownstreamStatusRecord | None,
) -> bool:
    """Return whether the next-release target bound should apply this run.

    Release-stepping advances a *catching-up* downstream one release tag at a
    time instead of jumping to the upstream tip.  It is suppressed when:

    - the mode is not ``"next-release"`` (``"master"`` opts out, tracking the tip);
    - the pin could not be resolved (nothing to step from); or
    - a known-bad boundary is active.  A failing downstream is parked behind a
      break, so it must keep targeting the tip until it recovers — bounding the
      target back to a release that predates the break would pass and be misread
      as a recovery, discarding the stored boundary.  (``apply_result``'s
      ``target_before_fkb`` guard is the backstop for any producer that still
      targets below the break.)
    """
    if target_mode != "next-release":
        return False
    if not pinned_commit:
        return False
    return previous is None or previous.first_known_bad_commit is None


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
        skip_known_bad_bisect=config.skip_known_bad_bisect,
        revalidate_boundary=config.revalidate_boundary,
        nuke_lakedir=config.nuke_lakedir,
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

        # next-release (the default) bounds the target at the next release tag
        # after the current pin, so the downstream steps through releases and
        # never jumps over one.  When the pin is already at/past the newest tag,
        # the target stays at the upstream tip resolved above (track master until
        # a new tag lands).  The fresh bare clone above carries mathlib's tags.
        # Suppressed while a known-bad boundary is active — see should_release_step.
        if should_release_step(
            target_mode=config.target_mode,
            pinned_commit=selection.pinned_commit,
            previous=previous,
        ):
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
        elif config.target_mode == "next-release" and previous and previous.first_known_bad_commit:
            print(
                "target_mode=next-release: active known-bad boundary; tracking "
                f"{args.upstream_ref} HEAD until the downstream recovers."
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

        if (
            search_base_commit is not None
            and search_base_commit != target_commit
            and len(commit_window) <= 1
            and not is_strict_ancestor(upstream_dir, search_base_commit, target_commit)
        ):
            selection.search_base_not_ancestor = True
            print(
                f"::warning title=search-base-not-ancestor::[{config.name}] "
                f"pinned commit {search_base_commit[:12]} is not an ancestor of "
                f"target {target_commit[:12]} — no bisect window is available; "
                f"falling back to head-only probe"
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
