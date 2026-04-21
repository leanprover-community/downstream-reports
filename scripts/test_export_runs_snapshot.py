#!/usr/bin/env python3
"""Tests for export_runs_snapshot.py — snapshot builder, CLI, and SQL query."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
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
    )


# ---------------------------------------------------------------------------
# build_runs_snapshot() top-level tests
# ---------------------------------------------------------------------------


class BuildRunsSnapshotSchemaTests(unittest.TestCase):
    """Top-level fields and schema version."""

    def test_schema_version_is_constant(self) -> None:
        """Scenario: snapshot schema_version matches the module constant."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["schema_version"], SCHEMA_VERSION)
        self.assertEqual(snap["schema_version"], 1)

    def test_upstream_reflects_argument(self) -> None:
        """Scenario: upstream field echoes the caller-supplied value."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["upstream"], _UPSTREAM)

    def test_exported_at_ends_with_z(self) -> None:
        """Scenario: exported_at is a UTC timestamp string ending in 'Z'."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        self.assertTrue(snap["exported_at"].endswith("Z"), snap["exported_at"])

    def test_source_run_none_when_not_provided(self) -> None:
        """Scenario: source_run is null when not passed."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM, source_run=None)
        self.assertIsNone(snap["source_run"])

    def test_source_run_preserved_when_provided(self) -> None:
        """Scenario: source_run is passed through verbatim."""
        src = {"run_id": "99", "run_url": "https://example.com/runs/99"}
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM, source_run=src)
        self.assertEqual(snap["source_run"], src)

    def test_downstreams_is_dict(self) -> None:
        """Scenario: downstreams field is a dict keyed by name."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        self.assertIsInstance(snap["downstreams"], dict)


# ---------------------------------------------------------------------------
# Per-downstream entry tests
# ---------------------------------------------------------------------------


class BuildRunsSnapshotEntryTests(unittest.TestCase):
    """Per-downstream entry population."""

    def test_all_inventory_downstreams_present(self) -> None:
        """Scenario: every inventory entry appears in the snapshot."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        self.assertIn("physlib", snap["downstreams"])
        self.assertIn("alglib", snap["downstreams"])

    def test_entry_without_run_has_nulls_but_preserves_repo(self) -> None:
        """Scenario: downstream with no run history gets null fields and inventory repo."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        self.assertEqual(entry["repo"], "leanprover-community/physlib")
        self.assertEqual(entry["dependency_name"], "mathlib")
        self.assertIsNone(entry["run_id"])
        self.assertIsNone(entry["run_url"])
        self.assertIsNone(entry["target_commit"])
        self.assertIsNone(entry["downstream_commit"])
        self.assertIsNone(entry["outcome"])
        self.assertIsNone(entry["episode_state"])
        self.assertIsNone(entry["first_known_bad_commit"])
        self.assertIsNone(entry["last_known_good_commit"])

    def test_entry_without_run_still_has_result_artifact_name(self) -> None:
        """Scenario: result_artifact_name is derived from downstream name even without runs."""
        snap = build_runs_snapshot({}, _INVENTORY, _UPSTREAM)
        self.assertEqual(
            snap["downstreams"]["physlib"]["result_artifact_name"], "result-physlib"
        )

    def test_full_run_populates_all_fields(self) -> None:
        """Scenario: a complete LatestRunRecord populates every downstream field."""
        run = _make_run()
        snap = build_runs_snapshot({"physlib": run}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        self.assertEqual(entry["run_id"], "1")
        self.assertEqual(entry["run_url"], "https://example.com/runs/1")
        self.assertEqual(entry["job_id"], "job_42")
        self.assertEqual(entry["job_url"], "https://example.com/jobs/42")
        self.assertEqual(entry["reported_at"], "2026-04-20T06:00:00Z")
        self.assertEqual(entry["target_commit"], "target_abc")
        self.assertEqual(entry["downstream_commit"], "ds_head")
        self.assertEqual(entry["outcome"], "failed")
        self.assertEqual(entry["episode_state"], "failing")
        self.assertEqual(entry["first_known_bad_commit"], "bad_xyz")
        self.assertEqual(entry["last_known_good_commit"], "good_uvw")

    def test_run_without_job_metadata_produces_null_job_fields(self) -> None:
        """Scenario: LatestRunRecord with no job_id/job_url renders null job fields."""
        run = _make_run(job_id=None, job_url=None)
        snap = build_runs_snapshot({"physlib": run}, _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        self.assertIsNone(entry["job_id"])
        self.assertIsNone(entry["job_url"])
        # run-level fields still present
        self.assertEqual(entry["run_id"], "1")
        self.assertEqual(entry["run_url"], "https://example.com/runs/1")

    def test_partial_run_with_some_downstreams_only(self) -> None:
        """Scenario: a downstream with run data coexists with one that has none."""
        snap = build_runs_snapshot({"physlib": _make_run()}, _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["downstreams"]["physlib"]["run_id"], "1")
        self.assertIsNone(snap["downstreams"]["alglib"]["run_id"])

    def test_empty_reported_at_string_rendered_as_null(self) -> None:
        """Scenario: empty reported_at (no DB row) renders as null, not empty string."""
        run = _make_run(reported_at="")
        snap = build_runs_snapshot({"physlib": run}, _INVENTORY, _UPSTREAM)
        self.assertIsNone(snap["downstreams"]["physlib"]["reported_at"])

    def test_extra_latest_runs_entry_is_ignored_when_not_in_inventory(self) -> None:
        """Scenario: latest_runs may contain names absent from the inventory; they're dropped."""
        snap = build_runs_snapshot(
            {"physlib": _make_run(), "ghost": _make_run(run_id="2")},
            _INVENTORY,
            _UPSTREAM,
        )
        self.assertIn("physlib", snap["downstreams"])
        self.assertNotIn("ghost", snap["downstreams"])


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class MainCliTests(unittest.TestCase):
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

            self.assertEqual(rc, 0)
            return json.loads(out_path.read_text())

    def test_main_returns_zero_on_success(self) -> None:
        """Scenario: successful export exits with code 0."""
        snap = self._run()
        self.assertIsNotNone(snap)

    def test_disabled_downstreams_included(self) -> None:
        """Scenario: export uses include_disabled=True so disabled entries still appear."""
        snap = self._run()
        self.assertIn("disabled-project", snap["downstreams"])

    def test_output_is_valid_json(self) -> None:
        """Scenario: output file is parseable JSON with expected top-level keys."""
        snap = self._run()
        self.assertIn("schema_version", snap)
        self.assertIn("upstream", snap)
        self.assertIn("downstreams", snap)

    def test_custom_upstream_reflected(self) -> None:
        """Scenario: --upstream arg is reflected in snapshot output."""
        snap = self._run(extra_argv=["--upstream", "some-org/some-upstream"])
        self.assertEqual(snap["upstream"], "some-org/some-upstream")

    def test_dry_run_backend_does_not_query_sql(self) -> None:
        """Scenario: dry-run backend skips SQL entirely; source_run remains null."""
        snap = self._run()
        self.assertIsNone(snap["source_run"])

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
            self.assertEqual(rc, 0)
            ml.assert_called_once()
            fs.assert_called_once()
            snap = json.loads(out_path.read_text())
            self.assertEqual(snap["source_run"], src)


# ---------------------------------------------------------------------------
# _fetch_source_run() tests
# ---------------------------------------------------------------------------


class FetchSourceRunTests(unittest.TestCase):
    """Tests for the _fetch_source_run() helper (mirrors the LKG export variant)."""

    def test_returns_none_when_no_dsn(self) -> None:
        """Scenario: no DSN available → returns None without raising."""
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(_fetch_source_run(None))

    def test_returns_none_when_no_runs_found(self) -> None:
        """Scenario: SQL backend has no regression runs → returns None."""
        with (
            patch("scripts.storage.latest_regression_run_id", return_value=None),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
        ):
            self.assertIsNone(_fetch_source_run("postgresql://fake"))

    def test_returns_dict_with_run_id_and_url(self) -> None:
        """Scenario: SQL has a run → returns dict with run_id and run_url."""
        with (
            patch("scripts.storage.latest_regression_run_id", return_value="99"),
            patch("sqlalchemy.create_engine", return_value=MagicMock()),
            patch.dict("os.environ", {"GITHUB_REPOSITORY": "owner/repo"}),
        ):
            result = _fetch_source_run("postgresql://fake")
        self.assertIsNotNone(result)
        self.assertEqual(result["run_id"], "99")
        self.assertIn("owner/repo", result["run_url"])
        self.assertIn("99", result["run_url"])

    def test_returns_none_on_exception(self) -> None:
        """Scenario: DB error is swallowed and None is returned."""
        with patch("sqlalchemy.create_engine", side_effect=RuntimeError("boom")):
            self.assertIsNone(_fetch_source_run("postgresql://fake"))


# ---------------------------------------------------------------------------
# SQL query tests (load_latest_run_per_downstream against real SQLite)
# ---------------------------------------------------------------------------


class LoadLatestRunPerDownstreamTests(unittest.TestCase):
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
        self.assertEqual(load_latest_run_per_downstream(engine, "regression", _UPSTREAM), {})

    def test_single_run_single_downstream(self) -> None:
        """Scenario: one run with one result returns one LatestRunRecord."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        reported = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "1", reported)

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        self.assertIn("physlib", result)
        rec = result["physlib"]
        self.assertEqual(rec.run_id, "1")
        self.assertEqual(rec.run_url, "https://example.com/runs/1")
        self.assertEqual(rec.outcome, "failed")
        self.assertEqual(rec.first_known_bad, "bad_xyz")
        self.assertEqual(rec.downstream_commit, "ds_head")

    def test_returns_latest_run_when_multiple(self) -> None:
        """Scenario: when multiple runs exist, the newest wins per downstream."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        older = datetime(2026, 4, 19, 6, 0, 0, tzinfo=timezone.utc)
        newer = older + timedelta(days=1)
        self._seed_run(engine, "old-run", older, target_commit="older_target")
        self._seed_run(engine, "new-run", newer, target_commit="newer_target")

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        self.assertEqual(result["physlib"].run_id, "new-run")
        self.assertEqual(result["physlib"].target_commit, "newer_target")

    def test_ignores_other_workflow(self) -> None:
        """Scenario: rows from workflow=ondemand are excluded from regression snapshot."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "od-1", when, workflow="ondemand")

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        self.assertEqual(result, {})

    def test_ignores_other_upstream(self) -> None:
        """Scenario: rows with a different upstream are excluded."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "x-1", when, upstream="leanprover-community/other")

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        self.assertEqual(result, {})

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
        self.assertEqual(rec.job_id, "7777")
        self.assertEqual(rec.job_url, "https://example.com/jobs/7777")

    def test_null_job_when_no_validate_job_row(self) -> None:
        """Scenario: missing validate_job leaves job_id / job_url as None."""
        from scripts.storage import load_latest_run_per_downstream

        engine, _ = self._engine()
        when = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
        self._seed_run(engine, "no-job", when)  # no job_id/job_url → no validate_job row

        result = load_latest_run_per_downstream(engine, "regression", _UPSTREAM)
        rec = result["physlib"]
        self.assertIsNone(rec.job_id)
        self.assertIsNone(rec.job_url)

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
        self.assertEqual(result["physlib"].target_commit, "new_phys")
        self.assertEqual(result["alglib"].target_commit, "alg_tgt")


if __name__ == "__main__":
    unittest.main()
