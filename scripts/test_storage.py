#!/usr/bin/env python3
"""Tests for the storage module: backend factory, serialization, filesystem backend."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.storage import (
    DownstreamStatusRecord,
    FilesystemBackend,
    RunResultRecord,
    create_backend,
    result_to_row,
)


class ResultToRowTests(unittest.TestCase):
    """Test that result_to_row() faithfully serializes a RunResultRecord."""

    def test_all_fields_are_present(self) -> None:
        record = RunResultRecord(
            upstream="leanprover-community/mathlib4",
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
        row = result_to_row(record)
        self.assertEqual(row["downstream"], "TestDownstream")
        self.assertEqual(row["outcome"], "passed")
        self.assertEqual(row["pinned_commit"], "pin_abc")
        self.assertIsNone(row["error"])
        self.assertFalse(row["commit_window_truncated"])
        # Verify all RunResultRecord fields are present
        import dataclasses
        for field in dataclasses.fields(record):
            self.assertIn(field.name, row, f"Missing field: {field.name}")


class CreateBackendTests(unittest.TestCase):
    """Test the create_backend() factory function."""

    def test_filesystem_backend_with_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = create_backend("filesystem", state_root=Path(tmp))
            self.assertIsInstance(backend, FilesystemBackend)

    def test_filesystem_backend_requires_state_root(self) -> None:
        with self.assertRaises(SystemExit):
            create_backend("filesystem")

    def test_sql_backend_requires_dsn(self) -> None:
        import os
        # Ensure POSTGRES_DSN is not set for this test
        old = os.environ.pop("POSTGRES_DSN", None)
        try:
            with self.assertRaises(SystemExit):
                create_backend("sql")
        finally:
            if old is not None:
                os.environ["POSTGRES_DSN"] = old


class FilesystemBackendTests(unittest.TestCase):
    """Test the filesystem storage backend."""

    def test_load_all_statuses_returns_empty_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            result = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertEqual(result, {})

    def test_save_run_and_load_statuses_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            record = RunResultRecord(
                upstream="leanprover-community/mathlib4",
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
            backend.save_run(
                run_id="run_123",
                workflow="regression",
                upstream="leanprover-community/mathlib4",
                upstream_ref="master",
                run_url="https://example.com/run/123",
                created_at="2026-04-01T00:00:00Z",
                results=[record],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertIn("TestDownstream", loaded)
            self.assertEqual(loaded["TestDownstream"].last_known_good_commit, "target_abc")
            self.assertEqual(loaded["TestDownstream"].pinned_commit, "pin_abc")

    def test_downstream_commit_round_trip(self) -> None:
        """downstream_commit is persisted in the status JSON and reloaded."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            statuses = {
                "TestDownstream": DownstreamStatusRecord(
                    last_known_good_commit="target_abc",
                    downstream_commit="ds_commit_abc",
                ),
            }
            backend.save_run(
                run_id="run_456",
                workflow="regression",
                upstream="leanprover-community/mathlib4",
                upstream_ref="master",
                run_url="https://example.com/run/456",
                created_at="2026-04-02T00:00:00Z",
                results=[],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertEqual(loaded["TestDownstream"].downstream_commit, "ds_commit_abc")

    def test_downstream_commit_defaults_to_none(self) -> None:
        """Existing status files without downstream_commit load as None."""
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp) / "status"
            status_dir.mkdir()
            (status_dir / "current.json").write_text(json.dumps({
                "schema_version": 2,
                "reported_at": "2026-04-01T00:00:00Z",
                "downstreams": {
                    "OldDownstream": {
                        "last_known_good_commit": "abc",
                        "first_known_bad_commit": None,
                        "pinned_commit": None,
                    },
                },
            }))
            backend = FilesystemBackend(Path(tmp))
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertIsNone(loaded["OldDownstream"].downstream_commit)

    def test_bumping_seen_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            self.assertEqual(backend.load_bumping_seen(), {})
            backend.save_bumping_seen({"TestDownstream": "abc123"})
            loaded = backend.load_bumping_seen()
            self.assertEqual(loaded, {"TestDownstream": "abc123"})
            # Update with a new value
            backend.save_bumping_seen({"TestDownstream": "def456"})
            loaded = backend.load_bumping_seen()
            self.assertEqual(loaded, {"TestDownstream": "def456"})


if __name__ == "__main__":
    unittest.main()
