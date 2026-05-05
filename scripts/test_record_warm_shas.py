#!/usr/bin/env python3
"""Tests for record_warm_shas.collect_warm_shas."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.record_warm_shas import collect_warm_shas


_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


class CollectWarmShasTests(unittest.TestCase):
    """Tests for collect_warm_shas — terminal-warm filter."""

    def test_picks_already_warm_and_warmed(self) -> None:
        """Scenario: both terminal-warm statuses are recorded."""
        summary = [
            {"sha": _SHA_A, "status": "already_warm"},
            {"sha": _SHA_B, "status": "warmed"},
        ]
        self.assertEqual(collect_warm_shas(summary), [_SHA_A, _SHA_B])

    def test_drops_failure_and_intermediate_statuses(self) -> None:
        """Scenario: failed/non-terminal statuses do not record warmth."""
        summary = [
            {"sha": _SHA_A, "status": "build_failed"},
            {"sha": _SHA_B, "status": "push_failed"},
            {"sha": _SHA_C, "status": "verify_failed"},
            {"sha": "d" * 40, "status": "no_result"},
            {"sha": "e" * 40, "status": "staged"},
        ]
        self.assertEqual(collect_warm_shas(summary), [])

    def test_dedups(self) -> None:
        """Scenario: a SHA appearing multiple times is recorded once."""
        summary = [
            {"sha": _SHA_A, "status": "warmed"},
            {"sha": _SHA_A, "status": "already_warm"},
        ]
        self.assertEqual(collect_warm_shas(summary), [_SHA_A])

    def test_skips_entries_without_sha(self) -> None:
        """Scenario: entries with missing/empty SHA are silently dropped."""
        summary = [
            {"sha": "", "status": "warmed"},
            {"status": "warmed"},
            {"sha": _SHA_A, "status": "warmed"},
        ]
        self.assertEqual(collect_warm_shas(summary), [_SHA_A])

    def test_empty_summary(self) -> None:
        """Scenario: empty list produces no SHAs."""
        self.assertEqual(collect_warm_shas([]), [])


if __name__ == "__main__":
    unittest.main()
