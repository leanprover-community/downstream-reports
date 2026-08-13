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
      ``downstream_status`` for every enabled downstream, dedups LKG/FKB
      across them, consults ``cache_warmth`` records to skip verified-warm
      SHAs and pace failed ones through the backoff retry schedule, and
      tags each entry by the role(s) it plays.

Out of scope:
    - ``main()`` and ``build_parser()`` — argparse + I/O glue.  The
      end-to-end behaviour is exercised by the workflow itself; the unit
      suite focuses on the matrix-building logic that the workflow can't
      easily assert against.
    - ``SqlBackend.load_cache_warmth`` — covered indirectly here via
      the ``warmth`` parameter and lives in ``test_storage.py`` for the
      SQL side.

Why this matters
----------------
The matrix is the contract with ``warm-mathlib-cache.yml``: a SHA listed
in ``include`` will be cloned, built, and pushed to the shared Azure
cache.  A SHA listed in ``skipped`` will be reported under its skip
status (``cache_warmth_hit`` / ``retry_backoff`` / ``retry_exhausted``)
in the finalize summary.  Misclassifying a cold SHA as warm causes
``publish-lkg`` to advertise a ``recommended_bump_commit`` whose Azure
cache is empty — exactly the cold-SHA contract violation the warming
pipeline is designed to prevent.  See ``docs/internal/cache-warming.md``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scripts.conftest import SHA_A, SHA_B, SHA_C
from scripts.models import DownstreamConfig
from scripts.plan_cache_warm_jobs import (
    MAX_WARM_ATTEMPTS,
    _parse_manual_shas,
    build_matrix_from_db,
    build_matrix_manual,
    retry_delay,
)
from scripts.storage import CacheWarmthRecord, DownstreamStatusRecord

# Fixed "current time" for the retry-schedule tests: the planner compares
# `now` against `last_attempt_at + retry_delay(attempts)`, so tests pin
# both sides instead of racing the wall clock.
_NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _config(name: str) -> DownstreamConfig:
    """Construct a minimal ``DownstreamConfig`` for matrix-building tests.

    The fields (``repo``, ``default_branch``, ``dependency_name``) take
    stable mathlib-shaped defaults so the test focus stays on matrix
    logic, not config plumbing.  A factory rather than module-level
    fixtures because most tests want two configs with different names.
    """
    return DownstreamConfig(
        name=name,
        repo=f"org/{name}",
        default_branch="main",
        dependency_name="mathlib",
    )


def _warmth(
    status: str = "warmed", attempts: int = 1, age_hours: float = 0.0
) -> CacheWarmthRecord:
    """Construct a ``CacheWarmthRecord`` whose attempt is *age_hours* before ``_NOW``."""
    stamp = (_NOW - timedelta(hours=age_hours)).isoformat().replace("+00:00", "Z")
    return CacheWarmthRecord(status=status, attempts=attempts, last_attempt_at=stamp)


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


class TestBuildMatrixManual:
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
        assert matrix == [
                {"sha": SHA_A, "short_sha": SHA_A[:7], "tag": "manual", "downstreams": []},
                {"sha": SHA_B, "short_sha": SHA_B[:7], "tag": "manual", "downstreams": []},
            ], (
                "Manual matrix entries are the contract with _warm-one-sha.yml; "
                "the four-field shape and `manual` tag must be stable"
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
        assert matrix == [], "Empty input must yield an empty matrix"


# ----------------------------------------------------------------------
# build_matrix_from_db — the planner.  Most tests use unittest because
# they don't benefit from parametrize; the warm-filter cases at the
# bottom are tabular and use parametrize.
# ----------------------------------------------------------------------


class TestBuildMatrixFromDbRoleTagging:
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
        assert include == [{"sha": SHA_A, "short_sha": SHA_A[:7], "tag": "lkg", "downstreams": ["physlib"]}], "LKG-only SHA must be tagged 'lkg'"
        assert skipped == []

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
        assert include == [{"sha": SHA_A, "short_sha": SHA_A[:7], "tag": "fkb", "downstreams": ["physlib"]}], "FKB-only SHA must be tagged 'fkb'"
        assert skipped == []

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
        assert len(include) == 1, "A single SHA produces a single entry"
        assert include[0]["sha"] == SHA_A
        assert include[0]["tag"] == "both", "Cross-role SHA must be tagged 'both'"
        assert sorted(include[0]["downstreams"]) == ["FLT", "physlib"], "Both downstreams must appear in the entry's downstreams list"


class TestBuildMatrixFromDbDedupAndOrdering:
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
        assert len(include) == 1, "Shared LKG must dedup to one entry"
        assert include[0]["tag"] == "lkg"
        assert sorted(include[0]["downstreams"]) == ["FLT", "physlib"]

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
        assert sorted((entry["sha"], entry["tag"]) for entry in include) == [(SHA_A, "lkg"), (SHA_B, "fkb")], "Distinct LKG and FKB must produce two correctly-tagged entries"

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
        assert shas == sorted(shas), "Matrix must be SHA-sorted for deterministic logs across runs"


class TestBuildMatrixFromDbEmptyState:
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
        assert result == ([], []), "Status with both endpoints None contributes nothing"

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
        assert result == ([], []), "Missing status row must skip silently, not crash the planner"


class TestBuildMatrixFromDbKnownWarmFilter:
    """Tests for the verified-warm filter (``warmth`` records)."""

    def test_build_matrix_drops_verified_warm_shas_into_skipped_list(self) -> None:
        """
        The ``cache_warmth`` table is the steady-state contract that
        prevents re-warming SHAs we know are populated.  A verified-warm
        SHA must move from ``include`` to ``skipped`` — not vanish —
        carrying the ``cache_warmth_hit`` status so the finalize summary
        can still mention it rather than implying nothing was planned.
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
            inventory, statuses, warmth={SHA_A: _warmth("warmed")}, now=_NOW
        )

        # Assert
        assert [entry["sha"] for entry in include] == [SHA_B], "Cold SHA stays in include; warm SHA leaves include"
        assert [(entry["sha"], entry["status"]) for entry in skipped] == [(SHA_A, "cache_warmth_hit")], "Warm SHA must appear in skipped as cache_warmth_hit so the summary can show it"

    def test_build_matrix_with_all_shas_verified_warm_yields_empty_include(self) -> None:
        """
        Steady-state expectation: every SHA in the planner's view is
        already warm (either terminal warm status), so ``include`` is
        empty and ``skipped`` lists all of them.  This is the
        "everything green" tick that should still run finalize (so the
        summary reflects the cache_warmth hits) without spinning up any
        per-SHA runners.
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
            inventory,
            statuses,
            warmth={SHA_A: _warmth("warmed"), SHA_B: _warmth("already_warm")},
            now=_NOW,
        )

        # Assert
        assert include == [], "All SHAs warm: nothing to build"
        assert sorted(entry["sha"] for entry in skipped) == [SHA_A, SHA_B], "All SHAs warm: skipped lists every verified-warm SHA"
        assert {entry["status"] for entry in skipped} == {"cache_warmth_hit"}, "Both warm statuses classify as cache_warmth_hit"

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
            inventory, statuses, warmth={SHA_A: _warmth("warmed")}, now=_NOW
        )

        # Assert
        assert [(entry["sha"], entry["tag"]) for entry in include] == [(SHA_B, "fkb")], "Cold FKB must remain in include even when its sibling LKG is warm"
        assert [(entry["sha"], entry["tag"]) for entry in skipped] == [(SHA_A, "lkg")], "Warm LKG must appear in skipped with its original tag preserved"

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
            inventory, statuses, warmth={SHA_A: _warmth("warmed")}, now=_NOW
        )

        # Assert
        assert len(skipped) == 1, "A single SHA produces a single skipped entry"
        assert skipped[0]["sha"] == SHA_A
        assert skipped[0]["tag"] == "both", "Skipped entry must carry the cross-role tag, not be reduced to 'lkg' or 'fkb'"
        assert sorted(skipped[0]["downstreams"]) == ["FLT", "physlib"]


class TestBuildMatrixFromDbRetrySchedule:
    """Tests for the failed-SHA backoff retry schedule.

    Mathlib master always builds, so every failed warming attempt is
    infra trouble: the planner re-attempts failed SHAs once their
    backoff has elapsed, and stops (``retry_exhausted``) only when the
    attempt budget runs out.  Permanently trusting a failure is exactly
    the "failed warming attempts are recorded as warmth" bug this
    schedule replaces.
    """

    @pytest.mark.parametrize(
        "status", ["build_failed", "push_failed", "verify_failed"]
    )
    def test_failed_sha_with_elapsed_backoff_is_replanned(self, status: str) -> None:
        """
        Every failure status re-enters the matrix once its backoff has
        elapsed — none of them is terminal before the attempt budget
        runs out.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A)}
        delay_h = retry_delay(1).total_seconds() / 3600

        # Act — last attempt just past its backoff window.
        include, skipped = build_matrix_from_db(
            inventory,
            statuses,
            warmth={SHA_A: _warmth(status, attempts=1, age_hours=delay_h + 0.1)},
            now=_NOW,
        )

        # Assert
        assert [entry["sha"] for entry in include] == [SHA_A], f"{status} SHA past its backoff must be re-planned"
        assert skipped == []

    def test_failed_sha_inside_backoff_window_is_skipped_as_retry_backoff(self) -> None:
        """
        A failed SHA whose backoff has not yet elapsed is skipped this
        tick with status ``retry_backoff`` — visible in the summary,
        untouched in the matrix — so a flapping infra failure can't
        spin the self-hosted runner on every 6h tick.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A)}

        # Act — attempt 2 (12h backoff), only 1h old.
        include, skipped = build_matrix_from_db(
            inventory,
            statuses,
            warmth={SHA_A: _warmth("push_failed", attempts=2, age_hours=1.0)},
            now=_NOW,
        )

        # Assert
        assert include == [], "SHA inside its backoff window must not re-enter the matrix"
        assert [(entry["sha"], entry["status"]) for entry in skipped] == [(SHA_A, "retry_backoff")]
        assert "push_failed" in skipped[0]["detail"], "The detail field carries the underlying failure status for the summary"

    def test_backoff_doubles_per_recorded_attempt(self) -> None:
        """
        The schedule is exponential: attempt n waits ``6h * 2^(n-1)``.
        An age that clears attempt 1's window must still be inside
        attempt 3's, so the same age classifies differently as the
        attempt counter grows.
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A)}
        age_hours = 7.0  # > 6h (attempt 1), < 24h (attempt 3)

        # Act
        include_a1, _ = build_matrix_from_db(
            inventory, statuses,
            warmth={SHA_A: _warmth("verify_failed", attempts=1, age_hours=age_hours)},
            now=_NOW,
        )
        include_a3, skipped_a3 = build_matrix_from_db(
            inventory, statuses,
            warmth={SHA_A: _warmth("verify_failed", attempts=3, age_hours=age_hours)},
            now=_NOW,
        )

        # Assert
        assert [entry["sha"] for entry in include_a1] == [SHA_A], "7h clears the 6h backoff of attempt 1"
        assert include_a3 == [], "7h is inside the 24h backoff of attempt 3"
        assert skipped_a3[0]["status"] == "retry_backoff"

    def test_sha_out_of_attempt_budget_is_skipped_as_retry_exhausted(self) -> None:
        """
        After ``MAX_WARM_ATTEMPTS`` recorded failures the planner stops
        rescheduling the SHA regardless of age, and reports it as
        ``retry_exhausted`` so the finalize summary can warn the
        operator (a ``--manual-shas`` backfill bypasses the filter).
        """
        # Arrange
        inventory = {"physlib": _config("physlib")}
        statuses = {"physlib": DownstreamStatusRecord(last_known_good_commit=SHA_A)}

        # Act — ancient failure, but the budget is spent.
        include, skipped = build_matrix_from_db(
            inventory,
            statuses,
            warmth={
                SHA_A: _warmth(
                    "build_failed", attempts=MAX_WARM_ATTEMPTS, age_hours=24 * 365
                )
            },
            now=_NOW,
        )

        # Assert
        assert include == [], "An exhausted SHA must never re-enter the matrix"
        assert [(entry["sha"], entry["status"]) for entry in skipped] == [(SHA_A, "retry_exhausted")]
        assert f"{MAX_WARM_ATTEMPTS}/{MAX_WARM_ATTEMPTS}" in skipped[0]["detail"], "The detail field shows the spent budget"
