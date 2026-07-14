"""Shared domain types for the downstream regression workflow."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

# The canonical mathlib release-tag shape, shared by the select/probe,
# aggregation, and site-rendering paths.  Matches a final (v4.32.0) or release
# candidate (v4.32.0-rc1); excludes daily/nightly tags (master-2026-04-15,
# nightly-*) and patched re-tags (v4.14.0-patch1, v4.32.0-rc1-patch1) — the
# trailing anchor is what rejects the patched re-tags.  Groups 1-3 are
# major/minor/patch for callers that sort by version.
RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-rc\d+)?$")


class Outcome(str, Enum):
    """Possible outcomes for one downstream validation attempt."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True)
class DownstreamConfig:
    """Inventory entry for one downstream repository."""

    name: str
    repo: str
    default_branch: str
    dependency_name: str = "mathlib"
    enabled: bool = True
    # Which upstream commit the regression pipeline advances this downstream
    # toward.  "next-release" (default): target the next semver release tag
    # (including prereleases, e.g. v4.32.0 or v4.32.0-rc1) that is a descendant
    # of the current pin, so the downstream steps through releases and never
    # jumps over one in a single bump; once the pin is at/past the newest tag,
    # fall back to the upstream default-branch tip (track master until a new tag
    # lands, which is also where actively-bumped downstreams already sit).
    # "master": always target the tip, the older behavior that may advance past
    # a release tag without stopping at it.
    target_mode: str = "next-release"
    bumping_branch: str | None = None
    skip_already_good: bool = True
    skip_known_bad_bisect: bool = True
    # When True, a failing HEAD probe on a changed downstream re-validates the
    # stored (LKG, FKB) pair — one build each — instead of re-bisecting the
    # whole window.  The monotonicity assumption (downstream source changes
    # don't move the regression boundary) is only trusted when the
    # downstream's lake-manifest.json is unchanged since the last validated
    # run; any manifest change (a dependency bump) disables the shortcut for
    # that run.  A full bisect still runs whenever the revalidation fails
    # (stored LKG now fails, or stored FKB now passes).  Enable for
    # actively-developed downstreams in long failing episodes, where
    # try_skip_known_bad_bisect never fires because downstream_commit moves
    # between runs.
    revalidate_boundary: bool = False
    warm_cache: bool = False
    # When True, the probe step sets HOPSCOTCH_DEBUG_NUKE_LAKEDIR=1 in the
    # hopscotch subprocess environment.  Hopscotch then wipes <projectDir>/.lake
    # (preserving .lake/hopscotch/) before every probe and forces the bump step
    # to re-run.  Enable for downstreams whose culprit log shows a stale-artifact
    # symptom such as "ProofWidgets not up-to-date" that survives across probes
    # and causes bisect to walk into a false culprit.
    nuke_lakedir: bool = False
    # Optional verify steps hopscotch runs after `lake build` on every probe
    # (hopscotch's --test / --lint).  `lake build` always runs; when these are
    # set, `lake test` / `lake lint` must also pass for a commit to count as
    # good, so the regression search becomes sensitive to test/lint breakage and
    # not just build breakage.  Both opt-in: enable only for downstreams with a
    # test/lint driver wired up — hopscotch aborts a run whose enabled step has
    # no driver.
    run_test: bool = False
    run_lint: bool = False
    # Extra arguments forwarded to each verify step's underlying `lake`
    # invocation (hopscotch's --build-args / --test-args / --lint-args).  Each
    # list element is one argument token; they are joined with single spaces on
    # the command line and hopscotch re-splits on whitespace, so a single token
    # cannot itself contain a space.  build_args apply to the always-run
    # `lake build`; test_args / lint_args apply only when run_test / run_lint is
    # set.  All default empty (no extra arguments).
    build_args: list[str] = field(default_factory=list)
    test_args: list[str] = field(default_factory=list)
    lint_args: list[str] = field(default_factory=list)
    # When True, the manifest-watcher (.github/workflows/manifest-watcher.yml,
    # cron */15) inspects this downstream every 15 min and dispatches a
    # targeted regression-report run when its lake-manifest.json pin moves
    # to or past first_known_bad_commit.  Default False (opt-in) so the
    # watcher only spends API calls on downstreams that actively bump-track.
    watch_manifest: bool = False
    # Labels passed verbatim to the probe job's `runs-on:` directive.
    # Default is the self-hosted PR pool.  Override (e.g. `["ubuntu-latest"]`)
    # for downstreams whose build needs something the self-hosted image lacks
    # — currently Robo, which depends on a populated `/usr/share/zoneinfo`
    # database for `Std.Time` lookups during `MakeGame` elaboration.
    runs_on: list[str] = field(default_factory=lambda: ["self-hosted", "pr"])

    def __post_init__(self) -> None:
        valid_target_modes = ("master", "next-release")
        if self.target_mode not in valid_target_modes:
            raise ValueError(
                f"{self.name}: invalid target_mode {self.target_mode!r} "
                f"(expected one of {valid_target_modes})"
            )


@dataclass(frozen=True)
class CommitDetail:
    """One upstream commit plus the title shown in reports."""

    sha: str
    title: str


@dataclass
class WindowSelection:
    """Persisted output from the pre-probe window-selection step."""

    schema_version: int = 1
    # True when a multi-commit bisect window is available.  In both regression
    # and on-demand workflows the probe job always runs; this field tells the
    # probe step whether to attempt a bisect after the HEAD probe.
    has_bisect_window: bool = False
    downstream: str | None = None
    repo: str | None = None
    default_branch: str | None = None
    dependency_name: str = "mathlib"
    downstream_commit: str | None = None
    upstream_ref: str | None = None
    target_commit: str | None = None
    search_mode: str = "head-only"
    tested_commits: list[str] = field(default_factory=list)
    tested_commit_details: list[CommitDetail] = field(default_factory=list)
    commit_window_truncated: bool = False
    head_probe_outcome: str | None = None
    head_probe_failure_stage: str | None = None
    head_probe_summary: str | None = None
    pinned_commit: str | None = None
    selected_lower_bound_commit: str | None = None
    # True when the selected lower-bound commit is not an ancestor of the target,
    # making a bisect window impossible regardless of window size.
    search_base_not_ancestor: bool = False
    decision_reason: str | None = None
    next_action: str | None = None
    # `--from`/`--to` refs for the bisect probe step.  Computed by the window-
    # selection step (which has the local mathlib clone) and stored here so the
    # probe step can invoke the tool without its own mathlib clone.
    probe_from_ref: str | None = None
    probe_to_ref: str | None = None
    # Prior episode state from the database, embedded by the select step so the
    # probe step can apply skip heuristics without a database connection.
    previous_first_known_bad_commit: str | None = None
    previous_downstream_commit: str | None = None
    previous_last_known_good_commit: str | None = None
    # When the select step already resolved the final result (e.g. skip-already-
    # good fired), the serialised ValidationResult payload is stored here.  The
    # probe step writes it directly to result.json without invoking hopscotch.
    pre_resolved_result: dict[str, Any] | None = None
    # Per-downstream skip flag from the inventory, forwarded so the probe step
    # respects inventory-level overrides without access to the inventory file.
    skip_known_bad_bisect: bool = True
    # Per-downstream boundary-revalidation flag from the inventory, forwarded
    # like skip_known_bad_bisect.  See DownstreamConfig.revalidate_boundary.
    revalidate_boundary: bool = False
    # Whether any of the downstream's dependency files (lake-manifest.json,
    # lean-toolchain — see git_ops.DEPENDENCY_FILES) differ between the
    # previously-validated downstream commit and the current one.  Computed by
    # the select step (which has the downstream clone); None when there is no
    # prior commit to compare against or the comparison could not be made.
    # The probe step only applies boundary revalidation when this is False.
    dependency_files_changed_since_last_run: bool | None = None
    # True when the stored boundary is due a scheduled fresh bisect: the most
    # recent search_mode='bisect' run is older than the select step's
    # --max-boundary-age-days, or no fresh bisect is recorded at all.  The
    # probe step skips boundary revalidation when set, so a real bisect
    # re-derives the pair on a bounded cadence — the staleness valve that
    # caps how long a confirmable-but-misleading boundary can persist.
    boundary_bisect_overdue: bool = False
    # Per-downstream nuke-lakedir flag from the inventory, forwarded so the
    # probe step can set HOPSCOTCH_DEBUG_NUKE_LAKEDIR=1 without re-reading the
    # inventory file.  See DownstreamConfig.nuke_lakedir.
    nuke_lakedir: bool = False
    # Per-downstream verify-step and build-argument settings from the inventory,
    # forwarded so the probe step can pass hopscotch's --test / --lint /
    # --build-args / --test-args / --lint-args without re-reading the inventory.
    # See DownstreamConfig.run_test / build_args.
    run_test: bool = False
    run_lint: bool = False
    build_args: list[str] = field(default_factory=list)
    test_args: list[str] = field(default_factory=list)
    lint_args: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WindowSelection":
        """Decode one persisted selection payload."""

        field_names = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in payload.items() if k in field_names}
        # Handle nested CommitDetail objects.
        if "tested_commit_details" in kwargs:
            kwargs["tested_commit_details"] = [
                CommitDetail(**detail) for detail in kwargs["tested_commit_details"]
            ]
        return cls(**kwargs)

    def to_json(self) -> dict[str, Any]:
        """Serialize the selection using plain JSON-compatible values."""

        return asdict(self)


# DownstreamConfig fields that describe the downstream's identity rather than
# per-run tool behaviour.  They are mapped explicitly when a WindowSelection is
# constructed (``downstream`` ↔ ``name``; ``default_branch`` may be overridden
# on the on-demand path), so the name-driven forwarding below excludes them.
_CONFIG_IDENTITY_FIELDS = frozenset({"repo", "default_branch", "dependency_name"})


def forwarded_config_fields() -> tuple[str, ...]:
    """Names of the tool-config fields forwarded config → selection → probe.

    Derived as the field names ``DownstreamConfig`` and ``WindowSelection``
    share, minus the identity fields: declaring a new per-downstream tool
    flag on both dataclasses is all it takes for the select scripts to
    forward it and for the probe job to read it back — no per-script
    plumbing.
    """

    config_names = {f.name for f in dataclasses.fields(DownstreamConfig)}
    selection_names = {f.name for f in dataclasses.fields(WindowSelection)}
    return tuple(sorted((config_names & selection_names) - _CONFIG_IDENTITY_FIELDS))


def apply_config_forwarding(
    selection: WindowSelection,
    config: DownstreamConfig,
    *,
    exclude: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Copy every forwarded tool-config field from ``config`` onto ``selection``.

    ``exclude`` names forwarded fields to leave at their selection defaults —
    the on-demand select leg uses it to keep ``revalidate_boundary`` off
    (the bumping branch moves the manifest by design, so the heuristic's
    manifest-unchanged guard would reject nearly every run).
    """

    for field_name in forwarded_config_fields():
        if field_name in exclude:
            continue
        setattr(selection, field_name, getattr(config, field_name))


def config_from_selection(selection: WindowSelection) -> DownstreamConfig:
    """Reconstruct the probe job's ``DownstreamConfig`` from a selection.

    The probe job has no inventory file; the identity fields plus every
    forwarded tool-config field come back out of ``selection.json``.
    Callers must ensure ``downstream``/``repo``/``default_branch`` are set.
    """

    forwarded = {name: getattr(selection, name) for name in forwarded_config_fields()}
    return DownstreamConfig(
        name=selection.downstream,
        repo=selection.repo,
        default_branch=selection.default_branch,
        dependency_name=selection.dependency_name,
        **forwarded,
    )


def describe_verify_commands(
    *,
    build_args: Sequence[str] = (),
    run_test: bool = False,
    test_args: Sequence[str] = (),
    run_lint: bool = False,
    lint_args: Sequence[str] = (),
) -> list[str]:
    """The ``lake`` commands hopscotch verifies a commit with, in run order.

    ``lake build`` always runs (with ``build_args`` appended); ``lake test`` and
    ``lake lint`` follow only when their verify step is enabled, each with its
    own arguments.  Naming the exact recipe distinguishes a warning promoted to
    an error (e.g. ``--wfail``) from a genuine build break.  The default recipe
    is ``["lake build"]``; reporters show it only when it differs.
    """

    def _cmd(name: str, extra: Sequence[str]) -> str:
        return f"lake {name}" + (" " + " ".join(extra) if extra else "")

    commands = [_cmd("build", build_args)]
    if run_test:
        commands.append(_cmd("test", test_args))
    if run_lint:
        commands.append(_cmd("lint", lint_args))
    return commands


DEFAULT_VERIFY_COMMANDS: list[str] = ["lake build"]

# Ordered verify steps, matching hopscotch's build → test → lint sequence.  The
# chain stops at the first failing step, so anything after it never ran.
_VERIFY_STEP_ORDER: tuple[str, ...] = ("build", "test", "lint")

# Per-step status words for the verify summary.
VERIFY_STATUS_PASSED = "passed"
VERIFY_STATUS_FAILED = "failed"
VERIFY_STATUS_NOT_RUN = "not run"  # an earlier step failed, so this one never ran


def _command_step(command: str) -> str | None:
    """The verify step a rendered ``lake <step> …`` command belongs to."""

    parts = command.split()
    return parts[1] if len(parts) > 1 and parts[1] in _VERIFY_STEP_ORDER else None


def _failed_step(failure_stage: str | None) -> str | None:
    """Map hopscotch's ``failureStage`` to a verify step, or None.

    ``failureStage`` is a command-shaped string (e.g. ``"lake build"``); the
    setup/runner error stages carry no step, so they map to None.
    """

    if not failure_stage:
        return None
    tokens = failure_stage.split()
    for step in _VERIFY_STEP_ORDER:
        if step in tokens:
            return step
    return None


def annotate_verify_commands(
    commands: Sequence[str],
    *,
    outcome: str | None = None,
    failure_stage: str | None = None,
) -> list[tuple[str | None, str]]:
    """Pair each verify command with a status word (or None when unknown).

    A passing run marks every step ``passed``.  A failing run whose stage
    localises to a step marks the steps before it ``passed``, that step
    ``failed``, and any steps after it ``not run`` (the chain stopped, so they
    never ran).  When the outcome is an error, or a failure's stage doesn't
    localise to a listed step, the status is left None rather than guessed — the
    caller renders the bare command.
    """

    steps = [_command_step(command) for command in commands]
    failed = _failed_step(failure_stage)
    localised_failure = outcome == "failed" and failed in steps

    annotated: list[tuple[str | None, str]] = []
    seen_failure = False
    for command, step in zip(commands, steps):
        if outcome == "passed":
            status: str | None = VERIFY_STATUS_PASSED
        elif localised_failure:
            if step == failed:
                status = VERIFY_STATUS_FAILED
                seen_failure = True
            elif seen_failure:
                status = VERIFY_STATUS_NOT_RUN
            else:
                status = VERIFY_STATUS_PASSED
        else:
            status = None
        annotated.append((status, command))
    return annotated


def render_verify_summary(
    *,
    build_args: Sequence[str] = (),
    run_test: bool = False,
    test_args: Sequence[str] = (),
    run_lint: bool = False,
    lint_args: Sequence[str] = (),
    outcome: str | None = None,
    failure_stage: str | None = None,
    label: str = "Verify",
) -> str | None:
    """A multi-line ``- <label>:`` step summary, or None for the default recipe.

    One indented bullet per verify command, tagged ``passed``/``failed``/``not
    run`` when the outcome localises a failing step (see
    ``annotate_verify_commands``).  Suppressed for the plain ``lake build`` recipe.
    """

    commands = describe_verify_commands(
        build_args=build_args,
        run_test=run_test,
        test_args=test_args,
        run_lint=run_lint,
        lint_args=lint_args,
    )
    if commands == DEFAULT_VERIFY_COMMANDS:
        return None
    annotated = annotate_verify_commands(
        commands, outcome=outcome, failure_stage=failure_stage
    )
    lines = [f"- {label}:"]
    for status, command in annotated:
        suffix = f": {status}" if status else ""
        lines.append(f"  - `{command}`{suffix}")
    return "\n".join(lines)


def render_verify_summary_from_record(
    record: dict[str, Any], *, label: str = "Verify"
) -> str | None:
    """``render_verify_summary`` keyed off a serialised result dict.

    Pulls the verify-recipe fields from a report row or alert record (the
    serialised ``RunResultRecord`` / ``ValidationResult`` shape), so the markdown
    report and the Zulip formatters share one extraction point.
    """
    return render_verify_summary(
        build_args=record.get("build_args") or [],
        run_test=record.get("run_test", False),
        test_args=record.get("test_args") or [],
        run_lint=record.get("run_lint", False),
        lint_args=record.get("lint_args") or [],
        outcome=record.get("outcome"),
        failure_stage=record.get("failure_stage"),
        label=label,
    )


@dataclass
class ValidationResult:
    """Machine-readable result for one downstream validation run."""

    schema_version: int
    downstream: str
    repo: str
    default_branch: str
    downstream_commit: str | None
    dependency_name: str
    upstream_ref: str
    target_commit: str | None
    tested_commits: list[str]
    commit_window_truncated: bool
    outcome: Outcome
    failure_stage: str | None
    first_failing_commit: str | None
    last_successful_commit: str | None
    summary: str
    error: str | None
    generated_at: str
    search_mode: str = "head-only"
    tested_commit_details: list[CommitDetail] = field(default_factory=list)
    head_probe_outcome: str | None = None
    head_probe_failure_stage: str | None = None
    head_probe_summary: str | None = None
    pinned_commit: str | None = None
    search_base_not_ancestor: bool = False
    # The verify recipe hopscotch ran, copied from the downstream's config by the
    # result builders so reports can name the exact command (see
    # `describe_verify_commands`).  Defaults to the plain `lake build` recipe.
    run_test: bool = False
    run_lint: bool = False
    build_args: list[str] = field(default_factory=list)
    test_args: list[str] = field(default_factory=list)
    lint_args: list[str] = field(default_factory=list)
    # The fixes hopscotch recorded for the boundary, carried verbatim from its
    # results.json `proposedFixes` so the bump action can overlay them onto its
    # own run and `hopscotch fix apply`.  Each entry keeps hopscotch's own object
    # shape and is treated opaquely (fix-type-agnostic).  Empty when none were
    # recorded.
    proposed_fixes: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Serialize the result using plain JSON-compatible values."""

        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload


def utc_now() -> str:
    """Return a stable UTC timestamp string."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_inventory(path: Path, *, include_disabled: bool = False) -> dict[str, DownstreamConfig]:
    """Load the JSON inventory and index it by downstream name.

    By default only enabled entries are returned.  Pass ``include_disabled=True``
    to include every entry regardless of the ``enabled`` flag.
    """

    payload = json.loads(path.read_text())
    entries = payload.get("downstreams", [])
    return {
        entry["name"]: DownstreamConfig(**entry)
        for entry in entries
        if include_disabled or entry.get("enabled", True)
    }
