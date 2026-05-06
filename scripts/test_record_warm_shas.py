#!/usr/bin/env python3
"""
Tests for: scripts.record_warm_shas

Coverage scope:
    - ``collect_warm_shas`` — terminal-warm filter + dedup applied to the
      ``warm-mathlib-cache.yml`` ``finalize`` job's ``summary.json``.

Out of scope:
    - ``main()``: the CLI entry point reads files, constructs a backend,
      and calls ``backend.record_warm_shas``.  Backend behavior is covered
      in ``test_storage.py`` and the file-loading path is a thin argparse
      shim — exercising it would require a live SQL backend, which the
      unit suite intentionally avoids.  See "Out of scope for the unit
      suite" in ``conftest.py``.

Why this matters
----------------
``cache_warmth`` rows are the dedup key that lets ``plan_cache_warm_jobs``
stop re-warming SHAs we have already confirmed warm.  If
``collect_warm_shas`` ever silently records a non-terminal status (e.g.
``staged``), a transiently-staged-then-failed SHA would be skipped on the
next planning pass and never actually warmed — the stored ``cache_warmth``
row would be a lie.  These tests are therefore the contract that
``WARM_STATUSES`` is a closed set of *confirmed-warm* statuses.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.conftest import SHA_A, SHA_B, SHA_C, SHA_D, SHA_E
from scripts.record_warm_shas import WARM_STATUSES, collect_warm_shas


class TestCollectWarmShasTerminalFilter(unittest.TestCase):
    """Tests covering which statuses are recorded as terminally warm."""

    def test_collect_warm_shas_with_already_warm_and_warmed_records_both(self) -> None:
        """
        Both ``already_warm`` (probe found a populated cache) and ``warmed``
        (we built and uploaded) are documented terminal-warm statuses in
        ``warm-mathlib-cache.yml``.  This guards against either being dropped
        from ``WARM_STATUSES`` by accident — if that happened, the affected
        category of SHAs would be re-warmed every planning pass forever.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "already_warm"},
            {"sha": SHA_B, "status": "warmed"},
        ]

        # Act
        result = collect_warm_shas(summary)

        # Assert
        assert result == [SHA_A, SHA_B], (
            "Both already_warm and warmed are terminal-warm; both must be "
            "returned in input order"
        )

    def test_collect_warm_shas_with_only_failure_or_intermediate_statuses_returns_empty(
        self,
    ) -> None:
        """
        Non-terminal statuses (``staged``, ``no_result``) and explicit failure
        statuses (``build_failed``, ``push_failed``, ``verify_failed``) must
        never become ``cache_warmth`` rows.  Recording a failed/staged SHA
        would lie to the next planner about cache state.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "build_failed"},
            {"sha": SHA_B, "status": "push_failed"},
            {"sha": SHA_C, "status": "verify_failed"},
            {"sha": SHA_D, "status": "no_result"},
            {"sha": SHA_E, "status": "staged"},
        ]

        # Act
        result = collect_warm_shas(summary)

        # Assert
        assert result == [], (
            "No status in the summary is terminal-warm; the result must be "
            "empty rather than partial"
        )


class TestCollectWarmShasDedup(unittest.TestCase):
    """Tests for dedup ordering and identity semantics."""

    def test_collect_warm_shas_with_duplicate_sha_returns_single_first_occurrence(
        self,
    ) -> None:
        """
        The same SHA can legitimately appear multiple times in one summary
        (e.g. as both LKG for project A and FKB for project B in a fan-out
        matrix).  Dedup is "first occurrence wins" — preserving input order
        keeps the recorded list deterministic for log readability and for
        any downstream consumer that might (today or later) care about
        ordering.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "warmed"},
            {"sha": SHA_A, "status": "already_warm"},
        ]

        # Act
        result = collect_warm_shas(summary)

        # Assert
        assert result == [SHA_A], (
            "Duplicates collapse to a single entry in first-occurrence order"
        )


# NOTE: this group uses a bare class (not unittest.TestCase) so that
# @pytest.mark.parametrize can drive the cases.  The unittest framework
# does not honour parametrize on TestCase methods; mixing the two styles
# in one file is fine and keeps the tabular cases legible.
class TestCollectWarmShasMissingSha:
    """Tests for entries that arrive without a usable SHA field."""

    # NOTE: the production docstring on collect_warm_shas carries a FIXME
    # acknowledging that it does not document the silent-skip behaviour for
    # missing/empty SHAs.  This class is the executable contract for that
    # behaviour until the docstring catches up.

    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param([{"sha": "", "status": "warmed"}], id="empty_sha_string"),
            pytest.param([{"status": "warmed"}], id="sha_key_absent"),
        ],
    )
    def test_collect_warm_shas_with_missing_or_empty_sha_skips_entry(
        self, summary
    ) -> None:
        """
        A summary entry with no usable SHA value is silently dropped.  This
        is defensive: the schema *should* always include a SHA, but if
        ``warm-mathlib-cache.yml``'s shell-level summary builder ever emits
        a malformed row we prefer to skip it rather than crash the
        ``finalize`` job and lose the rest of the warmth recording.
        """
        # Arrange — the parametrize id is the human label for the case.

        # Act
        result = collect_warm_shas(summary)

        # Assert
        assert result == [], (
            "Entry without a usable SHA must be skipped without raising"
        )

    def test_collect_warm_shas_skips_invalid_entries_but_preserves_valid_ones(
        self,
    ) -> None:
        """
        A mixed summary (some malformed entries, some valid) must still
        record the valid ones.  A single bad row in a 100-row summary
        cannot cost us the whole warmth recording.
        """
        # Arrange
        summary = [
            {"sha": "", "status": "warmed"},
            {"status": "warmed"},
            {"sha": SHA_A, "status": "warmed"},
        ]

        # Act
        result = collect_warm_shas(summary)

        # Assert
        assert result == [SHA_A], (
            "Malformed entries are skipped silently while valid ones are kept"
        )


class TestCollectWarmShasEmptyInput(unittest.TestCase):
    """Tests for the edge case where the summary file is empty."""

    def test_collect_warm_shas_with_empty_summary_returns_empty_list(self) -> None:
        """
        An empty ``summary.json`` (the orchestrator ran but produced no
        per-SHA rows — e.g. the matrix was filtered down to nothing by the
        ``cache_warmth`` table) must return an empty list, not raise.
        ``main()`` then short-circuits with "No warm SHAs to record." and
        leaves the database untouched.
        """
        # Arrange
        summary: list[dict] = []

        # Act
        result = collect_warm_shas(summary)

        # Assert
        assert result == [], "Empty input must yield empty output"


class TestWarmStatusesConstant(unittest.TestCase):
    """Tests that pin the ``WARM_STATUSES`` constant against accidental drift."""

    def test_warm_statuses_contains_exactly_already_warm_and_warmed(self) -> None:
        """
        ``WARM_STATUSES`` is the contract this module shares with
        ``warm-mathlib-cache.yml``.  Pinning the contents (not just
        membership) means a maintainer who adds a new status to
        ``WARM_STATUSES`` is forced to update this test, which forces them
        to think about whether the new status is genuinely terminal-warm
        and whether ``record_warm_shas.py``'s caller is emitting it.
        """
        # Arrange / Act / Assert
        assert WARM_STATUSES == frozenset({"already_warm", "warmed"}), (
            "WARM_STATUSES is the contract with warm-mathlib-cache.yml; "
            "any change should be intentional and propagated to the workflow"
        )


if __name__ == "__main__":
    unittest.main()
