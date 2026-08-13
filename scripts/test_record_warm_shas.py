#!/usr/bin/env python3
"""
Tests for: scripts.record_warm_shas

Coverage scope:
    - ``collect_terminal_results`` — terminal-status filter + dedup applied
      to the ``warm-mathlib-cache.yml`` ``finalize`` job's ``summary.json``.

Out of scope:
    - ``main()``: the CLI entry point reads files, constructs a backend,
      and calls ``backend.record_warmth_results``.  Backend behavior is
      covered in ``test_storage.py`` and the file-loading path is a thin
      argparse shim — exercising it would require a live SQL backend, which
      the unit suite intentionally avoids.  See "Out of scope for the unit
      suite" in ``conftest.py``.

Why this matters
----------------
``cache_warmth`` rows carry each attempted SHA's terminal status: warm
rows let ``plan_cache_warm_jobs`` skip verified SHAs forever, failed rows
feed its backoff retry schedule.  ``TERMINAL_STATUSES`` is a closed set
that pairs the verified-warm path (``already_warm`` / ``warmed``) with the
failed-attempt path (``build_failed`` / ``push_failed`` /
``verify_failed``).  Letting a non-terminal status slip in (``no_result``,
``staged``, or the planner's own skip statuses) would record an attempt
that never happened — these tests are the contract that guards against
that drift.
"""

from __future__ import annotations

import pytest

from scripts.conftest import SHA_A, SHA_B, SHA_C
from scripts.record_warm_shas import TERMINAL_STATUSES, collect_terminal_results


class TestCollectTerminalResultsFilter:
    """Tests covering which statuses are recorded as terminal."""

    def test_collect_terminal_results_with_success_statuses_records_both(self) -> None:
        """
        ``already_warm`` (probe found a populated cache) and ``warmed``
        (we built and uploaded) are the verified-warm terminal statuses
        documented in ``warm-mathlib-cache.yml``.  Dropping either from
        ``TERMINAL_STATUSES`` would cause the affected category of SHAs
        to be re-attempted every planning pass forever.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "already_warm"},
            {"sha": SHA_B, "status": "warmed"},
        ]

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {SHA_A: "already_warm", SHA_B: "warmed"}, (
            "Both already_warm and warmed are terminal; both must be "
            "returned with their status verbatim"
        )

    def test_collect_terminal_results_with_failure_statuses_records_all(self) -> None:
        """
        ``build_failed``, ``push_failed``, and ``verify_failed`` represent
        SHAs we tried twice in-job without success.  They must be recorded
        into ``cache_warmth`` with their status so the planner paces the
        re-attempts through its backoff schedule — unrecorded, a sticky
        infra failure would burn self-hosted runner time every 6h tick.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "build_failed"},
            {"sha": SHA_B, "status": "push_failed"},
            {"sha": SHA_C, "status": "verify_failed"},
        ]

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {
            SHA_A: "build_failed",
            SHA_B: "push_failed",
            SHA_C: "verify_failed",
        }, (
            "Failed-attempt statuses are terminal for this run — recording "
            "them with status feeds the planner's backoff retry schedule"
        )

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param("no_result", id="no_result_runner_died"),
            pytest.param("staged", id="staged_intermediate"),
            pytest.param("cache_warmth_hit", id="planner_skip_verified_warm"),
            pytest.param("retry_backoff", id="planner_skip_backoff"),
            pytest.param("retry_exhausted", id="planner_skip_exhausted"),
        ],
    )
    def test_collect_terminal_results_excludes_non_terminal_statuses(
        self, status: str
    ) -> None:
        """
        ``no_result`` means the matrix entry didn't produce a result at all
        (runner died, timeout, infra blip) — we have no signal and should
        retry next tick without consuming retry budget.  ``staged`` is a
        non-terminal intermediate state within ``build_and_stage``.  The
        planner's own skip statuses (``cache_warmth_hit`` /
        ``retry_backoff`` / ``retry_exhausted``) describe SHAs that did NOT
        run this tick; recording them would inflate attempt counters for
        attempts that never happened.  None of these may land in
        ``cache_warmth``.
        """
        # Arrange
        summary = [{"sha": SHA_A, "status": status}]

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {}, (
            f"{status} is non-terminal — recording it would corrupt the "
            "attempt accounting for a SHA that didn't run"
        )


class TestCollectTerminalResultsDedup:
    """Tests for dedup ordering and identity semantics."""

    def test_collect_terminal_results_with_duplicate_sha_keeps_first_occurrence(
        self,
    ) -> None:
        """
        The same SHA can legitimately appear multiple times in one summary
        (e.g. as both LKG for project A and FKB for project B in a fan-out
        matrix).  Dedup is "first occurrence wins" — preserving input order
        keeps the recorded mapping deterministic for log readability and
        pins which status survives when duplicates disagree.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "warmed"},
            {"sha": SHA_A, "status": "already_warm"},
        ]

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {SHA_A: "warmed"}, (
            "Duplicates collapse to a single entry with the first status"
        )

    def test_collect_terminal_results_dedups_across_success_and_failure(self) -> None:
        """
        A SHA can plausibly appear once as ``warmed`` (one fan-out entry)
        and once as ``build_failed`` (another) within the same run if the
        matrix construction ever produces it twice with differing outcomes.
        Dedup must still collapse to one row — the table primary key is
        ``(upstream, sha)`` and can't carry both.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "warmed"},
            {"sha": SHA_A, "status": "build_failed"},
        ]

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {SHA_A: "warmed"}, (
            "Same SHA across success and failure rows collapses to one entry"
        )


class TestCollectTerminalResultsMissingSha:
    """Tests for entries that arrive without a usable SHA field."""

    # The production docstring on collect_terminal_results spells out the
    # silent-skip-on-missing-sha contract; this class is its executable
    # form so any future loosening of that contract fails here first.

    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param([{"sha": "", "status": "warmed"}], id="empty_sha_string"),
            pytest.param([{"status": "warmed"}], id="sha_key_absent"),
        ],
    )
    def test_collect_terminal_results_with_missing_or_empty_sha_skips_entry(
        self, summary
    ) -> None:
        """
        A summary entry with no usable SHA value is silently dropped.  This
        is defensive: the schema *should* always include a SHA, but if
        ``warm-mathlib-cache.yml``'s shell-level summary builder ever emits
        a malformed row we prefer to skip it rather than crash the
        ``finalize`` job and lose the rest of the recording.
        """
        # Arrange — the parametrize id is the human label for the case.

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {}, (
            "Entry without a usable SHA must be skipped without raising"
        )

    def test_collect_terminal_results_skips_invalid_entries_but_preserves_valid_ones(
        self,
    ) -> None:
        """
        A mixed summary (some malformed entries, some valid) must still
        record the valid ones.  A single bad row in a 100-row summary
        cannot cost us the whole recording.
        """
        # Arrange
        summary = [
            {"sha": "", "status": "warmed"},
            {"status": "warmed"},
            {"sha": SHA_A, "status": "warmed"},
        ]

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {SHA_A: "warmed"}, (
            "Malformed entries are skipped silently while valid ones are kept"
        )


class TestCollectTerminalResultsEmptyInput:
    """Tests for the edge case where the summary file is empty."""

    def test_collect_terminal_results_with_empty_summary_returns_empty_dict(
        self,
    ) -> None:
        """
        An empty ``summary.json`` (the orchestrator ran but produced no
        per-SHA rows — e.g. the matrix was filtered down to nothing by the
        ``cache_warmth`` table) must return an empty dict, not raise.
        ``main()`` then short-circuits with "No terminal statuses to
        record." and leaves the database untouched.
        """
        # Arrange
        summary: list[dict] = []

        # Act
        result = collect_terminal_results(summary)

        # Assert
        assert result == {}, "Empty input must yield empty output"


class TestTerminalStatusesConstant:
    """Tests that pin ``TERMINAL_STATUSES`` against accidental drift."""

    def test_terminal_statuses_pins_membership(self) -> None:
        """
        ``TERMINAL_STATUSES`` is the contract this module shares with
        ``warm-mathlib-cache.yml``.  Pinning the contents (not just
        membership) means a maintainer who adds a new status to
        ``TERMINAL_STATUSES`` is forced to update this test, which forces
        them to think about whether the new status is genuinely the
        outcome of a warming attempt (recordable) versus a no-signal or
        didn't-run state (``no_result``, planner skips).
        """
        # Arrange / Act / Assert
        assert TERMINAL_STATUSES == frozenset({
            "already_warm",
            "warmed",
            "build_failed",
            "push_failed",
            "verify_failed",
        }), (
            "TERMINAL_STATUSES is the contract with warm-mathlib-cache.yml; "
            "any change should be intentional and propagated to the workflow"
        )

    def test_no_result_is_not_terminal(self) -> None:
        """
        ``no_result`` is the one worker status that MUST remain
        unrecorded.  It means the matrix entry didn't produce a
        result.json (runner died, timeout, infra), and unlike ``*_failed``
        there was no observed attempt to count against the SHA's retry
        budget — we just didn't get to find out.
        """
        # Arrange / Act / Assert
        assert "no_result" not in TERMINAL_STATUSES, (
            "no_result must stay unrecorded so a dead-runner attempt is "
            "retried next tick at full budget, not counted as a failure"
        )
