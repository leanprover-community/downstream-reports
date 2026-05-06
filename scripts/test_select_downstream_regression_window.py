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

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.conftest import PHYSLIB_CONFIG, make_selection
from scripts.models import Outcome
from scripts.select_downstream_regression_window import (
    build_parser as select_build_parser,
    try_skip_already_good,
)
from scripts.storage import DownstreamStatusRecord


class TrySkipAlreadyGoodTests(unittest.TestCase):
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
        self.assertIsNone(result)

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
        self.assertIsNone(result)

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
        self.assertIsNone(result)

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
        self.assertIsNone(result)

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
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, Outcome.PASSED)
        self.assertEqual(
            result.search_mode,
            "skipped-already-good",
            msg="search_mode is the report's distinguisher between fresh and skipped passes",
        )
        self.assertIn("already verified", selection.decision_reason)
        self.assertIn("Skip all probes", selection.next_action)


class SelectParserSkipAlreadyGoodTests(unittest.TestCase):
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
        self.assertTrue(args.skip_already_good)

    def test_skip_already_good_can_be_disabled(self) -> None:
        """``--no-skip-already-good`` forces a full validation."""
        # Arrange / Act
        args = select_build_parser().parse_args(
            [*self._REQUIRED, "--no-skip-already-good"]
        )

        # Assert
        self.assertFalse(args.skip_already_good)

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
        self.assertTrue(args.skip_already_good)


if __name__ == "__main__":
    unittest.main()
