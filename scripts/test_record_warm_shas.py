#!/usr/bin/env python3
"""
Tests for: scripts.record_warm_shas

Coverage scope:
    - ``collect_terminal_shas`` — terminal-status filter + dedup applied to
      the ``warm-mathlib-cache.yml`` ``finalize`` job's ``summary.json``.

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
stop re-attempting SHAs we are already done with.  ``TERMINAL_STATUSES``
is a closed set that pairs the success path (``already_warm`` / ``warmed``)
with the post-retry give-up path (``build_failed`` / ``push_failed`` /
``verify_failed``).  Letting a non-terminal status slip in (``no_result``,
``staged``) would silently skip a SHA we genuinely should retry — these
tests are the contract that guards against that drift.
"""

from __future__ import annotations

import pytest

from scripts.conftest import SHA_A, SHA_B, SHA_C
from scripts.record_warm_shas import TERMINAL_STATUSES, collect_terminal_shas


class TestCollectTerminalShasFilter:
    """Tests covering which statuses are recorded as terminal."""

    def test_collect_terminal_shas_with_success_statuses_records_both(self) -> None:
        """
        ``already_warm`` (probe found a populated cache) and ``warmed``
        (we built and uploaded) are the success-path terminal statuses
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
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [SHA_A, SHA_B], (
            "Both already_warm and warmed are terminal; both must be "
            "returned in input order"
        )

    def test_collect_terminal_shas_with_failure_statuses_records_all(self) -> None:
        """
        ``build_failed``, ``push_failed``, and ``verify_failed`` represent
        SHAs we tried twice in-job and gave up on.  They must be recorded
        into ``cache_warmth`` so the next planning tick doesn't re-attempt
        them — otherwise a sticky failure (e.g. ProofWidgets release pruned)
        would burn self-hosted runner time every 6h indefinitely.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "build_failed"},
            {"sha": SHA_B, "status": "push_failed"},
            {"sha": SHA_C, "status": "verify_failed"},
        ]

        # Act
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [SHA_A, SHA_B, SHA_C], (
            "Post-retry failure statuses are terminal — recording them "
            "stops the every-6h retry loop on sticky failures"
        )

    def test_collect_terminal_shas_with_no_result_or_staged_returns_empty(
        self,
    ) -> None:
        """
        ``no_result`` means the matrix entry didn't produce a result at all
        (runner died, timeout, infra blip) — we have no signal and should
        retry next tick.  ``staged`` is a non-terminal intermediate state
        within ``build_and_stage``.  Neither should land in ``cache_warmth``.
        """
        # Arrange
        summary = [
            {"sha": SHA_A, "status": "no_result"},
            {"sha": SHA_B, "status": "staged"},
        ]

        # Act
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [], (
            "no_result and staged are non-terminal — recording either would "
            "skip a SHA we should still attempt"
        )


class TestCollectTerminalShasDedup:
    """Tests for dedup ordering and identity semantics."""

    def test_collect_terminal_shas_with_duplicate_sha_returns_single_first_occurrence(
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
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [SHA_A], (
            "Duplicates collapse to a single entry in first-occurrence order"
        )

    def test_collect_terminal_shas_dedups_across_success_and_failure(self) -> None:
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
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [SHA_A], (
            "Same SHA across success and failure rows collapses to one entry"
        )


class TestCollectTerminalShasMissingSha:
    """Tests for entries that arrive without a usable SHA field."""

    # The production docstring on collect_terminal_shas spells out the
    # silent-skip-on-missing-sha contract; this class is its executable
    # form so any future loosening of that contract fails here first.

    @pytest.mark.parametrize(
        "summary",
        [
            pytest.param([{"sha": "", "status": "warmed"}], id="empty_sha_string"),
            pytest.param([{"status": "warmed"}], id="sha_key_absent"),
        ],
    )
    def test_collect_terminal_shas_with_missing_or_empty_sha_skips_entry(
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
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [], (
            "Entry without a usable SHA must be skipped without raising"
        )

    def test_collect_terminal_shas_skips_invalid_entries_but_preserves_valid_ones(
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
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [SHA_A], (
            "Malformed entries are skipped silently while valid ones are kept"
        )


class TestCollectTerminalShasEmptyInput:
    """Tests for the edge case where the summary file is empty."""

    def test_collect_terminal_shas_with_empty_summary_returns_empty_list(self) -> None:
        """
        An empty ``summary.json`` (the orchestrator ran but produced no
        per-SHA rows — e.g. the matrix was filtered down to nothing by the
        ``cache_warmth`` table) must return an empty list, not raise.
        ``main()`` then short-circuits with "No terminal SHAs to record."
        and leaves the database untouched.
        """
        # Arrange
        summary: list[dict] = []

        # Act
        result = collect_terminal_shas(summary)

        # Assert
        assert result == [], "Empty input must yield empty output"


class TestTerminalStatusesConstant:
    """Tests that pin ``TERMINAL_STATUSES`` against accidental drift."""

    def test_terminal_statuses_pins_membership(self) -> None:
        """
        ``TERMINAL_STATUSES`` is the contract this module shares with
        ``warm-mathlib-cache.yml``.  Pinning the contents (not just
        membership) means a maintainer who adds a new status to
        ``TERMINAL_STATUSES`` is forced to update this test, which forces
        them to think about whether the new status is genuinely terminal
        (we don't want to retry) versus retry-able (``no_result``).
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
        ``no_result`` is the one status that MUST remain retry-able.  It
        means the matrix entry didn't produce a result.json (runner died,
        timeout, infra), and unlike ``*_failed`` we have no signal that the
        SHA is permanently unwarmable — we just didn't get to find out.
        """
        # Arrange / Act / Assert
        assert "no_result" not in TERMINAL_STATUSES, (
            "no_result must remain non-terminal so failed runner attempts "
            "get retried, not silently filed under 'done'"
        )
