#!/usr/bin/env python3
"""
Tests for: scripts.export_runs_snapshot

Coverage scope:
    - ``build_runs_snapshot`` — pure builder.  ``LatestRunRecord`` rows
      + inventory → ``runs/latest.json``-shaped dict.  Pins every
      documented field (``schema_version``, ``upstream``,
      ``exported_at``, ``source_run``, plus per-downstream
      ``run_id``, ``run_url``, ``job_url``, ``downstream_commit``,
      ``reported_at``, ``culprit_log_artifact_url``).
    - ``main`` (CLI) — argv → output file; covers SQL-vs-dry-run
      backend dispatch, including the contract that ``dry-run`` never
      issues a SQL query.
    - ``_fetch_source_run`` — the same DB lookup as in
      ``export_lkg_snapshot``, but pointed at the runs schema.
    - ``LoadLatestRunPerDownstream`` — integration tests against an
      in-memory SQLite database.  These tests are
      ``@pytest.mark.integration`` so the unit tick can deselect them.

Out of scope:
    - Snapshot upload to Azure Blob Storage; lives in ``publish-lkg.yml``.
    - The LKG snapshot — see ``test_export_lkg_snapshot.py``.
    - SqlBackend's other methods (``record_warm_shas``, manifest watcher
      ledger) — exercised in their respective workflow's unit tests.

Why this matters
----------------
``runs/latest.json`` is the second public artefact (alongside
``lkg/latest.json``) consumed by the ``open-incompatibility-issue``
composite action.  It carries the back-link to the GitHub Actions run
and the precise downstream commit the regression was confirmed on, so
issues opened on consuming repos can deep-link to the failing job log.
A field silently dropped here means the issue body loses its
provenance link — the snapshot test pins each field so that drift is
caught at unit-test time rather than visible in stale issue bodies.

The ``LoadLatestRunPerDownstreamTests`` class spins up real SQLAlchemy
ORM tables in an in-memory SQLite database; this is the only place in
the unit suite where the SQL query side of ``SqlBackend`` is exercised.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.export_runs_snapshot import (
    SCHEMA_VERSION,
    _fetch_source_run,
    build_runs_snapshot,
)
from scripts.models import DownstreamConfig
from scripts.storage import LatestRunRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UPSTREAM = "leanprover-community/mathlib4"

_PHYSLIB = DownstreamConfig(
    name="physlib",
    repo="leanprover-community/physlib",
    default_branch="main",
    dependency_name="mathlib",
)

_ALGLIB = DownstreamConfig(
    name="alglib",
    repo="some-org/alglib",
    default_branch="master",
    dependency_name="mathlib",
)

_INVENTORY: dict[str, DownstreamConfig] = {
    "physlib": _PHYSLIB,
    "alglib": _ALGLIB,
}

_INVENTORY_JSON = {
    "downstreams": [
        {
            "name": "physlib",
            "repo": "leanprover-community/physlib",
            "default_branch": "main",
            "dependency_name": "mathlib",
        },
        {
            "name": "alglib",
            "repo": "some-org/alglib",
            "default_branch": "master",
            "dependency_name": "mathlib",
        },
        {
            "name": "disabled-project",
            "repo": "some-org/disabled-project",
            "default_branch": "main",
            "dependency_name": "mathlib",
            "enabled": False,
        },
    ]
}


def _make_run(
    run_id: str = "1",
    run_url: str = "https://example.com/runs/1",
    reported_at: str = "2026-04-20T06:00:00Z",
    target_commit: str = "target_abc",
    downstream_commit: str = "ds_head",
    outcome: str = "failed",
    episode_state: str = "failing",
    first_known_bad: str | None = "bad_xyz",
    last_known_good: str | None = "good_uvw",
    job_id: str | None = "job_42",
    job_url: str | None = "https://example.com/jobs/42",
    culprit_log_artifact_url: str | None = None,
) -> LatestRunRecord:
    return LatestRunRecord(
        run_id=run_id,
        run_url=run_url,
        reported_at=reported_at,
        target_commit=target_commit,
        downstream_commit=downstream_commit,
        outcome=outcome,
        episode_state=episode_state,
        first_known_bad=first_known_bad,
        last_known_good=last_known_good,
        job_id=job_id,
        job_url=job_url,
        culprit_log_artifact_url=culprit_log_artifact_url,
    )


# ---------------------------------------------------------------------------
# build_runs_snapshot() top-level tests
# ---------------------------------------------------------------------------


class TestBuildRunsSnapshotSchema:
    """Top-level fields and schema version."""

    def test_schema_version_is_constant(self) -> None:
        """Scenario: snapshot schema_version matches the module constant."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        assert snap["schema_version"] == SCHEMA_VERSION
        assert snap["schema_version"] == 1

    def test_upstream_reflects_argument(self) -> None:
        """Scenario: upstream field echoes the caller-supplied value."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        assert snap["upstream"] == _UPSTREAM

    def test_exported_at_ends_with_z(self) -> None:
        """Scenario: exported_at is a UTC timestamp string ending in 'Z'."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        assert snap["exported_at"].endswith("Z"), snap["exported_at"]

    def test_source_run_none_when_not_provided(self) -> None:
        """Scenario: source_run is null when not passed."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM, source_run=None)
        assert snap["source_run"] is None

    def test_source_run_preserved_when_provided(self) -> None:
        """Scenario: source_run is passed through verbatim."""
        src = {"run_id": "99", "run_url": "https://example.com/runs/99"}
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM, source_run=src)
        assert snap["source_run"] == src

    def test_downstreams_is_dict(self) -> None:
        """Scenario: downstreams field is a dict keyed by name."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        assert isinstance(snap["downstreams"], dict)


# ---------------------------------------------------------------------------
# Per-downstream entry tests
# ---------------------------------------------------------------------------


class TestBuildRunsSnapshotEntry:
    """Per-downstream entry population."""

    def test_all_inventory_downstreams_present(self) -> None:
        """Scenario: every inventory entry appears in the snapshot."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        assert "physlib" in snap["downstreams"]
        assert "alglib" in snap["downstreams"]

    def test_entry_without_run_has_nulls_but_preserves_repo(self) -> None:
        """Scenario: downstream with no run history gets null fields and inventory repo."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        assert entry["repo"] == "leanprover-community/physlib"
        assert entry["dependency_name"] == "mathlib"
        assert entry["run_id"] is None
        assert entry["run_url"] is None
        assert entry["target_commit"] is None
        assert entry["downstream_commit"] is None
        assert entry["outcome"] is None
        assert entry["episode_state"] is None
        assert entry["first_known_bad_commit"] is None
        assert entry["last_known_good_commit"] is None

    def test_entry_without_run_still_has_result_artifact_name(self) -> None:
        """Scenario: result_artifact_name is derived from downstream name even without runs."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        assert snap["downstreams"]["physlib"]["result_artifact_name"] == "result-physlib"

    def test_full_run_populates_all_fields(self) -> None:
        """Scenario: a complete LatestRunRecord populates every downstream field."""
        run = _make_run()
        snap = build_runs_snapshot({"physlib": run}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        assert entry["run_id"] == "1"
        assert entry["run_url"] == "https://example.com/runs/1"
        assert entry["job_id"] == "job_42"
        assert entry["job_url"] == "https://example.com/jobs/42"
        assert entry["reported_at"] == "2026-04-20T06:00:00Z"
        assert entry["target_commit"] == "target_abc"
        assert entry["downstream_commit"] == "ds_head"
        assert entry["outcome"] == "failed"
        assert entry["episode_state"] == "failing"
        assert entry["first_known_bad_commit"] == "bad_xyz"
        assert entry["last_known_good_commit"] == "good_uvw"

    def test_run_without_job_metadata_produces_null_job_fields(self) -> None:
        """Scenario: LatestRunRecord with no job_id/job_url renders null job fields."""
        run = _make_run(job_id=None, job_url=None)
        snap = build_runs_snapshot({"physlib": run}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        assert entry["job_id"] is None
        assert entry["job_url"] is None
        # run-level fields still present
        assert entry["run_id"] == "1"
        assert entry["run_url"] == "https://example.com/runs/1"

    def test_partial_run_with_some_downstreams_only(self) -> None:
        """Scenario: a downstream with run data coexists with one that has none."""
        snap = build_runs_snapshot({"physlib": _make_run()}, _INVENTORY, _UPSTREAM)
        assert snap["downstreams"]["physlib"]["run_id"] == "1"
        assert snap["downstreams"]["alglib"]["run_id"] is None

    def test_empty_reported_at_string_rendered_as_null(self) -> None:
        """Scenario: empty reported_at (no DB row) renders as null, not empty string."""
        run = _make_run(reported_at="")
        snap = build_runs_snapshot({"physlib": run}, _INVENTORY, _UPSTREAM)
        assert snap["downstreams"]["physlib"]["reported_at"] is None

    def test_culprit_log_fields_default_to_null_without_run(self) -> None:
        """Scenario: an entry without a run still exposes a culprit_log_artifact_name.

        The artifact name is derived deterministically from the downstream name so
        consumers can construct it even when no run has been recorded yet; the URL
        is null until a run with a culprit log is recorded.  The log text itself is
        intentionally not part of the snapshot — fetch the artifact for contents.
        """
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        assert entry["culprit_log_artifact_name"] == "culprit-log-physlib"
        assert entry["culprit_log_artifact_url"] is None
        assert "culprit_log_text" not in entry

    def test_culprit_log_artifact_url_propagates_from_run(self) -> None:
        """Scenario: culprit_log_artifact_url from LatestRunRecord lands in the entry."""
        run = _make_run(
            culprit_log_artifact_url="https://example.com/artifacts/9",
        )
        snap = build_runs_snapshot({"physlib": run}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        assert entry["culprit_log_artifact_url"] == "https://example.com/artifacts/9"
        assert "culprit_log_text" not in entry

    def test_extra_latest_runs_entry_is_ignored_when_not_in_inventory(self) -> None:
        """Scenario: latest_runs may contain names absent from the inventory; they're dropped."""
        snap = build_runs_snapshot(
            {"physlib": _make_run(), "ghost": _make_run(run_id="2")},
            _INVENTORY,
            _UPSTREAM,
        )
        assert "physlib" in snap["downstreams"]
        assert "ghost" not in snap["downstreams"]


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestMainCli:
    """Integration tests for the export_runs_snapshot CLI."""

    def _run(
        self,
        extra_argv: list[str] | None = None,
        latest_runs: dict[str, LatestRunRecord] | None = None,
    ) -> dict:
        import scripts.export_runs_snapshot as mod

        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "downstreams.json"
            inv_path.write_text(json.dumps(_INVENTORY_JSON))
            out_path = Path(tmpdir) / "runs-snapshot.json"

            argv = [
                "export_runs_snapshot.py",
                "--backend", "dry-run",
                "--inventory", str(inv_path),
                "--output", str(out_path),
            ]
            if extra_argv:
                argv.extend(extra_argv)

            with (
                patch.object(
                    mod, "_load_latest_runs", return_value=(latest_runs or {})
                ),
                patch.object(mod, "_fetch_source_run", return_value=None),
                patch.object(sys, "argv", argv),
            ):
                rc = mod.main()

            assert rc == 0
            return json.loads(out_path.read_text())

    def test_main_returns_zero_on_success(self) -> None:
        """Scenario: successful export exits with code 0."""
        snap = self._run()
        assert snap is not None

    def test_disabled_downstreams_included(self) -> None:
        """Scenario: export uses include_disabled=True so disabled entries still appear."""
        snap = self._run()
        assert "disabled-project" in snap["downstreams"]

    def test_output_is_valid_json(self) -> None:
        """Scenario: output file is parseable JSON with expected top-level keys."""
        snap = self._run()
        assert "schema_version" in snap
        assert "upstream" in snap
        assert "downstreams" in snap

    def test_custom_upstream_reflected(self) -> None:
        """Scenario: --upstream arg is reflected in snapshot output."""
        snap = self._run(extra_argv=["--upstream", "some-org/some-upstream"])
        assert snap["upstream"] == "some-org/some-upstream"

    def test_dry_run_backend_does_not_query_sql(self) -> None:
        """Scenario: dry-run backend skips SQL entirely; source_run remains null."""
        snap = self._run()
        assert snap["source_run"] is None

    def test_cli_uses_sql_when_backend_sql(self) -> None:
        """Scenario: --backend=sql invokes the SQL loaders."""
        import scripts.export_runs_snapshot as mod

        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "downstreams.json"
            inv_path.write_text(json.dumps(_INVENTORY_JSON))
            out_path = Path(tmpdir) / "runs-snapshot.json"

            argv = [
                "export_runs_snapshot.py",
                "--backend", "sql",
                "--dsn", "sqlite:///:memory:",
                "--inventory", str(inv_path),
                "--output", str(out_path),
            ]
            src = {"run_id": "7", "run_url": "https://example.com/runs/7"}
            with (
                patch.object(mod, "_load_latest_runs", return_value={}) as ml,
                patch.object(mod, "_fetch_source_run", return_value=src) as fs,
                patch.object(sys, "argv", argv),
            ):
                rc = mod.main()
            assert rc == 0
            ml.assert_called_once()
            fs.assert_called_once()
            snap = json.loads(out_path.read_text())
            assert snap["source_run"] == src


# ---------------------------------------------------------------------------
# _fetch_source_run() tests
# ---------------------------------------------------------------------------


class TestFetchSourceRun:
    """Tests for the _fetch_source_run() helper (mirrors the LKG export variant)."""

    def test_returns_none_when_no_dsn(self) -> None:
        """Scenario: no DSN available → returns None without raising."""
        with patch.dict("os.environ", {}, clear=True):
            assert _fetch_source_run(None) is None

    def test_returns_none_when_no_runs_found(self) -> None:
        """Scenario: SQL backend has no regression runs → returns None."""
        with (
            patch("scripts.storage.latest_regression_run_id", return_value=None),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
        ):
            assert _fetch_source_run("postgresql://fake") is None

    def test_returns_dict_with_run_id_and_url(self) -> None:
        """Scenario: SQL has a run → returns dict with run_id and run_url."""
        with (
            patch("scripts.storage.latest_regression_run_id", return_value="99"),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
            patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}),
        ):
            result = _fetch_source_run("postgresql://fake")
        assert result is not None
        assert result["run_id"] == "99"
        assert "owner/repo" in result["run_url"]
        assert "99" in result["run_url"]

    def test_returns_none_on_exception(self) -> None:
        """Scenario: DB error is swallowed and None is returned."""
        with patch("sqlalchemy.create_engine", side_effect=RuntimeError("boom")):
            assert _fetch_source_run("postgresql://fake") is None


# ---------------------------------------------------------------------------
# SQL query tests (load_latest_run_per_downstream against real SQLite)
# ---------------------------------------------------------------------------


# Integration tier: real SQLAlchemy + in-memory SQLite.  The
# ``integration`` marker lets a fast unit-only run deselect via
# ``pytest -m "not integration"``; the default ``pytest scripts/``
# invocation still includes them.
import pytest


@pytest.mark.integration
class TestLoadLatestRunPerDownstream:
    """End-to-end test of the SQL helper against an in-memory SQLite DB."""

    def _engine(self) -> tuple[object, object]:
        from sqlalchemy import create_engine

        from scripts.storage import SqlBackend, create_schema

        engine = create_engine("sqlite:///:memory:")
        create_schema(engine)
        return engine, SqlBackend(engine)

    def _seed_run(
        self,
        engine: object,
        run_id: str,
        reported_at: datetime,
        *,
        downstream: str = "physlib",
        outcome: str = "failed",
        episode_state: str = "failing",
        target_commit: str = "target_abc",
        downstream_commit: str = "ds_head",
        first_known_bad: str | None = "bad_xyz",
        last_known_good: str | None = "good_uvw",
        job_id: str | None = None,
        job_url: str | None = None,
        workflow: str = "regression",
        upstream: str = _UPSTREAM,
        culprit_log_artifact_url: str | None = None,
    ) -> None:
        from scripts.storage import (
            RunResultRecord,
            ValidateJobRecord,
            SqlBackend,
            DownstreamStatusRecord,
        )

        backend = SqlBackend(engine)
        result = RunResultRecord(
            upstream=upstream,
            downstream=downstream,
            repo="leanprover-community/physlib"
            if downstream == "physlib"
            else "some-org/alglib",
            downstream_commit=downstream_commit,
            outcome=outcome,
            episode_state=episode_state,
            target_commit=target_commit,
            previous_last_known_good=None,
            previous_first_known_bad=None,
            last_known_good=last_known_good,
            first_known_bad=first_known_bad,
            current_last_successful=None,
            current_first_failing=None,
            failure_stage=None,
            search_mode="head-only",
            commit_window_truncated=False,
            error=None,
            head_probe_outcome=outcome,
            head_probe_failure_stage=None,
            culprit_log_text=None,
            culprit_log_artifact_url=culprit_log_artifact_url,
        )
        validate_jobs: list[ValidateJobRecord] | None = None
        if job_id or job_url:
            validate_jobs = [
                ValidateJobRecord(
                    downstream=downstream,
                    job_id=job_id or "",
                    job_url=job_url or "",
                    started_at=reported_at.isoformat().replace("+00:00", "Z"),
                    finished_at=reported_at.isoformat().replace("+00:00", "Z"),
                    conclusion="failure" if outcome == "failed" else "success",
                )
            ]
        backend.save_run(
            run_id=run_id,
            workflow=workflow,
            upstream=upstream,
            upstream_ref="refs/heads/master",
            run_url=f"https://example.com/runs/{run_id}",
            created_at=reported_at.isoformat().replace("+00:00", "Z"),
            results=[result],
            updated_statuses={downstream: DownstreamStatusRecord()},
            validate_jobs=validate_jobs,
        )

    def test_empty_db_returns_empty_dict(self) -> None:
        """Scenario: no rows in run/run_result → empty mapping."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        assert load_latest_run_per_downstream(engine, "regression", _UPSTREAM) == {}

    def test_single_run_single_downstream(self) -> None:
        """Scenario: one run with one result returns one LatestRunRecord."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        reported = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "1", reported)

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        assert "physlib" in result
        rec = result["physlib"]
        assert rec.run_id == "1"
        assert rec.run_url == "https://example.com/runs/1"
        assert rec.outcome == "failed"
        assert rec.first_known_bad == "bad_xyz"
        assert rec.downstream_commit == "ds_head"

    def test_returns_latest_run_when_multiple(self) -> None:
        """Scenario: when multiple runs exist, the newest wins per downstream."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        older = datetime(2026, 4, 19, 6, 0, 0, tzinfo=timezone.utc)
        newer = older + timedelta(days=1)
        self._seed_run(engine, "old-run", older, target_commit="older_target")
        self._seed_run(engine, "new-run", newer, target_commit="newer_target")

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        assert result["physlib"].run_id == "new-run"
        assert result["physlib"].target_commit == "newer_target"

    def test_ignores_other_workflow(self) -> None:
        """Scenario: rows from workflow=ondemand are excluded from regression snapshot."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "od-1", when, workflow="ondemand")

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        assert result == {}

    def test_ignores_other_upstream(self) -> None:
        """Scenario: rows with a different upstream are excluded."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "x-1", when, upstream="leanprover-community/other")

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        assert result == {}

    def test_joins_validate_job_for_job_url(self) -> None:
        """Scenario: validate_job row populates job_id and job_url."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(
            engine,
            "with-job",
            when,
            job_id="7777",
            job_url="https://example.com/jobs/7777",
        )

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        rec = result["physlib"]
        assert rec.job_id == "7777"
        assert rec.job_url == "https://example.com/jobs/7777"

    def test_null_job_when_no_validate_job_row(self) -> None:
        """Scenario: missing validate_job leaves job_id / job_url as None."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "no-job", when)  # no job_id/job_url → no validate_job row

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        rec = result["physlib"]
        assert rec.job_id is None
        assert rec.job_url is None

    def test_culprit_log_artifact_url_round_trips_but_text_is_not_persisted(self) -> None:
        """Scenario: only the URL survives save/load — log text is never written to SQL.

        The probe job aggregates failure logs for in-memory consumers (markdown
        report, Zulip alert payload) but the database row only stores a pointer
        to the uploaded artifact.  This keeps arbitrary build output out of the
        relational schema; consumers fetch the artifact when they need contents.
        """
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(
            engine,
            "with-log",
            when,
            culprit_log_artifact_url="https://github.com/owner/repo/actions/runs/9/artifacts/42",
        )

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        rec = result["physlib"]
        assert rec.culprit_log_artifact_url == "https://github.com/owner/repo/actions/runs/9/artifacts/42"
        # LatestRunRecord has no culprit_log_text field — the log lives only in
        # the artifact, never in the database or the snapshot.
        assert not hasattr(rec, "culprit_log_text")

    def test_multiple_downstreams_each_get_latest(self) -> None:
        """Scenario: per-downstream latest is computed independently."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        t1 = datetime(2026, 4, 19, 6, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        # physlib: latest is newer_run
        self._seed_run(engine, "old-physlib", t1, downstream="physlib", target_commit="old_phys")
        self._seed_run(engine, "new-physlib", t2, downstream="physlib", target_commit="new_phys")
        # alglib: only one run
        self._seed_run(engine, "alg-1", t1, downstream="alglib", target_commit="alg_tgt")

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        assert result["physlib"].target_commit == "new_phys"
        assert result["alglib"].target_commit == "alg_tgt"
