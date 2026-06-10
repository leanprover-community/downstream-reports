#!/usr/bin/env python3
"""
Tests for: scripts.export_status_snapshot (and the storage snapshot helpers)

Coverage scope:
    - ``storage.write_status_snapshot`` / ``storage.status_snapshot_payload``
      — the staged snapshot must be byte-compatible with what
      ``FilesystemBackend.load_all_statuses`` reads, for both workflow keys.
    - ``main`` (CLI) — argv → snapshot directory.  Integration-style:
      stages from a filesystem source backend and from the dry-run backend.

Out of scope:
    - SQL reads (``SqlBackend.load_all_statuses`` is covered by
      ``test_storage.py``); the CLI only composes backend + writer.
    - Artifact upload/download; that lives in the workflow YAML.

Why this matters
----------------
The plan job stages this snapshot once and every select leg reads prior
episode state from it instead of dialing the database — the snapshot IS the
select fan-out's view of ``downstream_status``.  A shape drift between the
writer and ``FilesystemBackend.load_all_statuses`` would silently feed the
skip heuristics empty prior state, disabling ``try_skip_already_good`` and
``try_skip_known_bad_bisect`` across the board.  The round-trip tests here
pin that contract.
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
    FilesystemBackend,
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


class WriteStatusSnapshotTests(TestCase):
    def test_round_trip_regression(self) -> None:
        """Scenario: a snapshot staged for the regression workflow is read back
        verbatim by FilesystemBackend.load_all_statuses."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_status_snapshot(root, "regression", _STATUSES, reported_at="2026-06-10T00:00:00Z")
            loaded = FilesystemBackend(root).load_all_statuses("regression", _UPSTREAM)
        self.assertEqual(loaded, _STATUSES)

    def test_round_trip_ondemand(self) -> None:
        """Scenario: the ondemand workflow key writes ondemand-current.json and
        round-trips through the same backend read."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_status_snapshot(root, "ondemand", _STATUSES, reported_at="2026-06-10T00:00:00Z")
            self.assertEqual(path, root / "status" / "ondemand-current.json")
            loaded = FilesystemBackend(root).load_all_statuses("ondemand", _UPSTREAM)
        self.assertEqual(loaded, _STATUSES)

    def test_empty_statuses_round_trip(self) -> None:
        """Scenario: zero downstreams (dry-run source) still writes a valid
        snapshot file that loads back as an empty dict."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = write_status_snapshot(root, "regression", {}, reported_at="2026-06-10T00:00:00Z")
            self.assertTrue(path.exists())
            loaded = FilesystemBackend(root).load_all_statuses("regression", _UPSTREAM)
        self.assertEqual(loaded, {})

    def test_payload_shape_is_pinned(self) -> None:
        """Scenario: the snapshot payload keeps schema_version 2 and the exact
        per-downstream field names FilesystemBackend reads."""
        payload = status_snapshot_payload(_STATUSES, "2026-06-10T00:00:00Z")
        self.assertEqual(payload["schema_version"], 2)
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


class ExportStatusSnapshotCliTests(TestCase):
    def test_cli_stages_from_filesystem_source(self) -> None:
        """Scenario: the CLI copies prior state from a source state root into a
        fresh output root that works as a --state-root for the select step."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            output = Path(tmp) / "snapshot"
            write_status_snapshot(source, "regression", _STATUSES, reported_at="2026-06-09T00:00:00Z")
            argv = [
                "export_status_snapshot.py",
                "--backend", "filesystem",
                "--state-root", str(source),
                "--workflow", "regression",
                "--upstream", _UPSTREAM,
                "--output-root", str(output),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(export_main(), 0)
            loaded = FilesystemBackend(output).load_all_statuses("regression", _UPSTREAM)
        self.assertEqual(loaded, _STATUSES)

    def test_cli_dry_run_writes_empty_snapshot(self) -> None:
        """Scenario: in dry-run mode the CLI writes a snapshot with zero
        downstreams so select legs see the same empty prior state a dry-run
        database read would have produced."""
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "snapshot"
            argv = [
                "export_status_snapshot.py",
                "--backend", "dry-run",
                "--workflow", "regression",
                "--output-root", str(output),
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(export_main(), 0)
            payload = json.loads((output / "status" / "current.json").read_text())
            loaded = FilesystemBackend(output).load_all_statuses("regression", _UPSTREAM)
        self.assertEqual(payload["downstreams"], {})
        self.assertEqual(loaded, {})


if __name__ == "__main__":
    unittest_main()
