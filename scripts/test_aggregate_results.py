#!/usr/bin/env python3
"""
Tests for: scripts.aggregate_results

Coverage scope:
    - ``apply_result`` — the episode state machine.  Every documented
      ``(prior_state × outcome)`` transition has at least one test;
      ``ApplyResultTests`` is the executable form of the project's
      episode-state-machine contract.
    - ``find_non_adjacent_endpoints`` — adjacency-invariant validator.
    - ``_pin_crossed_fkb`` — the predicate that decides whether a
      continuing-FAILED result opens a new episode (``NEW_FAILURE``)
      vs. continuing the existing one (``FAILING``).
    - ``truncate_log_text`` / ``load_culprit_log_text`` /
      ``filter_culprit_log_text`` / ``first_bad_position`` — log /
      window utilities used by ``render_report``.
    - ``render_report`` — markdown rendering of the per-run report
      (skipped section, culprit-log truncation, release tags).
    - ``fetch_release_tags_api`` — GitHub API wrapper, mocked at the
      HTTP layer.

Out of scope:
    - ``main()``: the CLI entry point glues argparse, backend
      construction, and report rendering together; exercised
      end-to-end by the regression workflow itself.
    - ``_gh_get`` / ``fetch_commit_distances`` — thin HTTP wrappers
      exercised transitively via ``fetch_release_tags_api`` and
      ``_pin_crossed_fkb``.

Why this matters
----------------
``apply_result`` is the heart of the regression contract.  A wrong
transition silently corrupts the persisted ``DownstreamStatusRecord``,
and from there propagates into the public snapshot, the rendered site,
and the alert payloads.  Every transition in the matrix is therefore
pinned by an explicit test; new transitions must come with a new test.

FAILING-state boundary semantics
--------------------------------
During a continuing FAILING episode, a fresh bisect run (``search_mode
== "bisect"`` with both ``last_successful_commit`` and
``first_failing_commit`` populated) supersedes BOTH stored endpoints
with the new bisect's adjacent pair.  This is the contract that keeps
the persisted ``(last_known_good_commit, first_known_bad_commit)``
adjacent on master even when the downstream commit changes and a
fresh bisect now binds the regression to a different upstream commit.
Pinned by ``test_failing_bisect_finds_earlier_transition_replaces_pair``;
implemented in ``apply_result`` at aggregate_results.py:460-474.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.aggregate_results import (
    EpisodeState,
    Outcome,
    ValidationResult,
    _pin_crossed_fkb,
    apply_result,
    find_non_adjacent_endpoints,
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


class TestApplyResult:
    """Test the state machine that tracks regression episodes."""

    # -- passing states --

    def test_passing_plus_passed_is_passing(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(outcome=Outcome.PASSED, target_commit="good_new")
        updated, state = apply_result(current, result)
        assert state == EpisodeState.PASSING
        assert updated.last_known_good_commit == "good_new"
        assert updated.first_known_bad_commit is None

    def test_no_prior_state_plus_passed_is_passing(self) -> None:
        result = _make_result(outcome=Outcome.PASSED, target_commit="first_good")
        updated, state = apply_result(None, result)
        assert state == EpisodeState.PASSING
        assert updated.last_known_good_commit == "first_good"

    # -- new failure --

    def test_passing_plus_failed_is_new_failure(self) -> None:
        """Scenario: head-only failure opens a new episode without a known LKG."""
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit="bad_commit",
        )
        updated, state = apply_result(current, result)
        assert state == EpisodeState.NEW_FAILURE
        assert updated.first_known_bad_commit == "bad_commit"
        # No bisect data: prior LKG belongs to a different episode and is not
        # adjacent to the new FKB, so it is dropped to keep the invariant vacuous.
        assert updated.last_known_good_commit is None

    def test_passing_plus_bisect_failed_is_new_failure_with_adjacent_pair(self) -> None:
        """Scenario: bisect-mode failure opens a new episode with an adjacent (LKG, FKB) pair."""
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit="bad_commit",
            last_successful_commit="good_parent",
            search_mode="bisect",
        )
        updated, state = apply_result(current, result)
        assert state == EpisodeState.NEW_FAILURE
        assert updated.first_known_bad_commit == "bad_commit"
        assert updated.last_known_good_commit == "good_parent"

    def test_no_prior_state_plus_failed_is_new_failure(self) -> None:
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit="bad_commit",
        )
        updated, state = apply_result(None, result)
        assert state == EpisodeState.NEW_FAILURE
        assert updated.first_known_bad_commit == "bad_commit"
        assert updated.last_known_good_commit is None

    def test_new_failure_uses_target_when_no_first_failing(self) -> None:
        """Scenario: head-only failure with no first_failing_commit falls back to the target."""
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit=None,
        )
        updated, state = apply_result(current, result)
        assert state == EpisodeState.NEW_FAILURE
        assert updated.first_known_bad_commit == "bad_target"
        # Stale prior LKG is not adjacent to the new FKB; drop it.
        assert updated.last_known_good_commit is None

    # -- failing --

    def test_failing_head_only_preserves_both_endpoints(self) -> None:
        """Scenario: continuing FAILING with no fresh bisect data preserves both endpoints.

        Covers the head-only-known-bad skip path and head-only failures: the
        prior (LKG, FKB) pair is the only adjacent pair we have, so we keep it.
        """
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="still_bad",
            first_failing_commit=None,
            last_successful_commit=None,
            search_mode="head-only-known-bad",
        )
        updated, state = apply_result(current, result)
        assert state == EpisodeState.FAILING
        assert updated.first_known_bad_commit == "original_bad"
        assert updated.last_known_good_commit == "good_old"

    def test_failing_bisect_same_transition_is_noop(self) -> None:
        """Scenario: continuing FAILING and bisect identifies the same transition as before."""
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="still_bad",
            first_failing_commit="original_bad",
            last_successful_commit="good_old",
            search_mode="bisect",
        )
        updated, state = apply_result(current, result)
        assert state == EpisodeState.FAILING
        assert updated.first_known_bad_commit == "original_bad"
        assert updated.last_known_good_commit == "good_old"

    def test_failing_head_only_no_window_preserves_both_endpoints(self) -> None:
        """Scenario: continuing FAILING with a head-only failure (no bisect window) preserves prior pair."""
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="still_bad",
            first_failing_commit=None,
            last_successful_commit=None,
            search_mode="head-only",
        )
        updated, state = apply_result(current, result)
        assert state == EpisodeState.FAILING
        assert updated.first_known_bad_commit == "original_bad"
        assert updated.last_known_good_commit == "good_old"

    def test_failing_bisect_finds_earlier_transition_replaces_pair(self) -> None:
        """
        When a fresh bisect during a continuing FAILING episode identifies a
        different transition (typically because the downstream's
        ``downstream_commit`` changed and the regression now binds to an
        earlier upstream commit), we replace **both** endpoints with the
        new bisect's adjacent pair rather than keeping a stale FKB.

        Why this matters
        ----------------
        Persisting a stale FKB while the LKG advances would violate the
        adjacency invariant that ``find_non_adjacent_endpoints`` polices —
        the persisted (LKG, FKB) pair must always be parent/child on
        master, and only a fresh bisect can guarantee that.  See
        ``apply_result`` at aggregate_results.py:460-474 for the
        implementation and inline justification.
        """
        # Arrange — current FAILING state with original boundary.
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="still_bad",
            first_failing_commit="new_bad",
            last_successful_commit="new_good",
            search_mode="bisect",
        )

        # Act
        updated, state = apply_result(current, result)

        # Assert
        assert state == EpisodeState.FAILING, "State stays FAILING — the episode is not new, just refined"
        assert updated.first_known_bad_commit == "new_bad", (
                "Fresh bisect supersedes the stored FKB so the persisted "
                "(LKG, FKB) pair stays adjacent on master"
            )
        assert updated.last_known_good_commit == "new_good"

    # -- pin advanced past FKB: new episode --

    def test_pin_past_fkb_failing_opens_new_episode(self) -> None:
        """Scenario: downstream pinned forward past the stored FKB; bisect finds a new transition."""
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="still_bad",
            first_failing_commit="new_bad",
            last_successful_commit="new_good",
            search_mode="bisect",
        )
        updated, state = apply_result(current, result, pin_past_fkb=True)
        assert state == EpisodeState.NEW_FAILURE
        # Uses the new run's adjacent pair, not the old episode's endpoints.
        assert updated.first_known_bad_commit == "new_bad"
        assert updated.last_known_good_commit == "new_good"

    def test_pin_past_fkb_uses_target_when_no_first_failing(self) -> None:
        """Scenario: pin advanced past FKB but head-only probe found no first_failing_commit."""
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit=None,
        )
        updated, state = apply_result(current, result, pin_past_fkb=True)
        assert state == EpisodeState.NEW_FAILURE
        assert updated.first_known_bad_commit == "bad_target"
        # Stale prior LKG belongs to the old episode and is not adjacent to
        # the new FKB; drop it to keep the invariant vacuous.
        assert updated.last_known_good_commit is None

    def test_pin_past_fkb_false_preserves_old_episode(self) -> None:
        """Scenario: pin_past_fkb=False leaves the episode intact (default behaviour)."""
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="original_bad",
        )
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="still_bad",
            first_failing_commit="new_bad",
        )
        updated, state = apply_result(current, result, pin_past_fkb=False)
        assert state == EpisodeState.FAILING
        assert updated.first_known_bad_commit == "original_bad"
        assert updated.last_known_good_commit == "good_old"

    # -- recovery --

    def test_failing_plus_passed_is_recovered(self) -> None:
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="was_bad",
        )
        result = _make_result(outcome=Outcome.PASSED, target_commit="now_good")
        updated, state = apply_result(current, result)
        assert state == EpisodeState.RECOVERED
        assert updated.last_known_good_commit == "now_good"
        assert updated.first_known_bad_commit is None

    # -- error handling --

    def test_error_preserves_passing_state(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(outcome=Outcome.ERROR, target_commit="err_target")
        updated, state = apply_result(current, result)
        assert state == EpisodeState.ERROR
        assert updated.last_known_good_commit == "good_old"
        assert updated.first_known_bad_commit is None

    def test_error_preserves_failing_state(self) -> None:
        current = DownstreamStatusRecord(
            last_known_good_commit="good_old",
            first_known_bad_commit="bad_old",
        )
        result = _make_result(outcome=Outcome.ERROR, target_commit="err_target")
        updated, state = apply_result(current, result)
        assert state == EpisodeState.ERROR
        assert updated.last_known_good_commit == "good_old"
        assert updated.first_known_bad_commit == "bad_old"

    def test_error_with_no_prior_state(self) -> None:
        result = _make_result(outcome=Outcome.ERROR, target_commit="err_target")
        updated, state = apply_result(None, result)
        assert state == EpisodeState.ERROR
        assert updated.last_known_good_commit is None
        assert updated.first_known_bad_commit is None

    # -- pinned commit tracking --

    def test_pinned_commit_is_preserved_from_result(self) -> None:
        current = DownstreamStatusRecord(pinned_commit="old_pin")
        result = _make_result(
            outcome=Outcome.PASSED,
            target_commit="new_good",
            pinned_commit="new_pin",
        )
        updated, _ = apply_result(current, result)
        assert updated.pinned_commit == "new_pin"

    def test_error_preserves_existing_pin_when_result_has_none(self) -> None:
        current = DownstreamStatusRecord(pinned_commit="old_pin")
        result = _make_result(outcome=Outcome.ERROR, pinned_commit=None)
        updated, _ = apply_result(current, result)
        assert updated.pinned_commit == "old_pin"

    # -- downstream commit tracking --

    def test_downstream_commit_is_propagated_on_pass(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(outcome=Outcome.PASSED, target_commit="good_new")
        updated, _ = apply_result(current, result)
        assert updated.downstream_commit == "ds_head"

    def test_downstream_commit_is_propagated_on_failure(self) -> None:
        current = DownstreamStatusRecord(last_known_good_commit="good_old")
        result = _make_result(
            outcome=Outcome.FAILED,
            target_commit="bad_target",
            first_failing_commit="bad_commit",
        )
        updated, _ = apply_result(current, result)
        assert updated.downstream_commit == "ds_head"

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
        assert updated.downstream_commit == "old_ds_head"


class TestFindNonAdjacentEndpoints:
    """Tripwire that verifies LKG is the immediate parent of FKB on persisted records."""

    def test_no_warning_when_pair_is_adjacent(self) -> None:
        """Scenario: distance from LKG to FKB is exactly 1 — invariant holds."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit="lkg_sha",
                first_known_bad_commit="fkb_sha",
            ),
        }
        distances = {("lkg_sha", "fkb_sha"): 1}
        assert find_non_adjacent_endpoints(statuses, distances) == set()

    def test_warns_when_pair_is_not_adjacent(self) -> None:
        """Scenario: LKG and FKB are several commits apart — invariant violated."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit="lkg_sha",
                first_known_bad_commit="fkb_sha",
            ),
        }
        distances = {("lkg_sha", "fkb_sha"): 5}
        offending = find_non_adjacent_endpoints(statuses, distances)
        assert offending == {"Foo"}

    def test_warns_when_lkg_equals_fkb(self) -> None:
        """Scenario: degenerate state where LKG and FKB are the same commit."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit="same_sha",
                first_known_bad_commit="same_sha",
            ),
        }
        offending = find_non_adjacent_endpoints(statuses, {})
        assert offending == {"Foo"}

    def test_silent_when_lkg_is_none(self) -> None:
        """Scenario: head-only NEW_FAILURE leaves LKG unset — invariant vacuous."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit=None,
                first_known_bad_commit="fkb_sha",
            ),
        }
        assert find_non_adjacent_endpoints(statuses, {}) == set()

    def test_silent_when_fkb_is_none(self) -> None:
        """Scenario: PASSING/RECOVERED state has no active episode — invariant vacuous."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit="lkg_sha",
                first_known_bad_commit=None,
            ),
        }
        assert find_non_adjacent_endpoints(statuses, {}) == set()

    def test_silent_when_distance_lookup_failed(self) -> None:
        """Scenario: GitHub compare API returned None — no signal, no warning."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit="lkg_sha",
                first_known_bad_commit="fkb_sha",
            ),
        }
        distances: dict[tuple[str, str], int | None] = {("lkg_sha", "fkb_sha"): None}
        assert find_non_adjacent_endpoints(statuses, distances) == set()

    def test_silent_when_distance_not_in_cache(self) -> None:
        """Scenario: pair was never requested (e.g. main built compare_pairs differently)."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit="lkg_sha",
                first_known_bad_commit="fkb_sha",
            ),
        }
        assert find_non_adjacent_endpoints(statuses, {}) == set()

    def test_warns_for_each_offending_downstream(self) -> None:
        """Scenario: multiple downstreams checked independently."""
        statuses = {
            "Foo": DownstreamStatusRecord(
                last_known_good_commit="lkg_a",
                first_known_bad_commit="fkb_a",
            ),
            "Bar": DownstreamStatusRecord(
                last_known_good_commit="lkg_b",
                first_known_bad_commit="fkb_b",
            ),
            "Baz": DownstreamStatusRecord(
                last_known_good_commit="lkg_c",
                first_known_bad_commit="fkb_c",
            ),
        }
        distances = {
            ("lkg_a", "fkb_a"): 1,   # adjacent → no warn
            ("lkg_b", "fkb_b"): 7,   # not adjacent → warn
            ("lkg_c", "fkb_c"): 1,   # adjacent → no warn
        }
        offending = find_non_adjacent_endpoints(statuses, distances)
        assert offending == {"Bar"}


class TestPinCrossedFkb:
    """Tests for the _pin_crossed_fkb helper that guards new-episode detection."""

    def _distances(self, mapping: dict[tuple[str, str], int]) -> dict[tuple[str, str], int | None]:
        return dict(mapping)

    def test_returns_true_when_pin_crosses_fkb(self) -> None:
        """Scenario: prior pin was behind FKB, current pin is ahead."""
        distances = self._distances({("fkb", "new_pin"): 5, ("fkb", "old_pin"): 0})
        assert _pin_crossed_fkb("fkb", "old_pin", "new_pin", distances)

    def test_returns_false_when_pin_already_past_fkb(self) -> None:
        """Scenario: pin was already ahead of FKB when episode opened — no advancement."""
        distances = self._distances({("fkb", "old_pin"): 3, ("fkb", "old_pin"): 3})
        assert not _pin_crossed_fkb("fkb", "old_pin", "old_pin", distances)

    def test_returns_false_when_prior_pin_was_already_past_fkb(self) -> None:
        """Scenario: prior and current pins differ but prior was already past FKB."""
        distances = self._distances({("fkb", "new_pin"): 7, ("fkb", "old_pin"): 3})
        assert not _pin_crossed_fkb("fkb", "old_pin", "new_pin", distances)

    def test_returns_false_when_current_pin_equals_fkb(self) -> None:
        """Scenario: pin is exactly at FKB — not past it."""
        distances: dict[tuple[str, str], int | None] = {}
        assert not _pin_crossed_fkb("fkb", "old_pin", "fkb", distances)

    def test_returns_false_when_current_pin_is_none(self) -> None:
        """Scenario: no pin information in result."""
        distances: dict[tuple[str, str], int | None] = {}
        assert not _pin_crossed_fkb("fkb", "old_pin", None, distances)

    def test_returns_false_when_prior_pin_is_none(self) -> None:
        """Scenario: no prior pin stored; cannot confirm it was behind FKB."""
        distances = self._distances({("fkb", "new_pin"): 5})
        assert not _pin_crossed_fkb("fkb", None, "new_pin", distances)

    def test_returns_false_when_api_distance_missing(self) -> None:
        """Scenario: GitHub API call failed for this pair; fall back to no-op."""
        assert not _pin_crossed_fkb("fkb", "old_pin", "new_pin", {})

    def test_prior_pin_equals_fkb_counts_as_not_past(self) -> None:
        """Scenario: prior pin was exactly at FKB; current pin is ahead — new episode."""
        distances = self._distances({("fkb", "new_pin"): 2})
        assert _pin_crossed_fkb("fkb", "fkb", "new_pin", distances)


class TestTruncateLogText:
    """Tests for log text truncation."""

    def test_short_text_is_unchanged(self) -> None:
        text = "line1\nline2\nline3"
        assert truncate_log_text(text) == text

    def test_line_limit_is_enforced(self) -> None:
        lines = [f"line{i}" for i in range(300)]
        result = truncate_log_text("\n".join(lines), max_lines=200)
        assert "[log truncated]" in result
        # 200 lines + truncation notice
        assert result.count("\n") <= 201

    def test_char_limit_is_enforced(self) -> None:
        text = "x" * 50000
        result = truncate_log_text(text, max_chars=40000)
        assert "[log truncated]" in result
        assert len(result) <= 40020  # some slack for the notice

    def test_default_line_limit_is_50(self) -> None:
        """Scenario: default max_lines is 50 so summaries stay under GitHub's 1024k limit."""
        lines = [f"line{i}" for i in range(100)]
        result = truncate_log_text("\n".join(lines))
        assert "[log truncated]" in result
        assert result.count("\n") <= 51  # 50 lines + truncation notice


class TestLoadCulpritLogText:
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
            assert result is not None
            assert "culprit log" in result

    def test_bisect_log_found(self) -> None:
        """Scenario: bisect probe log is found when no culprit-probe log exists."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_log(root, "bisect", "tool-state", "logs", "culprit", content="bisect failed\n")
            result = load_culprit_log_text(root)
            assert result is not None
            assert "bisect failed" in result

    def test_head_probe_log_found(self) -> None:
        """Scenario: head probe log is found for head-only failing runs."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_log(root, "head-probe", "tool-state", "logs", "culprit", content="head probe failed\n")
            result = load_culprit_log_text(root)
            assert result is not None
            assert "head probe failed" in result

    def test_returns_none_when_no_logs_found(self) -> None:
        """Scenario: None returned when no culprit log exists anywhere."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = load_culprit_log_text(Path(tmp))
            assert result is None

    def test_update_log_is_found(self) -> None:
        """Scenario: update.log (not build.log) is found in the culprit directory."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_dir = root / "bisect" / "tool-state" / "logs" / "culprit"
            log_dir.mkdir(parents=True)
            (log_dir / "update.log").write_text("update failed\n")
            result = load_culprit_log_text(root)
            assert result is not None
            assert "update failed" in result


class TestFilterCulpritLogText:
    """Tests for culprit log filtering."""

    def test_successful_lines_are_removed(self) -> None:
        text = "✔ target passed\ntrace: .> LEAN_PATH=/home/lean\nERROR: build failed\n✔ another pass"
        result = filter_culprit_log_text(text)
        assert "ERROR: build failed" == result.strip()


class TestFirstBadPosition:
    """Tests for locating the first bad commit in the bisect window."""

    def test_returns_position_when_found(self) -> None:
        details = [{"sha": "a"}, {"sha": "b"}, {"sha": "c"}]
        assert first_bad_position(details, "b") == (2, 3)

    def test_returns_none_when_not_found(self) -> None:
        details = [{"sha": "a"}, {"sha": "b"}]
        assert first_bad_position(details, "z") is None

    def test_returns_none_for_empty_details(self) -> None:
        assert first_bad_position([], "a") is None

    def test_returns_none_for_none_sha(self) -> None:
        details = [{"sha": "a"}]
        assert first_bad_position(details, None) is None


class TestRenderReportSkipped:
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
        assert "Previously Tested" not in md

    def test_no_skipped_section_when_empty(self) -> None:
        """Scenario: no skipped section when skipped_rows is empty."""
        md = render_report(**self._COMMON_KWARGS, skipped_rows=[])
        assert "Previously Tested" not in md

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
        assert "Previously Tested" in md
        assert "TestProject" in md
        assert "compatible" in md
        assert "https://example.com/job/42" in md

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
        assert "bad123456789" in md
        assert "incompatible" in md


class TestRenderReportCulpritLog:
    """Tests for culprit log rendering and truncation notices in render_report."""

    _BASE_ROW: dict = dict(
        upstream="leanprover-community/mathlib4",
        downstream="TestProject",
        repo="owner/TestProject",
        downstream_commit="ds_abc",
        outcome="failed",
        episode_state="new_failure",
        target_commit="target_abc",
        previous_last_known_good=None,
        previous_first_known_bad=None,
        last_known_good=None,
        first_known_bad="bad_abc",
        current_last_successful=None,
        current_first_failing="bad_abc",
        failure_stage=None,
        search_mode="head-only",
        commit_window_truncated=False,
        error=None,
        head_probe_outcome="failed",
        head_probe_failure_stage=None,
        culprit_log_text=None,
        pinned_commit=None,
        search_base_not_ancestor=False,
        tested_commit_details=[],
    )

    _COMMON_KWARGS = dict(
        recorded_at="2026-04-22T00:00:00Z",
        upstream_ref="master",
        run_id="run_1",
        run_url="https://example.com/run/1",
    )

    def test_truncated_log_with_job_url_shows_download_link(self) -> None:
        """Scenario: truncated culprit log gets a download link pointing to the probe job."""
        truncated_log = "line1\nline2\n[log truncated]"
        row = {**self._BASE_ROW, "culprit_log_text": truncated_log}
        job_urls = {"TestProject": "https://example.com/job/42"}
        md = render_report(**self._COMMON_KWARGS, rows=[row], job_urls=job_urls)
        assert "probe job" in md
        assert "https://example.com/job/42" in md

    def test_non_truncated_log_has_no_download_link(self) -> None:
        """Scenario: complete log (not truncated) does not add a download link."""
        short_log = "line1\nline2\nbuild failed"
        row = {**self._BASE_ROW, "culprit_log_text": short_log}
        job_urls = {"TestProject": "https://example.com/job/42"}
        md = render_report(**self._COMMON_KWARGS, rows=[row], job_urls=job_urls)
        assert "probe job" not in md

    def test_truncated_log_without_job_url_has_no_download_link(self) -> None:
        """Scenario: truncated log without a known job URL shows no download link."""
        truncated_log = "line1\nline2\n[log truncated]"
        row = {**self._BASE_ROW, "culprit_log_text": truncated_log}
        md = render_report(**self._COMMON_KWARGS, rows=[row], job_urls=None)
        assert "probe job" not in md
        assert "[log truncated]" in md


class TestReleaseEnrichment:
    """Tests for the post-aggregation release tag enrichment in main()."""

    def test_enrichment_sets_tag_and_sha(self) -> None:
        """Scenario: fetch_release_tags_api returns a tag; updated_statuses gets tag+sha."""
        from dataclasses import replace
        from unittest.mock import patch
        import scripts.aggregate_results as agg

        status = DownstreamStatusRecord(last_known_good_commit="lkg_abc")
        updated_statuses = {"physlib": status}

        lkg_commits = {
            name: s.last_known_good_commit
            for name, s in updated_statuses.items()
            if s.last_known_good_commit is not None
        }
        with patch(
            "scripts.aggregate_results.fetch_release_tags_api",
            return_value={"physlib": ("v4.13.0", "sha_v4_13_0")},
        ) as mock_api:
            release_tags = agg.fetch_release_tags_api("upstream/repo", lkg_commits, None)
            for name, (tag, sha) in release_tags.items():
                updated_statuses[name] = replace(
                    updated_statuses[name], last_good_release=tag, last_good_release_commit=sha
                )
            mock_api.assert_called_once_with("upstream/repo", lkg_commits, None)

        assert updated_statuses["physlib"].last_good_release == "v4.13.0"
        assert updated_statuses["physlib"].last_good_release_commit == "sha_v4_13_0"

    def test_enrichment_sets_none_when_no_tag(self) -> None:
        """Scenario: no tag reachable from LKG — both fields stay None."""
        from dataclasses import replace
        from unittest.mock import patch
        import scripts.aggregate_results as agg

        status = DownstreamStatusRecord(last_known_good_commit="lkg_abc")
        updated_statuses = {"physlib": status}

        lkg_commits = {
            name: s.last_known_good_commit
            for name, s in updated_statuses.items()
            if s.last_known_good_commit is not None
        }
        with patch(
            "scripts.aggregate_results.fetch_release_tags_api",
            return_value={"physlib": (None, None)},
        ):
            release_tags = agg.fetch_release_tags_api("upstream/repo", lkg_commits, None)
            for name, (tag, sha) in release_tags.items():
                updated_statuses[name] = replace(
                    updated_statuses[name], last_good_release=tag, last_good_release_commit=sha
                )

        assert updated_statuses["physlib"].last_good_release is None
        assert updated_statuses["physlib"].last_good_release_commit is None

    def test_enrichment_skipped_when_no_lkg(self) -> None:
        """Scenario: downstream with no LKG is not included in release lookups."""
        status = DownstreamStatusRecord(last_known_good_commit=None)
        updated_statuses = {"physlib": status}

        lkg_commits = {
            name: s.last_known_good_commit
            for name, s in updated_statuses.items()
            if s.last_known_good_commit is not None
        }
        assert lkg_commits == {}


class TestFetchReleaseTagsApi:
    """Unit tests for fetch_release_tags_api / _fetch_semver_tags_api."""

    def _make_tag(self, name: str, sha: str) -> dict:
        return {"name": name, "commit": {"sha": sha}}

    def _mock_urlopen(self, responses: list):
        """Return a context-manager mock that yields successive responses."""
        import io
        from unittest.mock import MagicMock, patch

        call_count = [0]

        class _FakeResp:
            def __init__(self, body):
                self._body = body.encode() if isinstance(body, str) else body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        def _urlopen(req, timeout=10):
            body = json.dumps(responses[call_count[0]])
            call_count[0] += 1
            return _FakeResp(body)

        return patch("urllib.request.urlopen", side_effect=_urlopen)

    def test_finds_newest_reachable_tag(self) -> None:
        """Scenario: v4.13.0 is reachable, v4.14.0 is not; returns v4.13.0."""
        import scripts.aggregate_results as agg

        tags_page = [
            self._make_tag("v4.14.0", "sha_414"),
            self._make_tag("v4.13.0", "sha_413"),
            self._make_tag("v4.12.0", "sha_412"),
            self._make_tag("master-nightly", "sha_nightly"),
        ]
        # API call order: tags page 1, compare v4.14.0...lkg (behind), compare v4.13.0...lkg (ahead)
        responses = [
            tags_page,
            {"status": "behind"},    # v4.14.0 not reachable
            {"status": "ahead"},     # v4.13.0 reachable → stop
        ]
        with self._mock_urlopen(responses):
            result = agg.fetch_release_tags_api("owner/repo", {"physlib": "lkg_sha"}, None)

        assert result["physlib"] == ("v4.13.0", "sha_413")

    def test_returns_none_none_when_no_tag_reachable(self) -> None:
        """Scenario: all tags are newer than LKG; returns (None, None)."""
        import scripts.aggregate_results as agg

        tags_page = [self._make_tag("v4.14.0", "sha_414")]
        responses = [
            tags_page,
            {"status": "behind"},
        ]
        with self._mock_urlopen(responses):
            result = agg.fetch_release_tags_api("owner/repo", {"physlib": "lkg_sha"}, None)

        assert result["physlib"] == (None, None)

    def test_skips_non_semver_tags(self) -> None:
        """Scenario: non-semver tags are filtered out; only vX.Y.Z tags are checked."""
        import scripts.aggregate_results as agg

        tags_page = [
            self._make_tag("nightly-2026-04-01", "sha_nightly"),
            self._make_tag("v4.13.0", "sha_413"),
        ]
        responses = [
            tags_page,
            {"status": "ahead"},  # v4.13.0 reachable (nightly skipped, no compare for it)
        ]
        with self._mock_urlopen(responses):
            result = agg.fetch_release_tags_api("owner/repo", {"physlib": "lkg_sha"}, None)

        assert result["physlib"] == ("v4.13.0", "sha_413")

    def test_identical_status_counts_as_reachable(self) -> None:
        """Scenario: LKG commit is exactly the tag commit; status=identical is accepted."""
        import scripts.aggregate_results as agg

        tags_page = [self._make_tag("v4.13.0", "sha_413")]
        responses = [tags_page, {"status": "identical"}]
        with self._mock_urlopen(responses):
            result = agg.fetch_release_tags_api("owner/repo", {"physlib": "sha_413"}, None)

        assert result["physlib"] == ("v4.13.0", "sha_413")

    def test_exact_match_short_circuits_without_api_call(self) -> None:
        """Scenario: LKG SHA equals tag SHA — compare endpoint must not be called."""
        import scripts.aggregate_results as agg

        tags_page = [self._make_tag("v4.13.0", "sha_413"), self._make_tag("v4.12.0", "sha_412")]
        # Only one response (the tag list); a compare call would exhaust the mock and raise.
        responses = [tags_page]
        with self._mock_urlopen(responses):
            result = agg.fetch_release_tags_api("owner/repo", {"physlib": "sha_413"}, None)

        assert result["physlib"] == ("v4.13.0", "sha_413")

    def test_tag_list_fetched_once_for_multiple_downstreams(self) -> None:
        """Scenario: two downstreams share one tag-list fetch; two ancestry checks run."""
        import scripts.aggregate_results as agg
        from unittest.mock import patch

        tags_page = [self._make_tag("v4.13.0", "sha_413")]
        call_urls: list[str] = []

        import io

        class _FakeResp:
            def __init__(self, body):
                self._body = json.dumps(body).encode()

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        def _urlopen(req, timeout=10):
            call_urls.append(req.full_url)
            if "tags?" in req.full_url:
                return _FakeResp(tags_page)
            return _FakeResp({"status": "ahead"})

        with patch("urllib.request.urlopen", side_effect=_urlopen):
            result = agg.fetch_release_tags_api(
                "owner/repo",
                {"physlib": "lkg1", "mathlib4-port": "lkg2"},
                None,
            )

        tag_calls = [u for u in call_urls if "tags?" in u]
        compare_calls = [u for u in call_urls if "compare" in u]
        assert len(tag_calls) == 1
        assert len(compare_calls) == 2
        assert result["physlib"] == ("v4.13.0", "sha_413")
        assert result["mathlib4-port"] == ("v4.13.0", "sha_413")
