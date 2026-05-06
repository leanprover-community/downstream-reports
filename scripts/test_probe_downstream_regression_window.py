#!/usr/bin/env python3
"""
Tests for: scripts.probe_downstream_regression_window

Coverage scope:
    - ``try_skip_known_bad_bisect`` — the probe-side skip heuristic
      that re-uses a stored culprit when a HEAD probe fails on a
      target whose ancestry contains the previously-recorded
      ``first_known_bad`` commit.
    - ``run_culprit_probe`` — the follow-up build of the stored
      culprit that captures fresh failure logs in this job's
      artifacts (rather than back-linking to an older run).
    - ``build_parser`` — pins the ``--skip-known-bad-bisect`` /
      ``--no-skip-known-bad-bisect`` and ``--max-commits`` CLI
      surface.

Out of scope:
    - HEAD probe execution: tests synthesise the
      ``CompletedProcess`` and ``head_probe_state`` dict the heuristic
      consumes.
    - The actual hopscotch invocation in ``run_culprit_probe``:
      ``run_validation_attempt`` is patched at the module boundary.

Why this matters
----------------
The known-bad-bisect skip is the single largest cost saver in the
regression workflow: a fresh bisect can take 30+ minutes, and
re-confirming an unchanged regression is exactly what the skip
avoids.  Three guards make it safe — ``first_known_bad`` set,
downstream commit unchanged, ``first_known_bad`` is still a strict
ancestor of the current target.  Loosening any guard would re-attribute
a regression to the wrong commit.  ``run_culprit_probe`` is then the
contract that the failure log is attached to *this* run's artifacts,
so the alert payload's culprit-log link works without a back-link.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.conftest import PHYSLIB_CONFIG, make_selection
from scripts.models import CommitDetail, Outcome
from scripts.probe_downstream_regression_window import (
    build_parser as probe_build_parser,
    run_culprit_probe,
    try_skip_known_bad_bisect,
)
from scripts.storage import DownstreamStatusRecord


class TrySkipKnownBadBisectTests(unittest.TestCase):
    """``try_skip_known_bad_bisect`` — the probe-side skip heuristic."""

    _head_probe_run = subprocess.CompletedProcess(
        ["tool"], 1, stdout="failed", stderr="failed"
    )
    _head_probe_state: dict = {
        "failureStage": "lake build",
        "firstFailingCommit": "t" * 40,
    }

    def _call(
        self,
        *,
        skip_enabled: bool = True,
        previous: DownstreamStatusRecord | None = None,
        selection=None,
        ancestor: bool = True,
    ):
        """Invoke the heuristic with ``is_strict_ancestor`` patched.

        The heuristic queries git directly to check whether the stored
        ``first_known_bad`` is still in the current target's ancestry;
        this helper patches that boundary so tests don't need a real
        upstream clone on disk.
        """
        selection = selection or make_selection(
            head_probe_outcome="failed",
            head_probe_failure_stage="build",
            head_probe_summary="failed",
            tested_commit_details=[CommitDetail(sha="t" * 40, title="test")],
        )
        with patch(
            "scripts.probe_downstream_regression_window.is_strict_ancestor",
            return_value=ancestor,
        ):
            return (
                try_skip_known_bad_bisect(
                    skip_enabled=skip_enabled,
                    selection=selection,
                    previous=previous,
                    config=PHYSLIB_CONFIG,
                    upstream_ref="master",
                    upstream_dir=Path("/dummy"),
                    head_probe_run=self._head_probe_run,
                    head_probe_state=self._head_probe_state,
                    head_probe_summary_text="failed",
                ),
                selection,
            )

    def test_returns_none_when_disabled(self) -> None:
        """``skip_enabled=False`` bypasses the heuristic entirely.

        ``--no-skip-known-bad-bisect`` exists so an operator can force
        a fresh bisect when they suspect the stored culprit has gone
        stale (e.g. a known-bad commit was reverted upstream).
        """
        # Arrange
        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="d" * 40,
        )

        # Act
        result, _ = self._call(skip_enabled=False, previous=previous)

        # Assert
        self.assertIsNone(result)

    def test_returns_none_when_no_first_known_bad(self) -> None:
        """Without a prior ``first_known_bad`` there is no culprit to reuse.

        The heuristic re-attributes the current failure to the stored
        culprit; with no stored culprit there is nothing to attribute
        to, so the probe must run a fresh bisect.
        """
        # Arrange
        previous = DownstreamStatusRecord(downstream_commit="d" * 40)

        # Act
        result, _ = self._call(previous=previous)

        # Assert
        self.assertIsNone(result)

    def test_returns_none_when_downstream_changed(self) -> None:
        """A new downstream commit invalidates the stored culprit attribution.

        The previously-recorded culprit was attributed against the old
        downstream code; if the downstream has changed, the same
        upstream commit might now break for a different reason or not
        at all.  Re-bisecting is the only safe answer.
        """
        # Arrange
        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="other" * 8,
        )

        # Act
        result, _ = self._call(previous=previous)

        # Assert
        self.assertIsNone(result)

    def test_returns_none_when_not_ancestor(self) -> None:
        """The stored culprit is no longer an ancestor — must re-bisect.

        If the stored ``first_known_bad`` is not a strict ancestor of
        the current target, the regression boundary may have moved
        (e.g. upstream reverted the breaking change).  Skipping here
        would incorrectly persist a stale culprit.
        """
        # Arrange
        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="d" * 40,
        )

        # Act
        result, _ = self._call(previous=previous, ancestor=False)

        # Assert
        self.assertIsNone(result)

    def test_returns_failing_result_when_conditions_match(self) -> None:
        """All three guards match: emit a ``head-only-known-bad`` failed result.

        The synthetic result carries the stored culprit as
        ``first_failing_commit`` so the report can render it; the
        ``search_mode`` tag distinguishes a skipped-bisect failure
        from a fresh-bisect failure in the report.
        """
        # Arrange
        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="d" * 40,
        )

        # Act
        result, selection = self._call(previous=previous)

        # Assert
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertEqual(
            result.search_mode,
            "head-only-known-bad",
            msg="search_mode tag distinguishes skipped-bisect from fresh-bisect failures",
        )
        self.assertIn("Re-bisecting", selection.decision_reason)
        self.assertIn("Skip the bisect", selection.next_action)


class RunCulpritProbeTests(unittest.TestCase):
    """``run_culprit_probe`` — the culprit re-build that follows a known-bad skip."""

    def test_runs_validation_attempt_with_culprit_probe_output_dir(self) -> None:
        """The probe shells out to ``run_validation_attempt`` with a dedicated output dir.

        The ``culprit-probe`` subdirectory keeps the culprit run's
        logs separate from the main probe's logs, so the alert
        payload can link to the culprit log unambiguously.
        """
        # Arrange
        mock_run = Mock(return_value=(Mock(), {}, None))
        with patch(
            "scripts.probe_downstream_regression_window.run_validation_attempt",
            mock_run,
        ), patch(
            "scripts.probe_downstream_regression_window.parent_commit",
            return_value="p" * 40,
        ):
            # Act
            run_culprit_probe(
                config=PHYSLIB_CONFIG,
                culprit_commit="b" * 40,
                upstream_dir=Path("/dummy"),
                project_dir=Path("/dummy/downstream"),
                output_dir=Path("/dummy/output"),
                env={},
                tool_exe=None,
            )

        # Assert
        self.assertTrue(mock_run.called)
        call_kwargs = mock_run.call_args[1]
        self.assertEqual(
            call_kwargs["output_dir"],
            Path("/dummy/output/culprit-probe"),
            msg="Culprit run writes to a dedicated subdir to keep its log linkable",
        )

    def test_does_not_propagate_exception(self) -> None:
        """Any exception inside ``run_validation_attempt`` is swallowed.

        ``run_culprit_probe`` is a best-effort re-build — its purpose
        is to attach a fresh log to this run's artifacts, but the
        skip itself has already produced a valid ``ValidationResult``.
        Letting an unrelated subprocess error abort the probe job
        would lose the skip result that the heuristic already
        decided is correct.
        """
        # Arrange
        with patch(
            "scripts.probe_downstream_regression_window.run_validation_attempt",
            side_effect=RuntimeError("tool crashed"),
        ), patch(
            "scripts.probe_downstream_regression_window.parent_commit",
            return_value="p" * 40,
        ):
            # Act / Assert — must not raise
            run_culprit_probe(
                config=PHYSLIB_CONFIG,
                culprit_commit="b" * 40,
                upstream_dir=Path("/dummy"),
                project_dir=Path("/dummy/downstream"),
                output_dir=Path("/dummy/output"),
                env={},
                tool_exe=None,
            )


class ProbeParserTests(unittest.TestCase):
    """``probe_build_parser()`` — flag surface for the probe step."""

    _REQUIRED = ["--selection", "/tmp/s.json", "--workdir", "/tmp", "--output-dir", "/tmp"]

    def test_skip_known_bad_bisect_defaults_to_true(self) -> None:
        """Opt-out flag — defaults on so the heuristic engages by default."""
        # Arrange / Act
        args = probe_build_parser().parse_args(self._REQUIRED)

        # Assert
        self.assertTrue(args.skip_known_bad_bisect)

    def test_skip_known_bad_bisect_can_be_disabled(self) -> None:
        """``--no-skip-known-bad-bisect`` forces a full bisect.

        The operator escape hatch when the stored culprit might be
        stale or the regression boundary suspected to have moved.
        """
        # Arrange / Act
        args = probe_build_parser().parse_args(
            [*self._REQUIRED, "--no-skip-known-bad-bisect"]
        )

        # Assert
        self.assertFalse(args.skip_known_bad_bisect)

    def test_skip_known_bad_bisect_can_be_explicitly_enabled(self) -> None:
        """``--skip-known-bad-bisect`` is accepted as an explicit confirmation."""
        # Arrange / Act
        args = probe_build_parser().parse_args(
            [*self._REQUIRED, "--skip-known-bad-bisect"]
        )

        # Assert
        self.assertTrue(args.skip_known_bad_bisect)

    def test_max_commits_defaults_and_overrides(self) -> None:
        """``--max-commits`` defaults to 100000 and can be lowered for slow downstreams.

        The default is "effectively unbounded"; the override exists
        for downstreams where building 100k commits would exceed the
        runner's disk or wall-clock budget.  Pinning both the default
        and the override here means a maintainer who changes either
        has to update this test.
        """
        # Arrange / Act / Assert — default
        args = probe_build_parser().parse_args(self._REQUIRED)
        self.assertEqual(args.max_commits, 100000)

        # Act / Assert — override
        args = probe_build_parser().parse_args(
            [*self._REQUIRED, "--max-commits", "50"]
        )
        self.assertEqual(args.max_commits, 50)


if __name__ == "__main__":
    unittest.main()
