#!/usr/bin/env python3
"""
Tests for: scripts.export_status_snapshot (and the storage snapshot helpers)

Coverage scope:
    - ``storage.write_status_snapshot`` / ``storage.read_status_snapshot``
      / ``storage.status_snapshot_payload`` — the staged snapshot must
      round-trip exactly, and the reader must reject snapshots staged for
      a different (workflow, upstream) or schema version.
    - ``main`` (CLI) — argv → snapshot file.  Integration-style: stages
      from a SQLite source backend and from the dry-run backend.

Out of scope:
    - SQL reads (``SqlBackend.load_all_statuses`` is covered by
      ``test_storage.py``); the CLI only composes backend + writer.
    - Artifact upload/download; that lives in the workflow YAML.

Why this matters
----------------
The plan job stages this snapshot once and every select leg reads prior
episode state from it instead of dialing the database — the snapshot IS the
select fan-out's view of ``downstream_status``.  A shape drift between
``write_status_snapshot`` and ``read_status_snapshot`` would silently feed
the skip heuristics empty prior state, disabling ``try_skip_already_good``
and ``try_skip_known_bad_bisect`` across the board.  The round-trip tests
here pin that contract.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main as unittest_main
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.export_status_snapshot import main as export_main
from scripts.storage import (
    DownstreamStatusRecord,
    SqlBackend,
    create_schema,
    create_sql_engine,
    read_status_snapshot,
    status_snapshot_payload,
    write_status_snapshot,
)


_UPSTREAM = "leanprover-community/mathlib4"

_STATUSES: dict[str, DownstreamStatusRecord] = {
    "physlib": DownstreamStatusRecord(
        last_known_good_commit="aaa111",
        first_known_bad_commit="bbb222",
        pinned_commit="ccc333",
        downstream_commit="ddd444",
        last_good_release="v4.13.0",
        last_good_release_commit="eee555",
    ),
    "alglib": DownstreamStatusRecord(
        last_known_good_commit="fff666",
    ),
}


def _write(path: Path, workflow: str = "regression") -> Path:
    return write_status_snapshot(
        path, _STATUSES,
        workflow=workflow, upstream=_UPSTREAM, reported_at="2026-06-10T00:00:00Z",
    )


class StatusSnapshotRoundTripTests(TestCase):
    def test_round_trip_regression(self) -> None:
        """Scenario: a snapshot staged for the regression workflow is read back
        verbatim by read_status_snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "status.json")
            loaded = read_status_snapshot(path, workflow="regression", upstream=_UPSTREAM)
        self.assertEqual(loaded, _STATUSES)

    def test_round_trip_ondemand(self) -> None:
        """Scenario: a snapshot staged for the ondemand workflow round-trips
        through the same reader."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "status.json", workflow="ondemand")
            loaded = read_status_snapshot(path, workflow="ondemand", upstream=_UPSTREAM)
        self.assertEqual(loaded, _STATUSES)

    def test_empty_statuses_round_trip(self) -> None:
        """Scenario: zero downstreams (dry-run source) still writes a valid
        snapshot file that loads back as an empty dict."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_status_snapshot(
                Path(tmp) / "status.json", {},
                workflow="regression", upstream=_UPSTREAM,
                reported_at="2026-06-10T00:00:00Z",
            )
            self.assertTrue(path.exists())
            loaded = read_status_snapshot(path, workflow="regression", upstream=_UPSTREAM)
        self.assertEqual(loaded, {})

    def test_missing_per_downstream_fields_load_as_none(self) -> None:
        """Scenario: a snapshot entry without the optional fields (e.g. release
        metadata) loads with None for each absent field rather than raising."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            payload = status_snapshot_payload(
                {}, workflow="regression", upstream=_UPSTREAM,
                reported_at="2026-06-10T00:00:00Z",
            )
            payload["downstreams"] = {"sparse": {"last_known_good_commit": "abc"}}
            path.write_text(json.dumps(payload))
            loaded = read_status_snapshot(path, workflow="regression", upstream=_UPSTREAM)
        self.assertEqual(loaded["sparse"].last_known_good_commit, "abc")
        self.assertIsNone(loaded["sparse"].downstream_commit)
        self.assertIsNone(loaded["sparse"].last_good_release)

    def test_payload_shape_is_pinned(self) -> None:
        """Scenario: the snapshot payload keeps schema_version 3, embeds its
        (workflow, upstream) provenance, and uses the exact per-downstream
        field names read_status_snapshot reads."""
        payload = status_snapshot_payload(
            _STATUSES, workflow="regression", upstream=_UPSTREAM,
            reported_at="2026-06-10T00:00:00Z",
        )
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["workflow"], "regression")
        self.assertEqual(payload["upstream"], _UPSTREAM)
        self.assertEqual(payload["reported_at"], "2026-06-10T00:00:00Z")
        self.assertEqual(
            set(payload["downstreams"]["physlib"]),
            {
                "last_known_good_commit",
                "first_known_bad_commit",
                "pinned_commit",
                "downstream_commit",
                "last_good_release",
                "last_good_release_commit",
            },
        )


class ReadStatusSnapshotValidationTests(TestCase):
    """The reader fails loudly on wiring errors instead of returning empty state."""

    def test_missing_file_raises_system_exit(self) -> None:
        """Scenario: pointing --status-snapshot at a nonexistent file is a
        workflow wiring error, not a first-run situation."""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                read_status_snapshot(
                    Path(tmp) / "missing.json", workflow="regression", upstream=_UPSTREAM,
                )

    def test_workflow_mismatch_raises_system_exit(self) -> None:
        """Scenario: a snapshot staged for the ondemand workflow must be
        rejected by a regression select leg."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "status.json", workflow="ondemand")
            with self.assertRaises(SystemExit):
                read_status_snapshot(path, workflow="regression", upstream=_UPSTREAM)

    def test_upstream_mismatch_raises_system_exit(self) -> None:
        """Scenario: a snapshot staged for a different upstream must be
        rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "status.json")
            with self.assertRaises(SystemExit):
                read_status_snapshot(path, workflow="regression", upstream="other/upstream")

    def test_unexpected_schema_version_raises_system_exit(self) -> None:
        """Scenario: a snapshot with a different schema_version means writer and
        reader are out of sync; reading it anyway could drop fields silently."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "status.json")
            payload = json.loads(path.read_text())
            payload["schema_version"] = 2
            path.write_text(json.dumps(payload))
            with self.assertRaises(SystemExit):
                read_status_snapshot(path, workflow="regression", upstream=_UPSTREAM)


class ExportStatusSnapshotCliTests(TestCase):
    def test_cli_stages_from_sql_source(self) -> None:
        """Scenario: the CLI reads prior state from a SQL source and writes a
        snapshot file that read_status_snapshot accepts for the select step."""
        with tempfile.TemporaryDirectory() as tmp:
            dsn = f"sqlite:///{tmp}/state.db"
            engine = create_sql_engine(dsn)
            create_schema(engine)
            SqlBackend(engine).save_run(
                run_id="run_1",
                workflow="regression",
                upstream=_UPSTREAM,
                upstream_ref="master",
                run_url="https://example.com/run/1",
                created_at="2026-06-09T00:00:00Z",
                results=[],
                updated_statuses=_STATUSES,
            )
            output = Path(tmp) / "snapshot" / "status.json"
            argv = [
                "export_status_snapshot.py",
                "--backend", "sql",
                "--dsn", dsn,
                "--workflow", "regression",
                "--upstream", _UPSTREAM,
                "--output", str(output),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(export_main(), 0)
            loaded = read_status_snapshot(output, workflow="regression", upstream=_UPSTREAM)
        self.assertEqual(loaded, _STATUSES)

    def test_cli_dry_run_writes_empty_snapshot(self) -> None:
        """Scenario: in dry-run mode the CLI writes a snapshot with zero
        downstreams so select legs see the same empty prior state a dry-run
        database read would have produced."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot" / "status.json"
            argv = [
                "export_status_snapshot.py",
                "--backend", "dry-run",
                "--workflow", "regression",
                "--output", str(output),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(export_main(), 0)
            payload = json.loads(output.read_text())
            loaded = read_status_snapshot(output, workflow="regression", upstream=_UPSTREAM)
        self.assertEqual(payload["downstreams"], {})
        self.assertEqual(loaded, {})


if __name__ == "__main__":
    unittest_main()
