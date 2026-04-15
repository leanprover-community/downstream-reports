#!/usr/bin/env python3
"""Tests for the regression episode state machine and result aggregation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.aggregate_results import (
    EpisodeState,
    Outcome,
    ValidationResult,
    apply_result,
    load_culprit_log_text,
    render_report,
    truncate_log_text,
    filter_culprit_log_text,
    first_bad_position,
)
from scripts.storage import DownstreamStatusRecord


def _make_result(
    outcome: Outcome = Outcome.PASSED,
    target_commit: str = "target_abc",
    first_failing_commit: str | None = None,
    last_successful_commit: str | None = None,
    pinned_commit: str | None = None,
    **kwargs,
) -> ValidationResult:
    """Helper to construct a minimal ValidationResult for state-machine tests."""
    defaults = dict(
        downstream="TestDownstream",
        repo="owner/repo",
        downstream_commit="ds_head",
        commit_window_truncated=False,
        error=None,
        search_mode="head-only",
    )
    defaults.update(kwargs)
    return ValidationResult(
        outcome=outcome,
        target_commit=target_commit,
        failure_stage=None if outcome is Outcome.PASSED else "build",
        first_failing_commit=first_failing_commit,
        last_successful_commit=last_successful_commit,
        pinned_commit=pinned_commit,
        **defaults,
    )


class ApplyResultTests(unittest.TestCase):
    """Test the state machine that tracks regression episodes."""

    # -- passing states --

    def test_passing_plus_passed_is_passing(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(outcome=Outcome.PASSED, target_commit="good_new")
        updated, state = apply_result(current, result)
        self.assertEqual(state, EpisodeState.PASSING)
        self.assertEqual(updated.last_known_good_commit, "good_new")
        self.assertIsNone(updated.first_known_bad_commit)

    def test_no_prior_state_plus_passed_is_passing(self) -> None:
        result = _make_result(outcome=Outcome.PASSED, target_commit="first_good")
        updated, state = apply_result(None, result)
        self.assertEqual(state, EpisodeState.PASSING)
        self.assertEqual(updated.last_known_good_commit, "first_good")

    # -- new failure --

    def test_passing_plus_failed_is_new_failure(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit="bad_commit",
        )
        updated, state = apply_result(current, result)
        self.assertEqual(state, EpisodeState.NEW_FAILURE)
        self.assertEqual(updated.first_known_bad_commit, "bad_commit")

    def test_no_prior_state_plus_failed_is_new_failure(self) -> None:
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit="bad_commit",
        )
        updated, state = apply_result(None, result)
        self.assertEqual(state, EpisodeState.NEW_FAILURE)
        self.assertEqual(updated.first_known_bad_commit, "bad_commit")
        self.assertIsNone(updated.last_known_good_commit)

    def test_new_failure_uses_target_when_no_first_failing(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit=None,
        )
        updated, state = apply_result(current, result)
        self.assertEqual(state, EpisodeState.NEW_FAILURE)
        self.assertEqual(updated.first_known_bad_commit, "bad_target")

    # -- failing --

    def test_failing_plus_failed_is_failing(self) -> None:
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="still_bad",
            first_failing_commit="new_bad",
            last_successful_commit="new_good",
        )
        updated, state = apply_result(current, result)
        self.assertEqual(state, EpisodeState.FAILING)
        # Preserves original first-known-bad from the episode opener
        self.assertEqual(updated.first_known_bad_commit, "original_bad")
        # Updates last-known-good from the new run's bisect
        self.assertEqual(updated.last_known_good_commit, "new_good")

    # -- recovery --

    def test_failing_plus_passed_is_recovered(self) -> None:
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="was_bad",
        )
        result = _make_result(outcome=Outcome.PASSED, target_commit="now_good")
        updated, state = apply_result(current, result)
        self.assertEqual(state, EpisodeState.RECOVERED)
        self.assertEqual(updated.last_known_good_commit, "now_good")
        self.assertIsNone(updated.first_known_bad_commit)

    # -- error handling --

    def test_error_preserves_passing_state(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(outcome=Outcome.ERROR, target_commit="err_target")
        updated, state = apply_result(current, result)
        self.assertEqual(state, EpisodeState.ERROR)
        self.assertEqual(updated.last_known_good_commit, "good_old")
        self.assertIsNone(updated.first_known_bad_commit)

    def test_error_preserves_failing_state(self) -> None:
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="bad_old",
        )
        result = _make_result(outcome=Outcome.ERROR, target_commit="err_target")
        updated, state = apply_result(current, result)
        self.assertEqual(state, EpisodeState.ERROR)
        self.assertEqual(updated.last_known_good_commit, "good_old")
        self.assertEqual(updated.first_known_bad_commit, "bad_old")

    def test_error_with_no_prior_state(self) -> None:
        result = _make_result(outcome=Outcome.ERROR, target_commit="err_target")
        updated, state = apply_result(None, result)
        self.assertEqual(state, EpisodeState.ERROR)
        self.assertIsNone(updated.last_known_good_commit)
        self.assertIsNone(updated.first_known_bad_commit)

    # -- pinned commit tracking --

    def test_pinned_commit_is_preserved_from_result(self) -> None:
        current = DownstreamStatusRecord(pinned_commit="old_pin")
        result = _make_result(
            outcome=Outcome.PASSED,
            target_commit="new_good",
            pinned_commit="new_pin",
        )
        updated, _ = apply_result(current, result)
        self.assertEqual(updated.pinned_commit, "new_pin")

    def test_error_preserves_existing_pin_when_result_has_none(self) -> None:
        current = DownstreamStatusRecord(pinned_commit="old_pin")
        result = _make_result(outcome=Outcome.ERROR, pinned_commit=None)
        updated, _ = apply_result(current, result)
        self.assertEqual(updated.pinned_commit, "old_pin")

    # -- downstream commit tracking --

    def test_downstream_commit_is_propagated_on_pass(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(outcome=Outcome.PASSED, target_commit="good_new")
        updated, _ = apply_result(current, result)
        self.assertEqual(updated.downstream_commit, "ds_head")

    def test_downstream_commit_is_propagated_on_failure(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit="bad_commit",
        )
        updated, _ = apply_result(current, result)
        self.assertEqual(updated.downstream_commit, "ds_head")

    def test_downstream_commit_preserved_on_error_when_result_has_none(self) -> None:
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            downstream_commit="old_ds_head",
        )
        result = _make_result(
            outcome=Outcome.ERROR,
            target_commit="err_target",
            downstream_commit=None,
        )
        updated, _ = apply_result(current, result)
        self.assertEqual(updated.downstream_commit, "old_ds_head")


class TruncateLogTextTests(unittest.TestCase):
    """Tests for log text truncation."""

    def test_short_text_is_unchanged(self) -> None:
        text = "line1\nline2\nline3"
        self.assertEqual(truncate_log_text(text), text)

    def test_line_limit_is_enforced(self) -> None:
        lines = [f"line{i}" for i in range(300)]
        result = truncate_log_text("\n".join(lines), max_lines=200)
        self.assertIn("[log truncated]", result)
        # 200 lines + truncation notice
        self.assertLessEqual(result.count("\n"), 201)

    def test_char_limit_is_enforced(self) -> None:
        text = "x" * 50000
        result = truncate_log_text(text, max_chars=40000)
        self.assertIn("[log truncated]", result)
        self.assertLessEqual(len(result), 40020)  # some slack for the notice


class LoadCulpritLogTextTests(unittest.TestCase):
    """Tests for locating the culprit log in the artifact directory tree."""

    def _write_log(self, root: Path, *parts: str, content: str = "build failed\n") -> Path:
        log_dir = root.joinpath(*parts)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "build.log"
        log_file.write_text(content)
        return log_file

    def test_culprit_probe_takes_priority(self) -> None:
        """Scenario: culprit-probe log is preferred over other locations."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_log(root, "culprit-probe", "tool-state", "logs", "culprit", content="culprit log\n")
            self._write_log(root, "bisect", "tool-state", "logs", "culprit", content="bisect log\n")
            result = load_culprit_log_text(root)
            self.assertIsNotNone(result)
            self.assertIn("culprit log", result)

    def test_bisect_log_found(self) -> None:
        """Scenario: bisect probe log is found when no culprit-probe log exists."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_log(root, "bisect", "tool-state", "logs", "culprit", content="bisect failed\n")
            result = load_culprit_log_text(root)
            self.assertIsNotNone(result)
            self.assertIn("bisect failed", result)

    def test_head_probe_log_found(self) -> None:
        """Scenario: head probe log is found for head-only failing runs."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_log(root, "head-probe", "tool-state", "logs", "culprit", content="head probe failed\n")
            result = load_culprit_log_text(root)
            self.assertIsNotNone(result)
            self.assertIn("head probe failed", result)

    def test_returns_none_when_no_logs_found(self) -> None:
        """Scenario: None returned when no culprit log exists anywhere."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = load_culprit_log_text(Path(tmp))
            self.assertIsNone(result)

    def test_update_log_is_found(self) -> None:
        """Scenario: update.log (not build.log) is found in the culprit directory."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "bisect" / "tool-state" / "logs" / "culprit"
            log_dir.mkdir(parents=True)
            (log_dir / "update.log").write_text("update failed\n")
            result = load_culprit_log_text(root)
            self.assertIsNotNone(result)
            self.assertIn("update failed", result)


class FilterCulpritLogTextTests(unittest.TestCase):
    """Tests for culprit log filtering."""

    def test_successful_lines_are_removed(self) -> None:
        text = "✔ target passed\ntrace: .> LEAN_PATH=/home/lean\nERROR: build failed\n✔ another pass"
        result = filter_culprit_log_text(text)
        self.assertEqual("ERROR: build failed", result.strip())


class FirstBadPositionTests(unittest.TestCase):
    """Tests for locating the first bad commit in the bisect window."""

    def test_returns_position_when_found(self) -> None:
        details = [{"sha": "a"}, {"sha": "b"}, {"sha": "c"}]
        self.assertEqual(first_bad_position(details, "b"), (2, 3))

    def test_returns_none_when_not_found(self) -> None:
        details = [{"sha": "a"}, {"sha": "b"}]
        self.assertIsNone(first_bad_position(details, "z"))

    def test_returns_none_for_empty_details(self) -> None:
        self.assertIsNone(first_bad_position([], "a"))

    def test_returns_none_for_none_sha(self) -> None:
        details = [{"sha": "a"}]
        self.assertIsNone(first_bad_position(details, None))


class RenderReportSkippedTests(unittest.TestCase):
    """Tests for skipped-downstream rendering in render_report."""

    _COMMON_KWARGS = dict(
        recorded_at="2026-04-15T00:00:00Z",
        upstream_ref="ondemand",
        run_id="run_1",
        run_url="https://example.com/run/1",
        rows=[],
    )

    def test_no_skipped_section_when_none(self) -> None:
        """Scenario: no skipped section when skipped_rows is None."""
        md = render_report(**self._COMMON_KWARGS, skipped_rows=None)
        self.assertNotIn("Previously Tested", md)

    def test_no_skipped_section_when_empty(self) -> None:
        """Scenario: no skipped section when skipped_rows is empty."""
        md = render_report(**self._COMMON_KWARGS, skipped_rows=[])
        self.assertNotIn("Previously Tested", md)

    def test_skipped_section_rendered(self) -> None:
        """Scenario: skipped downstreams appear in the report."""
        skipped = [{
            "downstream": "TestProject",
            "repo": "owner/TestProject",
            "downstream_commit": "abc123def456",
            "outcome": "passed",
            "episode_state": "passing",
            "first_known_bad": None,
            "target_commit": "target789",
            "previous_run_url": "https://example.com/run/old",
            "previous_job_url": "https://example.com/job/42",
        }]
        md = render_report(**self._COMMON_KWARGS, skipped_rows=skipped)
        self.assertIn("Previously Tested", md)
        self.assertIn("TestProject", md)
        self.assertIn("compatible", md)
        self.assertIn("https://example.com/job/42", md)

    def test_skipped_failed_shows_first_known_bad(self) -> None:
        """Scenario: skipped downstream with failed outcome shows first_known_bad."""
        skipped = [{
            "downstream": "FailProject",
            "repo": "owner/FailProject",
            "downstream_commit": "abc",
            "outcome": "failed",
            "episode_state": "failing",
            "first_known_bad": "bad123456789abcdef",
            "target_commit": "target",
            "previous_run_url": None,
            "previous_job_url": None,
        }]
        md = render_report(**self._COMMON_KWARGS, skipped_rows=skipped)
        self.assertIn("bad123456789", md)
        self.assertIn("incompatible", md)


if __name__ == "__main__":
    unittest.main()
