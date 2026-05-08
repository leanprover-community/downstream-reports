#!/usr/bin/env python3
"""Tests for scripts/pr_validation/build_matrix.py."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# build_matrix.py uses no external deps and lives outside the `scripts.`
# package proper (no __init__ in pr_validation), so we import the module by
# path rather than as a regular package import.
import importlib.util

_BUILD_MATRIX_PATH = (
    Path(__file__).resolve().parent / "pr_validation" / "build_matrix.py"
)
_spec = importlib.util.spec_from_file_location(
    "pr_validation_build_matrix", _BUILD_MATRIX_PATH
)
build_matrix = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(build_matrix)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_INVENTORY_JSON = {
    "schema_version": 1,
    "downstreams": [
        {
            "name": "FLT",
            "repo": "leanprover-community/FLT",
            "default_branch": "main",
            "dependency_name": "mathlib",
            "enabled": True,
        },
        {
            "name": "Toric",
            "repo": "YaelDillies/toric",
            "default_branch": "master",
            "dependency_name": "mathlib",
            "enabled": True,
        },
        {
            "name": "newcomer",
            "repo": "some-org/newcomer",
            "default_branch": "main",
            "dependency_name": "mathlib",
            "enabled": True,
        },
    ],
}

_LKG_SNAPSHOT_JSON = {
    "schema_version": 1,
    "exported_at": "2026-05-01T00:00:00Z",
    "upstream": "leanprover-community/mathlib4",
    "source_run": {"run_id": "1", "run_url": "https://example/runs/1"},
    "downstreams": {
        "FLT": {
            "repo": "leanprover-community/FLT",
            "dependency_name": "mathlib",
            "last_known_good_commit": "f" * 40,
            "first_known_bad_commit": None,
            "last_good_release": None,
            "last_good_release_commit": None,
        },
        "Toric": {
            "repo": "YaelDillies/toric",
            "dependency_name": "mathlib",
            "last_known_good_commit": "0" * 40,
            "first_known_bad_commit": None,
            "last_good_release": None,
            "last_good_release_commit": None,
        },
        # `newcomer` is in the inventory but never had a successful run yet.
        "newcomer": {
            "repo": "some-org/newcomer",
            "dependency_name": "mathlib",
            "last_known_good_commit": None,
            "first_known_bad_commit": None,
            "last_good_release": None,
            "last_good_release_commit": None,
        },
    },
}


def _write_inventory(tmpdir: Path) -> Path:
    path = tmpdir / "downstreams.json"
    path.write_text(json.dumps(_INVENTORY_JSON))
    return path


def _write_snapshot(tmpdir: Path, payload: dict | None = None) -> Path:
    path = tmpdir / "lkg-latest.json"
    path.write_text(json.dumps(payload or _LKG_SNAPSHOT_JSON))
    return path


def _run_main(
    *,
    inventory: Path,
    names: str,
    output: Path,
    snapshot_url: str | None = None,
) -> int:
    argv = [
        "build_matrix.py",
        "--inventory",
        str(inventory),
        "--names",
        names,
        "--output",
        str(output),
    ]
    if snapshot_url is not None:
        argv.extend(["--lkg-snapshot-url", snapshot_url])
    with patch.object(sys, "argv", argv):
        return build_matrix.main()


# ---------------------------------------------------------------------------
# Token parsing
# ---------------------------------------------------------------------------


class ParseNameTokenTests(unittest.TestCase):
    """Tests for build_matrix._parse_name_token()."""

    def test_bare_name_yields_merge_mode(self) -> None:
        """Scenario: a name without an @ suffix defaults to merge mode."""
        self.assertEqual(
            build_matrix._parse_name_token("FLT"), ("FLT", "merge")
        )

    def test_lkg_suffix_yields_lkg_mode(self) -> None:
        """Scenario: a `@lkg` suffix selects LKG mode and is stripped from the name."""
        self.assertEqual(
            build_matrix._parse_name_token("FLT@lkg"), ("FLT", "lkg")
        )

    def test_unknown_suffix_raises(self) -> None:
        """Scenario: any suffix other than @lkg is rejected."""
        with self.assertRaises(ValueError) as ctx:
            build_matrix._parse_name_token("FLT@beta")
        self.assertIn("only @lkg is supported", str(ctx.exception))

    def test_empty_name_raises(self) -> None:
        """Scenario: a token with empty bare name (e.g. `@lkg`) is rejected."""
        with self.assertRaises(ValueError):
            build_matrix._parse_name_token("@lkg")


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class BuildMatrixCLITests(unittest.TestCase):
    """End-to-end exercise of build_matrix.main()."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = Path(self._tmp.name)
        self.inventory = _write_inventory(self.tmpdir)
        self.output = self.tmpdir / "matrix.json"

    def _read_matrix(self) -> list[dict]:
        return json.loads(self.output.read_text())["include"]

    def test_merge_mode_only_does_not_fetch_snapshot(self) -> None:
        """Scenario: a merge-only dispatch never touches the LKG snapshot URL."""
        with patch.object(build_matrix, "_fetch_lkg_snapshot") as fetch_mock:
            rc = _run_main(
                inventory=self.inventory,
                names="FLT, Toric",
                output=self.output,
            )
        self.assertEqual(rc, 0)
        fetch_mock.assert_not_called()
        include = self._read_matrix()
        self.assertEqual([e["name"] for e in include], ["FLT", "Toric"])
        for entry in include:
            self.assertEqual(entry["mode"], "merge")
            self.assertNotIn("lkg_commit", entry)

    def test_lkg_mode_resolves_commit_from_snapshot(self) -> None:
        """Scenario: an `@lkg` entry attaches the snapshot's last_known_good_commit."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT@lkg",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        include = self._read_matrix()
        self.assertEqual(len(include), 1)
        self.assertEqual(include[0]["mode"], "lkg")
        self.assertEqual(include[0]["lkg_commit"], "f" * 40)

    def test_mixed_mode_request_emits_both(self) -> None:
        """Scenario: `FLT@lkg, Toric` emits one LKG entry and one merge entry."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT@lkg, Toric",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        include = self._read_matrix()
        modes = {e["name"]: e["mode"] for e in include}
        self.assertEqual(modes, {"FLT": "lkg", "Toric": "merge"})

    def test_unknown_suffix_returns_nonzero(self) -> None:
        """Scenario: `FLT@beta` is rejected before any snapshot fetch."""
        with patch.object(build_matrix, "_fetch_lkg_snapshot") as fetch_mock:
            rc = _run_main(
                inventory=self.inventory,
                names="FLT@beta",
                output=self.output,
            )
        self.assertEqual(rc, 1)
        fetch_mock.assert_not_called()

    def test_unknown_name_returns_nonzero(self) -> None:
        """Scenario: a name not present in the inventory is rejected."""
        rc = _run_main(
            inventory=self.inventory,
            names="MissingProject",
            output=self.output,
        )
        self.assertEqual(rc, 1)

    def test_missing_lkg_for_known_name_returns_nonzero(self) -> None:
        """Scenario: an `@lkg` entry whose snapshot has null LKG fails fast."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="newcomer@lkg",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 1)

    def test_snapshot_fetch_failure_returns_nonzero(self) -> None:
        """Scenario: LKG snapshot fetch transport error surfaces a clean error."""
        rc = _run_main(
            inventory=self.inventory,
            names="FLT@lkg",
            output=self.output,
            snapshot_url="file:///nonexistent/path/lkg.json",
        )
        self.assertEqual(rc, 1)

    def test_duplicate_mode_request_rejected(self) -> None:
        """Scenario: requesting the same downstream twice in the same mode is rejected."""
        rc = _run_main(
            inventory=self.inventory,
            names="FLT, FLT",
            output=self.output,
        )
        self.assertEqual(rc, 1)

    def test_same_name_in_both_modes_is_allowed(self) -> None:
        """Scenario: `FLT, FLT@lkg` is accepted — these are distinct validations."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT, FLT@lkg",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        include = self._read_matrix()
        self.assertEqual([e["mode"] for e in include], ["merge", "lkg"])


if __name__ == "__main__":
    unittest.main()
