#!/usr/bin/env python3
"""
Tests for: scripts.storage

Coverage scope:
    - ``result_to_row`` — RunResultRecord → row dict serialiser used by
      every backend's ``save_run`` implementation.
    - ``create_backend`` — factory that selects FilesystemBackend /
      SqlBackend / DryRunBackend based on the ``--backend`` flag.
    - ``FilesystemBackend.{save_run, load_all_statuses,
      load_tested_downstream_commits, load_prior_results}`` — the local
      / dry-run path that local development and CI debugging use.

Out of scope:
    - ``SqlBackend`` (PostgreSQL): exercised in CI by the regression
      workflow itself; integration coverage of the SQL load path is in
      ``test_export_runs_snapshot.LoadLatestRunPerDownstreamTests`` (in-
      memory SQLite).  ``load_known_warm_shas``, ``record_warm_shas``,
      ``load_manifest_watcher_ledger``, and
      ``upsert_manifest_watcher_ledger`` rely on the SqlBackend
      implementation and are not exercised by the unit suite.
    - ``DryRunBackend``: by design it has no state to assert against; it
      only prints.

Why this matters
----------------
``DownstreamStatusRecord`` is the persisted contract that the report
job, the snapshot exporters, the manifest watcher, and the cache-warming
planner all read from.  A round-trip bug — a field silently dropped on
write or coerced to ``None`` on read — would propagate through every
consumer and silently corrupt the public ``lkg/latest.json``.  The
round-trip tests below pin every persisted field as part of that
contract.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.storage import (
    DownstreamStatusRecord,
    FilesystemBackend,
    RunResultRecord,
    connect_with_retry,
    create_backend,
    create_sql_engine,
    result_to_row,
)
import pytest
from sqlalchemy.exc import OperationalError


# ----------------------------------------------------------------------
# Test data builders.  Tests build records with all fields populated by
# default; individual tests use keyword overrides for the one or two
# fields that are relevant to their scenario.
# ----------------------------------------------------------------------

# Stable upstream slug used everywhere; matches the production inventory.
_UPSTREAM = "leanprover-community/mathlib4"


def _make_run_result(
    downstream: str,
    downstream_commit: str,
    outcome: str,
) -> RunResultRecord:
    """Build a minimal-but-valid ``RunResultRecord``.

    What state it provides
    ----------------------
    All required fields populated with stable conventional values; only
    the three arguments — name, downstream commit, and outcome —
    distinguish records across tests.  The ``last_known_good`` field
    mirrors the outcome (``"target_abc"`` on pass, ``None`` on fail/
    error) so the resulting record is internally consistent and can
    actually be persisted by the filesystem backend without violating
    its own invariants.

    Why a factory rather than module-level fixtures
    -----------------------------------------------
    Tests need slight variations (different downstreams, different
    outcomes) — a single pre-built record would force every test to
    rebuild it.  Composing tiny records via this helper keeps the
    intent of each test visible: ``_make_run_result("A", "x", "passed")``
    reads as the scenario it sets up.
    """
    return RunResultRecord(
        upstream=_UPSTREAM,
        downstream=downstream,
        repo="owner/repo",
        downstream_commit=downstream_commit,
        outcome=outcome,
        episode_state="passing" if outcome == "passed" else "error",
        target_commit="target_abc",
        previous_last_known_good=None,
        previous_first_known_bad=None,
        last_known_good="target_abc" if outcome == "passed" else None,
        first_known_bad=None,
        current_last_successful=None,
        current_first_failing=None,
        failure_stage=None,
        search_mode="head-only",
        commit_window_truncated=False,
        error=None,
        head_probe_outcome=outcome,
        head_probe_failure_stage=None,
        culprit_log_text=None,
    )


# ----------------------------------------------------------------------
# result_to_row — pure serialisation; the contract is "every dataclass
# field round-trips into the row dict, no field is silently dropped".
# ----------------------------------------------------------------------


class TestResultToRow:
    """Tests for ``result_to_row``."""

    def test_result_to_row_preserves_every_dataclass_field(self) -> None:
        """
        ``result_to_row`` is the choke-point that every backend's
        ``save_run`` calls.  Because the dataclass has 20+ fields, a
        manual key-by-key assertion would drift; instead this test
        introspects ``RunResultRecord`` via ``dataclasses.fields()`` and
        asserts each field name appears in the row.

        Why this matters: a field silently dropped here disappears from
        every backend's row writes, which means the SQL upsert wouldn't
        write it and the filesystem JSON wouldn't include it — both
        consumers go silently stale.
        """
        # Arrange
        record = RunResultRecord(
            upstream=_UPSTREAM,
            downstream="TestDownstream",
            repo="owner/repo",
            downstream_commit="ds_head",
            outcome="passed",
            episode_state="passing",
            target_commit="target_abc",
            previous_last_known_good="prev_good",
            previous_first_known_bad=None,
            last_known_good="target_abc",
            first_known_bad=None,
            current_last_successful="target_abc",
            current_first_failing=None,
            failure_stage=None,
            search_mode="head-only",
            commit_window_truncated=False,
            error=None,
            head_probe_outcome="passed",
            head_probe_failure_stage=None,
            culprit_log_text=None,
            pinned_commit="pin_abc",
        )

        # Act
        row = result_to_row(record)

        # Assert (spot-check a handful of representative fields)
        assert row["downstream"] == "TestDownstream"
        assert row["outcome"] == "passed"
        assert row["pinned_commit"] == "pin_abc"
        assert row["error"] is None, "None must serialise as None, not 'None' string"
        assert not row["commit_window_truncated"], "False must serialise as False, not 0 or 'false'"
        # Assert (full coverage via field introspection — guards against
        # any future field being silently dropped from the serialiser).
        for field in dataclasses.fields(record):
            assert field.name in row, f"result_to_row must include every dataclass field; missing {field.name!r}"


# ----------------------------------------------------------------------
# create_backend — factory selection and required-arg validation.
# ----------------------------------------------------------------------


class TestCreateBackendFactory:
    """Tests for ``create_backend`` selection logic."""

    def test_create_backend_filesystem_with_state_root_returns_filesystem_backend(
        self,
    ) -> None:
        """
        ``--backend filesystem --state-root <path>`` is the local-dev
        and CI-debug code path.  This is the happy path for the
        FilesystemBackend; if the factory dispatch ever silently fell
        back to a different backend (e.g. DryRun) tests against a
        FilesystemBackend would behave unpredictably.
        """
        # Arrange / Act
        with tempfile.TemporaryDirectory() as tmp:
            backend = create_backend("filesystem", state_root=Path(tmp))

            # Assert
            assert isinstance(backend, FilesystemBackend), "`--backend filesystem` must return a FilesystemBackend instance"

    def test_create_backend_filesystem_without_state_root_raises_system_exit(self) -> None:
        """
        Without ``--state-root`` the filesystem backend has nowhere to
        write.  The factory raises ``SystemExit`` (rather than
        ``ValueError``) so argparse-style CLI scripts get a clean exit
        message instead of a stack trace when an operator forgets the
        flag.
        """
        # Arrange / Act / Assert
        with pytest.raises(SystemExit):
            create_backend("filesystem")

    def test_create_backend_sql_without_postgres_dsn_raises_system_exit(self) -> None:
        """
        ``--backend sql`` without ``POSTGRES_DSN`` set is unrecoverable —
        we cannot connect.  As with the filesystem case, ``SystemExit``
        gives the operator a clean error rather than an SQLAlchemy
        traceback they have to read past.
        """
        # Arrange — strip POSTGRES_DSN if it happens to be set in the
        # developer's shell so the test reflects production CI's blank-
        # env conditions.
        old_dsn = os.environ.pop("POSTGRES_DSN", None)
        try:
            # Act / Assert
            with pytest.raises(SystemExit):
                create_backend("sql")
        finally:
            # Restore so subsequent tests see the original environment.
            if old_dsn is not None:
                os.environ["POSTGRES_DSN"] = old_dsn


# ----------------------------------------------------------------------
# Resilient connection handling — guards against transient Neon endpoint
# blips wiping out a whole select fan-out.  ``connect_with_retry`` is the
# primitive every SqlBackend read/write routes through; ``create_sql_engine``
# is the hardened engine factory.
# ----------------------------------------------------------------------


class _FlakyEngine:
    """Fake engine whose ``connect()`` fails a fixed number of times.

    Records how many times ``connect()`` was called so a test can assert the
    retry loop stopped as soon as a live connection was obtained.
    """

    def __init__(self, *, fail_times: int, exc: Exception, ok: object = "LIVE_CONN") -> None:
        self._fail_times = fail_times
        self._exc = exc
        self._ok = ok
        self.calls = 0

    def connect(self) -> object:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        return self._ok


def _operational_error() -> OperationalError:
    """Build an OperationalError shaped like a psycopg2 connection timeout."""
    return OperationalError("SELECT 1", {}, Exception("connection timed out"))


class TestConnectWithRetry:
    """Tests for ``connect_with_retry`` — the transient-blip retry primitive."""

    def test_returns_immediately_when_first_connect_succeeds(self) -> None:
        """
        The happy path must not sleep or retry: a healthy endpoint pays no
        latency tax.  A live connection on the first attempt returns straight
        through with zero backoff sleeps.
        """
        # Arrange
        engine = _FlakyEngine(fail_times=0, exc=_operational_error())
        sleeps: list[float] = []

        # Act
        conn = connect_with_retry(engine, sleep=sleeps.append, rng=lambda: 1.0)

        # Assert
        assert conn == "LIVE_CONN"
        assert engine.calls == 1
        assert sleeps == [], "the happy path must not back off"

    def test_retries_operational_error_then_succeeds(self) -> None:
        """
        A Neon connection timeout that clears on a later attempt is exactly the
        failure this primitive exists for: two blips, then success, must yield
        a live connection after backing off between attempts (one sleep per
        retry, never after the final success).
        """
        # Arrange
        engine = _FlakyEngine(fail_times=2, exc=_operational_error())
        sleeps: list[float] = []

        # Act — rng pinned to 1.0 so full-jitter delay equals the cap and the
        # exponential schedule is observable.
        conn = connect_with_retry(
            engine,
            attempts=4,
            base_delay=1.0,
            max_delay=8.0,
            sleep=sleeps.append,
            rng=lambda: 1.0,
        )

        # Assert
        assert conn == "LIVE_CONN"
        assert engine.calls == 3, "two failures plus the successful third attempt"
        assert sleeps == [1.0, 2.0], "exponential backoff: 1*2**0, then 1*2**1"

    def test_reraises_after_exhausting_attempts(self) -> None:
        """
        A sticky outage (every attempt times out) must surface the real
        OperationalError after the last attempt — never a generic or swallowed
        error — so the failing job log names the true cause.
        """
        # Arrange
        exc = _operational_error()
        engine = _FlakyEngine(fail_times=99, exc=exc)
        sleeps: list[float] = []

        # Act / Assert
        with pytest.raises(OperationalError) as caught:
            connect_with_retry(engine, attempts=3, sleep=sleeps.append, rng=lambda: 1.0)
        assert caught.value is exc
        assert engine.calls == 3, "exactly `attempts` connection attempts, no more"
        assert len(sleeps) == 2, "backs off between attempts but not after the last"

    def test_does_not_retry_non_operational_errors(self) -> None:
        """
        Only connection-level blips are transient.  A programming error (here a
        ValueError) is a genuine fault — retrying would just delay the
        traceback, so it must propagate on the first attempt with no backoff.
        """
        # Arrange
        engine = _FlakyEngine(fail_times=99, exc=ValueError("bad SQL"))
        sleeps: list[float] = []

        # Act / Assert
        with pytest.raises(ValueError):
            connect_with_retry(engine, attempts=4, sleep=sleeps.append, rng=lambda: 1.0)
        assert engine.calls == 1, "non-transient errors must not be retried"
        assert sleeps == []


class TestCreateSqlEngine:
    """Tests for ``create_sql_engine`` — the hardened engine factory."""

    def test_postgres_dsn_sets_fast_connect_timeout_and_pre_ping(self, monkeypatch) -> None:
        """
        Production runs against Postgres (Neon).  Without a connect timeout a
        dead endpoint hangs on the OS TCP timeout (minutes); with pre-ping a
        stale pooled connection silently reconnects.  Both must be wired in for
        the Postgres dialect.
        """
        # Arrange — capture the kwargs handed to SQLAlchemy without opening a
        # real connection.
        captured: dict[str, object] = {}

        def fake_create_engine(dsn: str, **kwargs: object) -> str:
            captured["dsn"] = dsn
            captured.update(kwargs)
            return "ENGINE"

        import sqlalchemy

        monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)

        # Act
        engine = create_sql_engine("postgresql://u:p@host/db")

        # Assert
        assert engine == "ENGINE"
        assert captured["pool_pre_ping"] is True
        assert captured["connect_args"] == {"connect_timeout": 10}

    def test_non_postgres_dsn_omits_connect_timeout(self, monkeypatch) -> None:
        """
        ``connect_timeout`` is a psycopg2 keyword; passing it to a non-Postgres
        driver (e.g. the SQLite engine the test suite uses) would error.  For
        those DSNs the factory still enables pre-ping but leaves connect_args
        empty.
        """
        # Arrange
        captured: dict[str, object] = {}

        def fake_create_engine(dsn: str, **kwargs: object) -> str:
            captured.update(kwargs)
            return "ENGINE"

        import sqlalchemy

        monkeypatch.setattr(sqlalchemy, "create_engine", fake_create_engine)

        # Act
        create_sql_engine("sqlite:///tmp.db")

        # Assert
        assert captured["pool_pre_ping"] is True
        assert captured["connect_args"] == {}


# ----------------------------------------------------------------------
# FilesystemBackend — the persisted JSON layout is the local-dev
# contract.  Tests cover the round-trip of every field that
# DownstreamStatusRecord persists.
# ----------------------------------------------------------------------


class TestFilesystemBackendStatusRoundTrip:
    """Tests for FilesystemBackend's ``save_run`` / ``load_all_statuses`` round trip."""

    def test_load_all_statuses_with_no_status_file_returns_empty_dict(self) -> None:
        """
        First-ever run on a fresh state-root: there is no
        ``status/current.json`` yet.  Returning ``{}`` (rather than
        raising) lets the report job treat first-run as the same code
        path as steady-state — every downstream is "no prior state".
        """
        # Arrange / Act / Assert
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            assert backend.load_all_statuses("regression", _UPSTREAM) == {}, "Missing status file must read as an empty mapping"

    def test_save_run_then_load_round_trips_lkg_and_pinned_commit(self) -> None:
        """
        The two oldest fields on ``DownstreamStatusRecord`` —
        ``last_known_good_commit`` and ``pinned_commit`` — are the core
        of the regression contract.  Round-tripping both pins the
        end-to-end JSON path and guards against schema evolution
        accidentally dropping either field.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            record = RunResultRecord(
                upstream=_UPSTREAM,
                downstream="TestDownstream",
                repo="owner/repo",
                downstream_commit="ds_head",
                outcome="passed",
                episode_state="passing",
                target_commit="target_abc",
                previous_last_known_good=None,
                previous_first_known_bad=None,
                last_known_good="target_abc",
                first_known_bad=None,
                current_last_successful="target_abc",
                current_first_failing=None,
                failure_stage=None,
                search_mode="head-only",
                commit_window_truncated=False,
                error=None,
                head_probe_outcome="passed",
                head_probe_failure_stage=None,
                culprit_log_text=None,
                pinned_commit="pin_abc",
            )
            statuses = {
                "TestDownstream": DownstreamStatusRecord(
                    last_known_good_commit="target_abc",
                    pinned_commit="pin_abc",
                ),
            }

            # Act
            backend.save_run(
                run_id="run_123",
                workflow="regression",
                upstream=_UPSTREAM,
                upstream_ref="master",
                run_url="https://example.com/run/123",
                created_at="2026-04-01T00:00:00Z",
                results=[record],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", _UPSTREAM)

            # Assert
            assert "TestDownstream" in loaded
            assert loaded["TestDownstream"].last_known_good_commit == "target_abc"
            assert loaded["TestDownstream"].pinned_commit == "pin_abc"

    def test_save_run_round_trips_downstream_commit(self) -> None:
        """
        ``downstream_commit`` was added to ``DownstreamStatusRecord``
        specifically to gate the skip heuristics
        (``try_skip_already_good`` / ``try_skip_known_bad_bisect``).  A
        round-trip drop here would silently disable both heuristics —
        every run would re-bisect even when the downstream hadn't moved.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            statuses = {
                "TestDownstream": DownstreamStatusRecord(
                    last_known_good_commit="target_abc",
                    downstream_commit="ds_commit_abc",
                ),
            }

            # Act
            backend.save_run(
                run_id="run_456",
                workflow="regression",
                upstream=_UPSTREAM,
                upstream_ref="master",
                run_url="https://example.com/run/456",
                created_at="2026-04-02T00:00:00Z",
                results=[],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", _UPSTREAM)

            # Assert
            assert loaded["TestDownstream"].downstream_commit == "ds_commit_abc", "downstream_commit round-trip is required for skip heuristics to work"

    def test_save_run_round_trips_last_good_release_fields(self) -> None:
        """
        ``last_good_release`` and ``last_good_release_commit`` are
        derived post-aggregation by ``latest_reachable_tag`` /
        ``resolve_tag``.  They are surfaced by the public site and the
        snapshot — a round-trip drop would leave the public dashboard
        showing "—" instead of the actual release name.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            statuses = {
                "TestDownstream": DownstreamStatusRecord(
                    last_known_good_commit="lkg_abc",
                    last_good_release="v4.13.0",
                    last_good_release_commit="sha_v4_13_0",
                ),
            }

            # Act
            backend.save_run(
                run_id="run_release",
                workflow="regression",
                upstream=_UPSTREAM,
                upstream_ref="master",
                run_url="https://example.com/run/release",
                created_at="2026-04-10T00:00:00Z",
                results=[],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", _UPSTREAM)

            # Assert
            assert loaded["TestDownstream"].last_good_release == "v4.13.0"
            assert loaded["TestDownstream"].last_good_release_commit == "sha_v4_13_0"


class TestFilesystemBackendBackwardsCompatibility:
    """Tests for reading status files that pre-date newer fields."""

    def test_load_all_statuses_with_pre_release_schema_loads_release_fields_as_none(
        self,
    ) -> None:
        """
        A status file written before ``last_good_release`` was added has
        no ``last_good_release`` / ``last_good_release_commit`` keys.
        Reading must default both to ``None`` so a long-running CI host
        can be upgraded in place without re-deriving the whole status
        store.
        """
        # Arrange — write a hand-crafted status file in the older shape.
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp) / "status"
            status_dir.mkdir()
            (status_dir / "current.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "reported_at": "2026-04-01T00:00:00Z",
                        "downstreams": {
                            "OldDownstream": {
                                "last_known_good_commit": "abc",
                                "first_known_bad_commit": None,
                                "pinned_commit": None,
                                "downstream_commit": None,
                            },
                        },
                    }
                )
            )
            backend = FilesystemBackend(Path(tmp))

            # Act
            loaded = backend.load_all_statuses("regression", _UPSTREAM)

            # Assert
            assert loaded["OldDownstream"].last_good_release is None, "Pre-release schema files must load with None for new fields"
            assert loaded["OldDownstream"].last_good_release_commit is None, "Pre-release schema files must load with None for new fields"

    def test_load_all_statuses_with_pre_downstream_commit_schema_loads_field_as_none(
        self,
    ) -> None:
        """
        Even older status files (pre-``downstream_commit``) must still
        load.  This is a regression test: the field was added after the
        v2 schema shipped, and the load path must be tolerant of its
        absence rather than raising ``KeyError``.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp) / "status"
            status_dir.mkdir()
            (status_dir / "current.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "reported_at": "2026-04-01T00:00:00Z",
                        "downstreams": {
                            "OldDownstream": {
                                "last_known_good_commit": "abc",
                                "first_known_bad_commit": None,
                                "pinned_commit": None,
                            },
                        },
                    }
                )
            )
            backend = FilesystemBackend(Path(tmp))

            # Act
            loaded = backend.load_all_statuses("regression", _UPSTREAM)

            # Assert
            assert loaded["OldDownstream"].downstream_commit is None, "Pre-downstream_commit schema files must load with None"


class TestFilesystemBackendLoadTestedDownstreamCommits:
    """Tests for ``load_tested_downstream_commits`` (ondemand dedup helper)."""

    def test_load_tested_downstream_commits_with_no_runs_returns_empty_set(self) -> None:
        """
        First on-demand run: nothing has been tested yet.  Returning an
        empty set rather than raising lets the on-demand select step
        treat first-run uniformly with steady-state.
        """
        # Arrange / Act / Assert
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            assert backend.load_tested_downstream_commits("ondemand") == set(), "No saved runs ⇒ empty tested-pairs set"

    def test_load_tested_downstream_commits_returns_passed_and_failed_but_not_error(
        self,
    ) -> None:
        """
        ``load_tested_downstream_commits`` is the ondemand workflow's
        dedup helper: "have we already tested this (downstream, commit)
        pair conclusively?"  An ``error`` outcome is *not* a conclusive
        test — the build crashed for an unrelated reason — so we want
        to retry it on the next run.  Including error pairs in the set
        would silently skip retries.

        # NOTE: the production docstring on
        # ``load_tested_downstream_commits`` says "Return which
        # (downstream, downstream_commit) pairs already have a non-error
        # result"; this test is the executable form of that contract.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            results = [
                _make_run_result("ProjectA", "commit_aaa", "passed"),
                _make_run_result("ProjectB", "commit_bbb", "failed"),
                _make_run_result("ProjectC", "commit_ccc", "error"),
            ]
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream=_UPSTREAM,
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=results,
                updated_statuses={},
            )

            # Act
            seen = backend.load_tested_downstream_commits("ondemand")

            # Assert
            assert ("ProjectA", "commit_aaa") in seen, "passed must be deduped"
            assert ("ProjectB", "commit_bbb") in seen, "failed must be deduped"
            assert ("ProjectC", "commit_ccc") not in seen, "error outcomes must NOT be deduped — retry on next run"

    def test_load_tested_downstream_commits_is_scoped_by_workflow(self) -> None:
        """
        Regression and ondemand workflows have separate dedup spaces.
        A regression run that tested ``ProjectA@commit_aaa`` does not
        mean the ondemand workflow has tested it — they target
        different upstream refs (master vs the bumping branch) and
        therefore validate different things.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_reg",
                workflow="regression",
                upstream=_UPSTREAM,
                upstream_ref="master",
                run_url="https://example.com/run/reg",
                created_at="2026-04-01T00:00:00Z",
                results=[_make_run_result("ProjectA", "commit_aaa", "passed")],
                updated_statuses={},
            )

            # Act
            ondemand_pairs = backend.load_tested_downstream_commits("ondemand")

            # Assert
            assert ondemand_pairs == set(), "Regression-workflow run must not appear in ondemand dedup set"


class TestFilesystemBackendLoadPriorResults:
    """Tests for ``load_prior_results`` — richer view of historical runs."""

    def test_load_prior_results_with_empty_pairs_returns_empty_dict(self) -> None:
        """
        Passing an empty pairs set is a normal case (e.g. when every
        candidate downstream is fresh).  Empty in, empty out — and no
        DB / filesystem read needs to happen.
        """
        # Arrange / Act / Assert
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            assert backend.load_prior_results("ondemand", set()) == {}, "Empty pairs ⇒ empty dict (no I/O required)"

    def test_load_prior_results_returns_record_dicts_for_matching_pairs(self) -> None:
        """
        For each requested ``(downstream, commit)`` pair, return a row
        dict with at minimum the ``outcome`` field.  The on-demand
        select step uses the outcome to decide whether to send a
        "skipped — already tested" Zulip alert.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            results = [
                _make_run_result("ProjectA", "commit_aaa", "passed"),
                _make_run_result("ProjectB", "commit_bbb", "failed"),
            ]
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream=_UPSTREAM,
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=results,
                updated_statuses={},
            )
            pairs = {("ProjectA", "commit_aaa"), ("ProjectB", "commit_bbb")}

            # Act
            prior = backend.load_prior_results("ondemand", pairs)

            # Assert
            assert ("ProjectA", "commit_aaa") in prior
            assert ("ProjectB", "commit_bbb") in prior
            assert prior[("ProjectA", "commit_aaa")]["outcome"] == "passed"
            assert prior[("ProjectB", "commit_bbb")]["outcome"] == "failed"

    def test_load_prior_results_excludes_error_outcomes(self) -> None:
        """
        Mirror of the ``load_tested_downstream_commits`` semantics — an
        ``error`` outcome is not a conclusive prior result.  Returning
        it here would let the on-demand step claim "already tested" for
        a (downstream, commit) pair we haven't actually validated.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream=_UPSTREAM,
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=[_make_run_result("ProjectC", "commit_ccc", "error")],
                updated_statuses={},
            )
            pairs = {("ProjectC", "commit_ccc")}

            # Act
            prior = backend.load_prior_results("ondemand", pairs)

            # Assert
            assert prior == {}, "Error outcomes must not appear in load_prior_results output"

    def test_load_prior_results_returns_only_pairs_that_were_requested(self) -> None:
        """
        Passing ``{("A", "x")}`` must not return anything for
        ``("B", "y")`` even if a ``ProjectB@commit_bbb`` run exists —
        the caller has explicitly limited the query and we shouldn't
        leak data they didn't ask for.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream=_UPSTREAM,
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=[
                    _make_run_result("ProjectA", "commit_aaa", "passed"),
                    _make_run_result("ProjectB", "commit_bbb", "failed"),
                ],
                updated_statuses={},
            )
            pairs = {("ProjectA", "commit_aaa")}

            # Act
            prior = backend.load_prior_results("ondemand", pairs)

            # Assert
            assert ("ProjectA", "commit_aaa") in prior
            assert ("ProjectB", "commit_bbb") not in prior, "Unrequested pair must not appear in result"

    def test_load_prior_results_returns_newest_when_a_pair_has_multiple_runs(self) -> None:
        """
        Two runs tested ``ProjectA@commit_aaa``: an older failed and a
        newer passed.  ``load_prior_results`` returns the *newest*
        outcome — that's the current truth, not the first attempt.

        The production docstring on ``load_prior_results`` documents
        this newest-wins tie-break; this test is the executable form of
        that contract so any change to the SQL ordering in
        ``SqlBackend`` (or the in-memory iteration in
        ``FilesystemBackend``) fails here first.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_old",
                workflow="ondemand",
                upstream=_UPSTREAM,
                upstream_ref="ondemand",
                run_url="https://example.com/run/old",
                created_at="2026-04-01T00:00:00Z",
                results=[_make_run_result("ProjectA", "commit_aaa", "failed")],
                updated_statuses={},
            )
            backend.save_run(
                run_id="run_new",
                workflow="ondemand",
                upstream=_UPSTREAM,
                upstream_ref="ondemand",
                run_url="https://example.com/run/new",
                created_at="2026-04-02T00:00:00Z",
                results=[_make_run_result("ProjectA", "commit_aaa", "passed")],
                updated_statuses={},
            )
            pairs = {("ProjectA", "commit_aaa")}

            # Act
            prior = backend.load_prior_results("ondemand", pairs)

            # Assert
            assert prior[("ProjectA", "commit_aaa")]["outcome"] == "passed", "Newer run's outcome wins over older run's outcome"
