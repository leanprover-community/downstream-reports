#!/usr/bin/env python3
"""
Tests for: scripts.select_downstream_regression_window

Coverage scope:
    - ``try_skip_already_good`` — the select-side skip heuristic that
      avoids re-validating a target commit already recorded as
      last-known-good when the downstream itself hasn't moved.
    - ``build_parser`` — pins the ``--skip-already-good`` /
      ``--no-skip-already-good`` CLI surface that the workflow
      dispatches on.

Out of scope:
    - Real cloning / DB reads done by ``main()``: the heuristic is the
      interesting unit; ``main()`` is glue exercised by the workflow
      itself.
    - Probe-side heuristics (``try_skip_known_bad_bisect``,
      ``run_culprit_probe``) — see
      ``test_probe_downstream_regression_window``.

Why this matters
----------------
``try_skip_already_good`` short-circuits the entire validation pipeline
for a downstream when nothing relevant has changed since the last run.
The two-field guard (``target_commit == last_known_good`` AND
``downstream_commit == previous.downstream_commit``) is what makes the
optimisation safe: relaxing either guard would skip a downstream whose
state has actually changed and silently propagate a stale outcome to
the report.
"""

from __future__ import annotations

from datetime import datetime, timezone

from scripts.conftest import PHYSLIB_CONFIG, make_selection
from scripts.models import Outcome
from scripts.select_downstream_regression_window import (
    boundary_bisect_overdue,
    try_skip_already_good,
)
from scripts.select_downstream_regression_window import (
    build_parser as select_build_parser,
)
from scripts.storage import DownstreamStatusRecord


class TestTrySkipAlreadyGood:
    """``try_skip_already_good`` — the select-side skip heuristic."""

    def test_returns_none_when_disabled(self) -> None:
        """When ``skip_enabled=False`` the heuristic is bypassed entirely.

        The CLI flag is the operator's escape hatch — passing
        ``--no-skip-already-good`` must force a full validation even
        when the heuristic's conditions would otherwise match.
        """
        # Arrange
        selection = make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit=selection.target_commit,
            downstream_commit=selection.downstream_commit,
        )

        # Act
        result = try_skip_already_good(
            skip_enabled=False,
            selection=selection,
            previous=previous,
            config=PHYSLIB_CONFIG,
            upstream_ref="master",
        )

        # Assert
        assert result is None

    def test_returns_none_when_no_previous(self) -> None:
        """Without a prior status record there is no baseline to compare against.

        First-ever run on a fresh database has ``previous=None``;
        skipping in that case would mean producing a "skipped"
        record before we have any actual run data — wrong.
        """
        # Arrange / Act
        result = try_skip_already_good(
            skip_enabled=True,
            selection=make_selection(),
            previous=None,
            config=PHYSLIB_CONFIG,
            upstream_ref="master",
        )

        # Assert
        assert result is None

    def test_returns_none_when_target_differs(self) -> None:
        """A different target commit means upstream advanced — must validate.

        The whole point of the regression workflow is to detect new
        breakages when upstream moves; the heuristic must not skip
        a commit it has never seen before, even if other guards
        would let it.
        """
        # Arrange
        selection = make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit="other" * 8,
            downstream_commit=selection.downstream_commit,
        )

        # Act
        result = try_skip_already_good(
            skip_enabled=True,
            selection=selection,
            previous=previous,
            config=PHYSLIB_CONFIG,
            upstream_ref="master",
        )

        # Assert
        assert result is None

    def test_returns_none_when_downstream_changed(self) -> None:
        """A new downstream commit invalidates the prior result.

        The downstream's own code changes can break or fix the build
        independently of upstream.  Skipping when only the upstream
        match holds (but the downstream moved) would silently carry
        a stale outcome forward.
        """
        # Arrange
        selection = make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit=selection.target_commit,
            downstream_commit="other" * 8,
        )

        # Act
        result = try_skip_already_good(
            skip_enabled=True,
            selection=selection,
            previous=previous,
            config=PHYSLIB_CONFIG,
            upstream_ref="master",
        )

        # Assert
        assert result is None

    def test_returns_passing_result_when_conditions_match(self) -> None:
        """Both guards match: emit a synthetic ``PASSED`` skip result.

        The synthetic result carries ``search_mode="skipped-already-good"``
        so the report's "Previously Tested" section can render it
        distinctly from a fresh pass.  The selection's
        ``decision_reason`` and ``next_action`` are populated so the
        select job's summary explains why no probe ran.
        """
        # Arrange
        selection = make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit=selection.target_commit,
            downstream_commit=selection.downstream_commit,
        )

        # Act
        result = try_skip_already_good(
            skip_enabled=True,
            selection=selection,
            previous=previous,
            config=PHYSLIB_CONFIG,
            upstream_ref="master",
        )

        # Assert
        assert result is not None
        assert result.outcome == Outcome.PASSED
        assert result.search_mode == "skipped-already-good", "search_mode is the report's distinguisher between fresh and skipped passes"
        assert "already verified" in selection.decision_reason
        assert "Skip all probes" in selection.next_action


class TestBoundaryBisectOverdue:
    """``boundary_bisect_overdue`` — the staleness valve's age comparison.

    The valve bounds how long boundary revalidation may keep confirming a
    stored pair without a real bisect re-deriving it.  A wrong False keeps a
    potentially misleading pair alive past its deadline; a wrong True only
    costs one redundant bisect.
    """

    _NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)

    def test_recent_bisect_is_not_overdue(self) -> None:
        """A bisect inside the age window keeps revalidation available."""
        assert (
            boundary_bisect_overdue("2026-06-08T00:00:00Z", max_age_days=7, now=self._NOW)
            is False
        )

    def test_old_bisect_is_overdue(self) -> None:
        """A bisect past the age window forces the next run to re-bisect."""
        assert (
            boundary_bisect_overdue("2026-06-01T00:00:00Z", max_age_days=7, now=self._NOW)
            is True
        )

    def test_missing_timestamp_is_overdue(self) -> None:
        """No fresh bisect on record → overdue.

        Covers boundaries that predate the valve (no run_result row with
        search_mode='bisect') and first runs after enabling revalidation —
        both start with a real bisect before any revalidation may fire.
        """
        assert boundary_bisect_overdue(None, max_age_days=7, now=self._NOW) is True

    def test_naive_timestamp_is_read_as_utc(self) -> None:
        """A timestamp without timezone info compares as UTC.

        SQLite (used in tests and local dev) stores DateTime columns
        naively; production Postgres returns aware values.  Both must
        compare against the same clock.
        """
        assert (
            boundary_bisect_overdue("2026-06-08T00:00:00", max_age_days=7, now=self._NOW)
            is False
        )


class TestSelectParserSkipAlreadyGood:
    """``select_build_parser()`` — ``--skip-already-good`` flag surface."""

    _REQUIRED = ["--workdir", "/tmp", "--output-dir", "/tmp"]

    def test_skip_already_good_defaults_to_true(self) -> None:
        """The heuristic is opt-out — engaged by default.

        Most downstreams benefit from the skip, so it defaults on; the
        flag exists for downstreams whose state-tracking is unreliable
        (or for forced full re-runs from the operator).
        """
        # Arrange / Act
        args = select_build_parser().parse_args(self._REQUIRED)

        # Assert
        assert args.skip_already_good

    def test_skip_already_good_can_be_disabled(self) -> None:
        """``--no-skip-already-good`` forces a full validation."""
        # Arrange / Act
        args = select_build_parser().parse_args(
            [*self._REQUIRED, "--no-skip-already-good"]
        )

        # Assert
        assert not args.skip_already_good

    def test_max_boundary_age_days_defaults_and_overrides(self) -> None:
        """``--max-boundary-age-days`` defaults to 7 and can be tuned per dispatch.

        Pinning the default here means changing the staleness valve's
        cadence requires updating this test — the weekly full bisect is a
        correctness backstop, not just a tuning knob.
        """
        # Arrange / Act / Assert — default
        args = select_build_parser().parse_args(self._REQUIRED)
        assert args.max_boundary_age_days == 7

        # Act / Assert — override
        args = select_build_parser().parse_args(
            [*self._REQUIRED, "--max-boundary-age-days", "3"]
        )
        assert args.max_boundary_age_days == 3

    def test_skip_already_good_can_be_explicitly_enabled(self) -> None:
        """``--skip-already-good`` is accepted as an explicit confirmation.

        Passing the flag explicitly is harmless and useful for
        operators who want their dispatch to be self-documenting.
        """
        # Arrange / Act
        args = select_build_parser().parse_args(
            [*self._REQUIRED, "--skip-already-good"]
        )

        # Assert
        assert args.skip_already_good
