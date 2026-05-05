#!/usr/bin/env python3
"""Tests for export_lkg_snapshot.py — snapshot builder and CLI."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.export_lkg_snapshot import SCHEMA_VERSION, build_snapshot
from scripts.models import DownstreamConfig
from scripts.storage import DownstreamStatusRecord


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


def _make_backend(statuses: dict[str, DownstreamStatusRecord] | None = None) -> MagicMock:
    """Build a mock StorageBackend that returns *statuses* from load_all_statuses."""
    backend = MagicMock()
    backend.load_all_statuses.return_value = statuses or {}
    return backend


# ---------------------------------------------------------------------------
# build_snapshot() tests
# ---------------------------------------------------------------------------


class BuildSnapshotSchemaTests(unittest.TestCase):
    """Tests for the top-level fields and schema version of build_snapshot()."""

    def test_schema_version_is_constant(self) -> None:
        """Scenario: snapshot schema_version always matches SCHEMA_VERSION."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["schema_version"], SCHEMA_VERSION)
        self.assertEqual(snap["schema_version"], 1)

    def test_upstream_field_matches_argument(self) -> None:
        """Scenario: upstream field reflects the caller-supplied value."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["upstream"], _UPSTREAM)

    def test_exported_at_present_and_ends_with_z(self) -> None:
        """Scenario: exported_at is a UTC timestamp string ending in 'Z'."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        self.assertIn("exported_at", snap)
        self.assertTrue(snap["exported_at"].endswith("Z"), snap["exported_at"])

    def test_source_run_none_when_not_provided(self) -> None:
        """Scenario: source_run is null when not passed to build_snapshot."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM, source_run=None)
        self.assertIsNone(snap["source_run"])

    def test_source_run_included_when_provided(self) -> None:
        """Scenario: source_run dict is preserved verbatim in snapshot output."""
        source_run = {"run_id": "42", "run_url": "https://example.com/runs/42"}
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM, source_run=source_run)
        self.assertEqual(snap["source_run"], source_run)

    def test_downstreams_key_is_dict(self) -> None:
        """Scenario: downstreams field is a dict keyed by downstream name."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        self.assertIsInstance(snap["downstreams"], dict)


class BuildSnapshotDownstreamFilterTests(unittest.TestCase):
    """Tests that only inventory-supplied downstreams appear in the snapshot."""

    def test_all_inventory_downstreams_present(self) -> None:
        """Scenario: every downstream in the inventory appears in the snapshot."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        self.assertIn("physlib", snap["downstreams"])
        self.assertIn("alglib", snap["downstreams"])

    def test_empty_inventory_produces_empty_downstreams(self) -> None:
        """Scenario: empty inventory results in an empty downstreams dict."""
        snap = build_snapshot(_make_backend(), {}, _UPSTREAM)
        self.assertEqual(snap["downstreams"], {})

    def test_backend_called_with_correct_workflow_and_upstream(self) -> None:
        """Scenario: load_all_statuses is called with workflow=regression."""
        backend = _make_backend()
        build_snapshot(backend, _INVENTORY, _UPSTREAM)
        backend.load_all_statuses.assert_called_once_with("regression", _UPSTREAM)


class BuildSnapshotCommitFieldTests(unittest.TestCase):
    """Tests for commit field population in individual downstream entries."""

    def test_no_status_produces_null_commits(self) -> None:
        """Scenario: downstream with no stored status gets null commit fields."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        entry = snap["downstreams"]["physlib"]
        self.assertIsNone(entry["last_known_good_commit"])
        self.assertIsNone(entry["first_known_bad_commit"])

    def test_lkg_commit_reflected_from_status(self) -> None:
        """Scenario: stored LKG commit appears in snapshot entry."""
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit="abc123def456")
        }
        snap = build_snapshot(_make_backend(statuses), _INVENTORY, _UPSTREAM)
        self.assertEqual(
            snap["downstreams"]["physlib"]["last_known_good_commit"], "abc123def456"
        )

    def test_first_known_bad_included_for_active_regression(self) -> None:
        """Scenario: active regression has first_known_bad_commit set in snapshot."""
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit="good111",
                first_known_bad_commit="bad222",
            )
        }
        snap = build_snapshot(_make_backend(statuses), _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["downstreams"]["physlib"]["first_known_bad_commit"], "bad222")

    def test_downstream_with_no_lkg_has_null_first_known_bad(self) -> None:
        """Scenario: downstream with only passing state has null first_known_bad_commit."""
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit="goodabc")
        }
        snap = build_snapshot(_make_backend(statuses), _INVENTORY, _UPSTREAM)
        self.assertIsNone(snap["downstreams"]["physlib"]["first_known_bad_commit"])

    def test_status_present_for_only_one_downstream(self) -> None:
        """Scenario: downstream with status gets populated fields; other gets nulls."""
        statuses = {"physlib": DownstreamStatusRecord(last_known_good_commit="abc")}
        snap = build_snapshot(_make_backend(statuses), _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["downstreams"]["physlib"]["last_known_good_commit"], "abc")
        self.assertIsNone(snap["downstreams"]["alglib"]["last_known_good_commit"])


class BuildSnapshotReleaseFieldTests(unittest.TestCase):
    """Tests for last_good_release and last_good_release_commit in snapshot entries."""

    def test_last_good_release_null_when_no_status(self) -> None:
        """Scenario: downstream with no status gets null last_good_release fields."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        self.assertIsNone(snap["downstreams"]["physlib"]["last_good_release"])
        self.assertIsNone(snap["downstreams"]["physlib"]["last_good_release_commit"])

    def test_last_good_release_reflected_from_status(self) -> None:
        """Scenario: stored release tag and SHA appear in snapshot entry."""
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit="lkg_abc",
                last_good_release="v4.13.0",
                last_good_release_commit="sha_v4_13_0",
            )
        }
        snap = build_snapshot(_make_backend(statuses), _INVENTORY, _UPSTREAM)
        self.assertEqual(snap["downstreams"]["physlib"]["last_good_release"], "v4.13.0")
        self.assertEqual(snap["downstreams"]["physlib"]["last_good_release_commit"], "sha_v4_13_0")

    def test_last_good_release_null_when_status_has_none(self) -> None:
        """Scenario: status present but release fields None produces null in snapshot."""
        statuses = {
            "physlib": DownstreamStatusRecord(
                last_known_good_commit="lkg_abc",
                last_good_release=None,
                last_good_release_commit=None,
            )
        }
        snap = build_snapshot(_make_backend(statuses), _INVENTORY, _UPSTREAM)
        self.assertIsNone(snap["downstreams"]["physlib"]["last_good_release"])
        self.assertIsNone(snap["downstreams"]["physlib"]["last_good_release_commit"])


class BuildSnapshotInventoryEnrichmentTests(unittest.TestCase):
    """Tests that repo and dependency_name come from the inventory, not the status table."""

    def test_repo_comes_from_inventory(self) -> None:
        """Scenario: repo field in snapshot entry matches DownstreamConfig.repo."""
        snap = build_snapshot(_make_backend(), _INVENTORY, _UPSTREAM)
        self.assertEqual(
            snap["downstreams"]["physlib"]["repo"], "leanprover-community/physlib"
        )
        self.assertEqual(snap["downstreams"]["alglib"]["repo"], "some-org/alglib")

    def test_dependency_name_comes_from_inventory(self) -> None:
        """Scenario: dependency_name reflects DownstreamConfig.dependency_name."""
        custom_dep = DownstreamConfig(
            name="custom",
            repo="org/custom",
            default_branch="main",
            dependency_name="my-lib",
        )
        snap = build_snapshot(_make_backend(), {"custom": custom_dep}, _UPSTREAM)
        self.assertEqual(snap["downstreams"]["custom"]["dependency_name"], "my-lib")


# ---------------------------------------------------------------------------
# main() / CLI integration tests
# ---------------------------------------------------------------------------


class MainCliTests(unittest.TestCase):
    """Integration tests for the export_lkg_snapshot CLI entry point."""

    def _run(
        self,
        extra_argv: list[str] | None = None,
        statuses: dict[str, DownstreamStatusRecord] | None = None,
    ) -> dict:
        """Run main() with a temp inventory/output and return the parsed snapshot."""
        import scripts.export_lkg_snapshot as mod

        with tempfile.TemporaryDirectory() as tmpdir:
            inv_path = Path(tmpdir) / "downstreams.json"
            inv_path.write_text(json.dumps(_INVENTORY_JSON))
            out_path = Path(tmpdir) / "snapshot.json"

            mock_backend = _make_backend(statuses)
            argv = [
                "export_lkg_snapshot.py",
                "--backend", "dry-run",
                "--inventory", str(inv_path),
                "--output", str(out_path),
            ]
            if extra_argv:
                argv.extend(extra_argv)

            with (
                patch("scripts.export_lkg_snapshot.create_backend", return_value=mock_backend),
                patch.object(sys, "argv", argv),
            ):
                rc = mod.main()

            self.assertEqual(rc, 0)
            return json.loads(out_path.read_text())

    def test_main_returns_zero_on_success(self) -> None:
        """Scenario: successful export exits with code 0."""
        snap = self._run()
        self.assertIsNotNone(snap)

    def test_disabled_downstreams_included_in_snapshot(self) -> None:
        """Scenario: export uses include_disabled=True so disabled entries appear in snapshot."""
        snap = self._run()
        self.assertIn("disabled-project", snap["downstreams"])

    def test_enabled_downstreams_present(self) -> None:
        """Scenario: enabled inventory entries appear in the snapshot output file."""
        snap = self._run()
        self.assertIn("physlib", snap["downstreams"])
        self.assertIn("alglib", snap["downstreams"])

    def test_output_is_valid_json(self) -> None:
        """Scenario: output file is parseable JSON."""
        snap = self._run()
        self.assertIsInstance(snap, dict)

    def test_custom_upstream_reflected(self) -> None:
        """Scenario: --upstream arg is reflected in snapshot output."""
        snap = self._run(extra_argv=["--upstream", "some-org/some-upstream"])
        self.assertEqual(snap["upstream"], "some-org/some-upstream")

    def test_source_run_null_for_dry_run_backend(self) -> None:
        """Scenario: dry-run backend never triggers SQL queries; source_run is null."""
        snap = self._run()
        self.assertIsNone(snap["source_run"])

    def test_lkg_commit_in_output_from_status(self) -> None:
        """Scenario: non-null LKG status is reflected in the output JSON file."""
        statuses = {
            "physlib": DownstreamStatusRecord(last_known_good_commit="deadbeef")
        }
        snap = self._run(statuses=statuses)
        self.assertEqual(
            snap["downstreams"]["physlib"]["last_known_good_commit"], "deadbeef"
        )


if __name__ == "__main__":
    unittest.main()
