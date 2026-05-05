#!/usr/bin/env python3
"""Tests for plan_cache_warm_jobs.py — matrix builder for cache warming."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.models import DownstreamConfig
from scripts.plan_cache_warm_jobs import (
    _parse_manual_shas,
    build_matrix_from_db,
    build_matrix_manual,
)
from scripts.storage import DownstreamStatusRecord


_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


def _config(name: str, *, warm_cache: bool = True) -> DownstreamConfig:
    """Minimal DownstreamConfig with the warm_cache flag dialed in."""
    return DownstreamConfig(
        name=name,
        repo=f"org/{name}",
        default_branch="main",
        dependency_name="mathlib",
        warm_cache=warm_cache,
    )


class ParseManualShasTests(unittest.TestCase):
    """Tests for _parse_manual_shas — input validation and dedup."""

    def test_accepts_valid_lowercase_hex(self) -> None:
        """Scenario: comma-separated 40-hex SHAs round-trip."""
        self.assertEqual(_parse_manual_shas(f"{_SHA_A},{_SHA_B}"), [_SHA_A, _SHA_B])

    def test_normalises_uppercase(self) -> None:
        """Scenario: uppercase hex is lowercased."""
        self.assertEqual(_parse_manual_shas(_SHA_A.upper()), [_SHA_A])

    def test_strips_whitespace_and_blanks(self) -> None:
        """Scenario: surrounding whitespace and empty tokens are ignored."""
        self.assertEqual(
            _parse_manual_shas(f"  {_SHA_A} , , {_SHA_B}  "),
            [_SHA_A, _SHA_B],
        )

    def test_dedups_preserving_order(self) -> None:
        """Scenario: repeated SHAs collapse, first occurrence wins position."""
        self.assertEqual(
            _parse_manual_shas(f"{_SHA_B},{_SHA_A},{_SHA_B}"),
            [_SHA_B, _SHA_A],
        )

    def test_rejects_short_sha(self) -> None:
        """Scenario: a 7-char SHA is rejected with a clear error."""
        with self.assertRaises(ValueError):
            _parse_manual_shas("abc1234")

    def test_rejects_non_hex(self) -> None:
        """Scenario: 40 non-hex chars are rejected."""
        with self.assertRaises(ValueError):
            _parse_manual_shas("z" * 40)


class BuildMatrixManualTests(unittest.TestCase):
    """Tests for build_matrix_manual — manual-mode passthrough."""

    def test_emits_one_entry_per_sha_with_manual_tag(self) -> None:
        """Scenario: each manual SHA gets tag=manual and empty downstreams."""
        self.assertEqual(
            build_matrix_manual([_SHA_A, _SHA_B]),
            [
                {"sha": _SHA_A, "tag": "manual", "downstreams": []},
                {"sha": _SHA_B, "tag": "manual", "downstreams": []},
            ],
        )

    def test_empty_input_yields_empty_matrix(self) -> None:
        """Scenario: no SHAs in, no entries out."""
        self.assertEqual(build_matrix_manual([]), [])


class BuildMatrixFromDbTests(unittest.TestCase):
    """Tests for build_matrix_from_db — opt-in filter, dedup, role tagging."""

    def test_skips_downstreams_without_warm_cache_opt_in(self) -> None:
        """Scenario: warm_cache=False entries contribute no SHAs."""
        inventory = {"physlib": _config("physlib", warm_cache=False)}
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit=_SHA_A,
                first_known_bad_commit=_SHA_B,
            ),
        }
        self.assertEqual(build_matrix_from_db(inventory, statuses), [])

    def test_lkg_only_yields_lkg_tag(self) -> None:
        """Scenario: a SHA that's only an LKG is tagged 'lkg'."""
        inventory = {"physlib": _config("physlib")}
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=_SHA_A),
        }
        self.assertEqual(
            build_matrix_from_db(inventory, statuses),
            [{"sha": _SHA_A, "tag": "lkg", "downstreams": ["physlib"]}],
        )

    def test_fkb_only_yields_fkb_tag(self) -> None:
        """Scenario: a SHA that's only an FKB is tagged 'fkb'."""
        inventory = {"physlib": _config("physlib")}
        statuses = {
            "physlib": DownstreamStatusRecord(first_known_bad_commit=_SHA_A),
        }
        self.assertEqual(
            build_matrix_from_db(inventory, statuses),
            [{"sha": _SHA_A, "tag": "fkb", "downstreams": ["physlib"]}],
        )

    def test_same_sha_lkg_for_one_fkb_for_another_yields_both(self) -> None:
        """Scenario: a SHA that's LKG for project A and FKB for project B is 'both'."""
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=_SHA_A),
            "FLT": DownstreamStatusRecord(first_known_bad_commit=_SHA_A),
        }
        result = build_matrix_from_db(inventory, statuses)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sha"], _SHA_A)
        self.assertEqual(result[0]["tag"], "both")
        self.assertEqual(sorted(result[0]["downstreams"]), ["FLT", "physlib"])

    def test_dedup_across_downstreams(self) -> None:
        """Scenario: the same LKG shared by two downstreams produces one entry."""
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=_SHA_A),
            "FLT": DownstreamStatusRecord(last_known_good_commit=_SHA_A),
        }
        result = build_matrix_from_db(inventory, statuses)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tag"], "lkg")
        self.assertEqual(sorted(result[0]["downstreams"]), ["FLT", "physlib"])

    def test_distinct_lkg_and_fkb_for_same_downstream(self) -> None:
        """Scenario: one downstream's LKG and FKB land on different SHAs → two entries."""
        inventory = {"physlib": _config("physlib")}
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit=_SHA_A,
                first_known_bad_commit=_SHA_B,
            ),
        }
        result = build_matrix_from_db(inventory, statuses)
        self.assertEqual(
            sorted((e["sha"], e["tag"]) for e in result),
            [(_SHA_A, "lkg"), (_SHA_B, "fkb")],
        )

    def test_null_commits_are_skipped(self) -> None:
        """Scenario: a status with both fields None contributes nothing."""
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord()}
        self.assertEqual(build_matrix_from_db(inventory, statuses), [])

    def test_downstream_missing_from_statuses(self) -> None:
        """Scenario: an opted-in downstream with no DB row is silently skipped."""
        inventory = {"physlib": _config("physlib")}
        self.assertEqual(build_matrix_from_db(inventory, statuses={}), [])

    def test_output_is_sorted_by_sha(self) -> None:
        """Scenario: matrix entries are emitted in deterministic SHA order."""
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=_SHA_C),
            "FLT": DownstreamStatusRecord(last_known_good_commit=_SHA_A),
        }
        shas = [entry["sha"] for entry in build_matrix_from_db(inventory, statuses)]
        self.assertEqual(shas, sorted(shas))


if __name__ == "__main__":
    unittest.main()
