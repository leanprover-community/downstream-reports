#!/usr/bin/env python3
"""
Tests for: scripts.plan_cache_warm_jobs

Coverage scope:
    - ``_parse_manual_shas`` — input validation, normalisation, dedup of
      the ``--manual-shas`` CLI argument used to bypass the inventory
      filter for forced re-warms.
    - ``build_matrix_manual`` — passthrough builder that emits one entry
      per manual SHA tagged ``manual``.
    - ``build_matrix_from_db`` — the steady-state planner: reads
      ``downstream_status`` for opted-in downstreams, dedups LKG/FKB
      across them, drops SHAs already recorded in ``cache_warmth``, and
      tags each entry by the role(s) it plays.

Out of scope:
    - ``main()`` and ``build_parser()`` — argparse + I/O glue.  The
      end-to-end behaviour is exercised by the workflow itself; the unit
      suite focuses on the matrix-building logic that the workflow can't
      easily assert against.
    - ``SqlBackend.load_known_warm_shas`` — covered indirectly here via
      the ``known_warm_shas`` parameter and lives in ``test_storage.py``
      for the SQL side.

Why this matters
----------------
The matrix is the contract with ``warm-mathlib-cache.yml``: a SHA listed
in ``include`` will be cloned, built, and pushed to the shared Azure
cache.  A SHA listed in ``skipped_warm`` will be reported as
``cache_warmth_hit`` in the finalize summary.  Misclassifying a cold
SHA as warm causes ``publish-lkg`` to advertise a SHA whose Azure cache
is empty — exactly the cold-SHA contract violation the warming pipeline
is designed to prevent.  See ``docs/internal/cache-warming.md``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.conftest import SHA_A, SHA_B, SHA_C
from scripts.models import DownstreamConfig
from scripts.plan_cache_warm_jobs import (
    _parse_manual_shas,
    build_matrix_from_db,
    build_matrix_manual,
)
from scripts.storage import DownstreamStatusRecord


def _config(name: str, *, warm_cache: bool = True) -> DownstreamConfig:
    """Construct a minimal ``DownstreamConfig`` for matrix-building tests.

    What state it provides
    ----------------------
    A frozen ``DownstreamConfig`` whose ``warm_cache`` flag is the only
    knob most tests need to vary.  The other fields (``repo``,
    ``default_branch``, ``dependency_name``) take stable mathlib-shaped
    defaults so the test focus stays on matrix logic, not config
    plumbing.

    Why a factory rather than module-level fixtures
    -----------------------------------------------
    Most tests want two configs with different names, and a few want
    the same name with ``warm_cache`` flipped.  A factory expresses
    that variation directly without requiring callers to pre-build
    every combination.
    """
    return DownstreamConfig(
        name=name,
        repo=f"org/{name}",
        default_branch="main",
        dependency_name="mathlib",
        warm_cache=warm_cache,
    )


# ----------------------------------------------------------------------
# _parse_manual_shas — pure validation, parametrised heavily for the
# tabular input/output cases.
# ----------------------------------------------------------------------


class TestParseManualShasAcceptedInputs:
    """Tests covering the inputs ``_parse_manual_shas`` accepts."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            pytest.param(
                f"{SHA_A},{SHA_B}",
                [SHA_A, SHA_B],
                id="comma_separated_valid_lowercase_hex",
            ),
            pytest.param(
                SHA_A.upper(),
                [SHA_A],
                id="uppercase_normalised_to_lowercase",
            ),
            pytest.param(
                f"  {SHA_A} , , {SHA_B}  ",
                [SHA_A, SHA_B],
                id="whitespace_and_empty_tokens_stripped",
            ),
            pytest.param(
                f"{SHA_B},{SHA_A},{SHA_B}",
                [SHA_B, SHA_A],
                id="duplicates_collapse_first_occurrence_wins",
            ),
        ],
    )
    def test_parse_manual_shas_with_valid_inputs_normalises_and_dedups(
        self, raw: str, expected: list[str]
    ) -> None:
        """
        ``--manual-shas`` is a forensic / backfill tool.  The contract
        with the operator is that "I pasted a list of SHAs from a
        terminal" works regardless of capitalisation, surrounding
        whitespace, or accidental duplicates.

        Dedup is "first occurrence wins" — pinning that ordering means
        the workflow log lists SHAs in the same order the operator
        typed them, which is easier to scan and to cross-reference.
        """
        # Arrange / Act
        result = _parse_manual_shas(raw)

        # Assert
        assert result == expected, (
            f"_parse_manual_shas({raw!r}) should normalise to {expected!r}, got {result!r}"
        )


class TestParseManualShasRejectedInputs:
    """Tests covering the inputs ``_parse_manual_shas`` rejects loudly."""

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("abc1234", id="seven_char_short_sha"),
            pytest.param("z" * 40, id="forty_char_non_hex"),
        ],
    )
    def test_parse_manual_shas_with_invalid_inputs_raises_value_error(self, raw: str) -> None:
        """
        Short or non-hex SHAs are operator typos.  A silent skip would
        leave the planner emitting an empty matrix without telling the
        operator their request was rejected — they would assume the
        warming completed when nothing actually happened.  Raising
        crashes the planning step with the bad token visible in the log.
        """
        # Arrange / Act / Assert
        with pytest.raises(ValueError):
            _parse_manual_shas(raw)


# ----------------------------------------------------------------------
# build_matrix_manual — passthrough; small, but pinned because the
# emitted shape is the contract with the reusable per-SHA workflow.
# ----------------------------------------------------------------------


class TestBuildMatrixManual(unittest.TestCase):
    """Tests for ``build_matrix_manual`` — the bypass path."""

    def test_build_matrix_manual_emits_one_entry_per_sha_with_manual_tag(self) -> None:
        """
        Manual entries always carry ``tag="manual"`` and an empty
        ``downstreams`` list.  The reusable workflow uses these fields
        to render its summary; ``manual`` is the visible signal that the
        warming pass was operator-triggered, not a scheduled / report-
        triggered run.
        """
        # Arrange
        shas = [SHA_A, SHA_B]

        # Act
        matrix = build_matrix_manual(shas)

        # Assert
        self.assertEqual(
            matrix,
            [
                {"sha": SHA_A, "short_sha": SHA_A[:7], "tag": "manual", "downstreams": []},
                {"sha": SHA_B, "short_sha": SHA_B[:7], "tag": "manual", "downstreams": []},
            ],
            msg=(
                "Manual matrix entries are the contract with _warm-one-sha.yml; "
                "the four-field shape and `manual` tag must be stable"
            ),
        )

    def test_build_matrix_manual_with_empty_input_yields_empty_matrix(self) -> None:
        """
        An empty manual list produces an empty matrix — the workflow then
        short-circuits to the finalize job without spinning up any
        per-SHA runners.  Returning ``[]`` rather than raising lets the
        operator dispatch with an empty ``shas:`` input as a no-op.
        """
        # Arrange / Act
        matrix = build_matrix_manual([])

        # Assert
        self.assertEqual(matrix, [], msg="Empty input must yield an empty matrix")


# ----------------------------------------------------------------------
# build_matrix_from_db — the planner.  Most tests use unittest because
# they don't benefit from parametrize; the warm-filter cases at the
# bottom are tabular and use parametrize.
# ----------------------------------------------------------------------


class TestBuildMatrixFromDbOptIn(unittest.TestCase):
    """Tests for the inventory opt-in filter (``warm_cache`` flag)."""

    def test_build_matrix_skips_downstreams_without_warm_cache_opt_in(self) -> None:
        """
        ``warm_cache=False`` is the default and means "do not pay the
        every-6-hours warming cost for this downstream".  An opted-out
        downstream with a populated LKG/FKB pair must contribute zero
        entries — otherwise the opt-in flag is a lie.
        """
        # Arrange
        inventory = {"physlib": _config("physlib", warm_cache=False)}
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit=SHA_A,
                first_known_bad_commit=SHA_B,
            ),
        }

        # Act
        include, skipped = build_matrix_from_db(inventory, statuses)

        # Assert
        self.assertEqual(
            (include, skipped),
            ([], []),
            msg="Opted-out downstream contributed entries — the warm_cache flag is broken",
        )


class TestBuildMatrixFromDbRoleTagging(unittest.TestCase):
    """Tests for the LKG / FKB / both role tagging on matrix entries."""

    def test_build_matrix_with_only_lkg_set_tags_entry_lkg(self) -> None:
        """
        A SHA that only appears as someone's LKG is tagged ``lkg``.  The
        warming workflow uses the tag in its summary so an operator can
        eyeball whether a particular cold SHA was an LKG (advancing
        compatible boundary) or an FKB (regression we need to fix).
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A)}

        # Act
        include, skipped = build_matrix_from_db(inventory, statuses)

        # Assert
        self.assertEqual(
            include,
            [{"sha": SHA_A, "short_sha": SHA_A[:7], "tag": "lkg", "downstreams": ["physlib"]}],
            msg="LKG-only SHA must be tagged 'lkg'",
        )
        self.assertEqual(skipped, [])

    def test_build_matrix_with_only_fkb_set_tags_entry_fkb(self) -> None:
        """
        Symmetric to the LKG-only case — a SHA appearing only as
        someone's FKB is tagged ``fkb``.  Tag asymmetry between LKG-only
        and FKB-only is what lets the report distinguish "advance
        compatible boundary" from "warm the regression boundary so the
        bisect is fast next time".
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord(first_known_bad_commit=SHA_A)}

        # Act
        include, skipped = build_matrix_from_db(inventory, statuses)

        # Assert
        self.assertEqual(
            include,
            [{"sha": SHA_A, "short_sha": SHA_A[:7], "tag": "fkb", "downstreams": ["physlib"]}],
            msg="FKB-only SHA must be tagged 'fkb'",
        )
        self.assertEqual(skipped, [])

    def test_build_matrix_with_sha_as_both_lkg_and_fkb_tags_entry_both(self) -> None:
        """
        The same upstream SHA can be one downstream's LKG (compatible)
        and another downstream's FKB (regression boundary) — bisects
        from different downstreams converge on different boundaries.
        Tagging this case ``both`` is documented in
        ``docs/internal/cache-warming.md``; pinning the tag value here
        keeps the workflow summary readable.
        """
        # Arrange
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A),
            "FLT": DownstreamStatusRecord(first_known_bad_commit=SHA_A),
        }

        # Act
        include, _ = build_matrix_from_db(inventory, statuses)

        # Assert
        self.assertEqual(len(include), 1, msg="A single SHA produces a single entry")
        self.assertEqual(include[0]["sha"], SHA_A)
        self.assertEqual(
            include[0]["tag"], "both", msg="Cross-role SHA must be tagged 'both'"
        )
        self.assertEqual(
            sorted(include[0]["downstreams"]),
            ["FLT", "physlib"],
            msg="Both downstreams must appear in the entry's downstreams list",
        )


class TestBuildMatrixFromDbDedupAndOrdering(unittest.TestCase):
    """Tests for cross-downstream dedup and deterministic SHA ordering."""

    def test_build_matrix_dedups_when_two_downstreams_share_an_lkg(self) -> None:
        """
        Two downstreams sitting on the same LKG must collapse to a single
        matrix entry — without dedup the warming workflow would build the
        same SHA twice in parallel, wasting a self-hosted ``pr`` runner
        slot.
        """
        # Arrange
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A),
            "FLT": DownstreamStatusRecord(last_known_good_commit=SHA_A),
        }

        # Act
        include, _ = build_matrix_from_db(inventory, statuses)

        # Assert
        self.assertEqual(len(include), 1, msg="Shared LKG must dedup to one entry")
        self.assertEqual(include[0]["tag"], "lkg")
        self.assertEqual(sorted(include[0]["downstreams"]), ["FLT", "physlib"])

    def test_build_matrix_with_distinct_lkg_and_fkb_emits_two_entries(self) -> None:
        """
        A single downstream's LKG and FKB are distinct upstream commits
        (unless the regression has zero distance, which the bisect
        wouldn't produce).  Both are warmed because consumers of
        ``lkg/latest.json`` expect both endpoints to have populated
        Azure caches.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit=SHA_A,
                first_known_bad_commit=SHA_B,
            ),
        }

        # Act
        include, _ = build_matrix_from_db(inventory, statuses)

        # Assert
        self.assertEqual(
            sorted((entry["sha"], entry["tag"]) for entry in include),
            [(SHA_A, "lkg"), (SHA_B, "fkb")],
            msg="Distinct LKG and FKB must produce two correctly-tagged entries",
        )

    def test_build_matrix_emits_entries_sorted_by_sha(self) -> None:
        """
        Sort order matters because the matrix is converted to a GitHub
        Actions matrix and rendered in workflow logs / job names.
        Deterministic order makes log diffs across runs comparable
        without spurious noise from dict-iteration ordering.
        """
        # Arrange
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=SHA_C),
            "FLT": DownstreamStatusRecord(last_known_good_commit=SHA_A),
        }

        # Act
        include, _ = build_matrix_from_db(inventory, statuses)
        shas = [entry["sha"] for entry in include]

        # Assert
        self.assertEqual(
            shas,
            sorted(shas),
            msg="Matrix must be SHA-sorted for deterministic logs across runs",
        )


class TestBuildMatrixFromDbEmptyState(unittest.TestCase):
    """Tests for inputs that contribute no SHAs."""

    def test_build_matrix_with_null_lkg_and_fkb_skips_downstream(self) -> None:
        """
        A downstream that has never run (or that recovered cleanly with
        no FKB) has both fields ``None``.  We have nothing to warm —
        emitting an entry would crash the per-SHA workflow at clone
        time.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord()}

        # Act
        result = build_matrix_from_db(inventory, statuses)

        # Assert
        self.assertEqual(
            result, ([], []), msg="Status with both endpoints None contributes nothing"
        )

    def test_build_matrix_with_inventory_entry_missing_from_statuses_skips_silently(
        self,
    ) -> None:
        """
        An opted-in downstream with no DB row yet (first run) has no
        endpoints to warm.  Silent skip rather than crash because new
        downstreams are added all the time and the warming workflow
        runs every 6h regardless of whether any have produced runs.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}

        # Act
        result = build_matrix_from_db(inventory, statuses={})

        # Assert
        self.assertEqual(
            result,
            ([], []),
            msg="Missing status row must skip silently, not crash the planner",
        )


class TestBuildMatrixFromDbKnownWarmFilter(unittest.TestCase):
    """Tests for the ``cache_warmth`` table filter (``known_warm_shas``)."""

    def test_build_matrix_drops_known_warm_shas_into_skipped_list(self) -> None:
        """
        The ``cache_warmth`` table is the steady-state contract that
        prevents re-warming SHAs we know are populated.  A known-warm
        SHA must move from ``include`` to ``skipped`` — not vanish —
        so the finalize summary can still mention it as
        ``cache_warmth_hit`` rather than implying nothing was planned.
        """
        # Arrange
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A),
            "FLT": DownstreamStatusRecord(last_known_good_commit=SHA_B),
        }

        # Act
        include, skipped = build_matrix_from_db(
            inventory, statuses, known_warm_shas={SHA_A}
        )

        # Assert
        self.assertEqual(
            [entry["sha"] for entry in include],
            [SHA_B],
            msg="Cold SHA stays in include; warm SHA leaves include",
        )
        self.assertEqual(
            [entry["sha"] for entry in skipped],
            [SHA_A],
            msg="Warm SHA must appear in skipped so the summary can show it",
        )

    def test_build_matrix_with_all_shas_known_warm_yields_empty_include(self) -> None:
        """
        Steady-state expectation: every SHA in the planner's view is
        already warm, so ``include`` is empty and ``skipped`` lists all
        of them.  This is the "everything green" tick that should still
        run finalize (so the summary reflects the cache_warmth hits)
        without spinning up any per-SHA runners.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit=SHA_A,
                first_known_bad_commit=SHA_B,
            ),
        }

        # Act
        include, skipped = build_matrix_from_db(
            inventory, statuses, known_warm_shas={SHA_A, SHA_B}
        )

        # Assert
        self.assertEqual(include, [], msg="All SHAs warm: nothing to build")
        self.assertEqual(
            sorted(entry["sha"] for entry in skipped),
            [SHA_A, SHA_B],
            msg="All SHAs warm: skipped lists every known-warm SHA",
        )

    def test_build_matrix_warm_filter_is_per_sha_not_per_downstream(self) -> None:
        """
        Warming a downstream's LKG does *not* imply its FKB is warm —
        these are two different upstream SHAs with two different Azure
        cache rows.  The filter must operate per-SHA, dropping only
        the warm one and keeping the cold one in ``include``.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit=SHA_A,
                first_known_bad_commit=SHA_B,
            ),
        }

        # Act — only the LKG is warm; the FKB should still be planned.
        include, skipped = build_matrix_from_db(
            inventory, statuses, known_warm_shas={SHA_A}
        )

        # Assert
        self.assertEqual(
            [(entry["sha"], entry["tag"]) for entry in include],
            [(SHA_B, "fkb")],
            msg="Cold FKB must remain in include even when its sibling LKG is warm",
        )
        self.assertEqual(
            [(entry["sha"], entry["tag"]) for entry in skipped],
            [(SHA_A, "lkg")],
            msg="Warm LKG must appear in skipped with its original tag preserved",
        )

    def test_build_matrix_skipped_entry_preserves_tag_and_downstreams(self) -> None:
        """
        Skipped entries are not just SHAs — they carry the same
        ``tag`` and ``downstreams`` metadata as include entries so the
        finalize summary can render skipped rows with the same context
        ("LKG for physlib, FKB for FLT") that include rows have.
        """
        # Arrange
        inventory = {
            "physlib": _config("physlib"),
            "FLT": _config("FLT"),
        }
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A),
            "FLT": DownstreamStatusRecord(first_known_bad_commit=SHA_A),
        }

        # Act
        _, skipped = build_matrix_from_db(
            inventory, statuses, known_warm_shas={SHA_A}
        )

        # Assert
        self.assertEqual(len(skipped), 1, msg="A single SHA produces a single skipped entry")
        self.assertEqual(skipped[0]["sha"], SHA_A)
        self.assertEqual(
            skipped[0]["tag"],
            "both",
            msg="Skipped entry must carry the cross-role tag, not be reduced to 'lkg' or 'fkb'",
        )
        self.assertEqual(sorted(skipped[0]["downstreams"]), ["FLT", "physlib"])


if __name__ == "__main__":
    unittest.main()
