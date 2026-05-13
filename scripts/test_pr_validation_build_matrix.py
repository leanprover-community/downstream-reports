#!/usr/bin/env python3
"""Tests for scripts/pr_validation/build_matrix.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# build_matrix lives outside the `scripts.` package proper (no __init__ in
# pr_validation), so import the module by path.
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
        },
        "Toric": {
            "repo": "YaelDillies/toric",
            "dependency_name": "mathlib",
            "last_known_good_commit": "0" * 40,
            "first_known_bad_commit": "b" * 40,
        },
        "newcomer": {
            "repo": "some-org/newcomer",
            "dependency_name": "mathlib",
            "last_known_good_commit": None,
            "first_known_bad_commit": None,
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
# Entry-string parsing
# ---------------------------------------------------------------------------


class ParseEntryTests(unittest.TestCase):
    """build_matrix._parse_entry covers the comment grammar."""

    def test_bare_name_defaults_to_lkg_mode_and_no_rev(self) -> None:
        """Scenario: a bare `<name>` defaults to LKG mode, no rev."""
        self.assertEqual(
            build_matrix._parse_entry("FLT"), ("FLT", None, "lkg")
        )

    def test_rev_suffix_attaches_to_entry(self) -> None:
        """Scenario: `<name>@<rev>` captures the rev separately from the name."""
        self.assertEqual(
            build_matrix._parse_entry("FLT@v1.2.3"),
            ("FLT", "v1.2.3", "lkg"),
        )

    def test_merge_branch_flag_flips_mode(self) -> None:
        """Scenario: trailing `--merge-branch` flips that entry to merge mode."""
        self.assertEqual(
            build_matrix._parse_entry("FLT --merge-branch"),
            ("FLT", None, "merge"),
        )

    def test_rev_and_merge_branch_combine(self) -> None:
        """Scenario: rev + flag work together."""
        self.assertEqual(
            build_matrix._parse_entry("FLT@v1.2.3 --merge-branch"),
            ("FLT", "v1.2.3", "merge"),
        )

    def test_unknown_flag_raises(self) -> None:
        """Scenario: any flag other than --merge-branch is rejected."""
        with self.assertRaises(ValueError) as ctx:
            build_matrix._parse_entry("FLT --bogus")
        self.assertIn("--merge-branch", str(ctx.exception))

    def test_empty_name_raises(self) -> None:
        """Scenario: `@v1` (no bare name) is rejected."""
        with self.assertRaises(ValueError):
            build_matrix._parse_entry("@v1")

    def test_empty_rev_after_at_raises(self) -> None:
        """Scenario: `FLT@` (with an `@` and nothing after) is rejected."""
        with self.assertRaises(ValueError):
            build_matrix._parse_entry("FLT@")


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------


class SlugifyTests(unittest.TestCase):
    """_slugify_rev maps revs to filesystem-safe slugs."""

    def test_none_yields_default(self) -> None:
        """Scenario: no rev → the literal sentinel 'default'."""
        self.assertEqual(build_matrix._slugify_rev(None), "default")
        self.assertEqual(build_matrix._slugify_rev(""), "default")

    def test_simple_branch_unchanged(self) -> None:
        """Scenario: `main`, `v1.2.3` pass through with no rewrite."""
        self.assertEqual(build_matrix._slugify_rev("main"), "main")
        self.assertEqual(build_matrix._slugify_rev("v1.2.3"), "v1.2.3")

    def test_slashes_replaced(self) -> None:
        """Scenario: `feature/foo` is sanitised so the slug is safe for paths."""
        self.assertEqual(
            build_matrix._slugify_rev("feature/foo"), "feature_foo"
        )

    def test_special_chars_collapsed(self) -> None:
        """Scenario: runs of unsafe chars collapse into a single underscore."""
        self.assertEqual(
            build_matrix._slugify_rev("foo bar*baz"), "foo_bar_baz"
        )


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

    def test_bare_name_picks_lkg_and_fetches_snapshot(self) -> None:
        """Scenario: a bare name defaults to LKG mode and the snapshot is fetched."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        include = self._read_matrix()
        self.assertEqual(len(include), 1)
        entry = include[0]
        self.assertEqual(entry["name"], "FLT")
        self.assertEqual(entry["mode"], "lkg")
        self.assertEqual(entry["rev"], "")
        self.assertEqual(entry["rev_slug"], "default")
        self.assertEqual(entry["lkg_commit"], "f" * 40)

    def test_merge_branch_entry_has_no_lkg_commit_field(self) -> None:
        """Scenario: a `--merge-branch` entry resolves without a `lkg_commit` field."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT --merge-branch",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        entry = self._read_matrix()[0]
        self.assertEqual(entry["mode"], "merge")
        self.assertNotIn("lkg_commit", entry)

    def test_merge_only_dispatch_tolerates_snapshot_fetch_failure(self) -> None:
        """Scenario: a merge-only dispatch still succeeds when the LKG snapshot is unreachable."""
        rc = _run_main(
            inventory=self.inventory,
            names="FLT --merge-branch",
            output=self.output,
            snapshot_url="file:///nonexistent/path/lkg.json",
        )
        # No LKG-mode entries, so the snapshot is enrichment-only and we
        # proceed with a warning.
        self.assertEqual(rc, 0)
        entry = self._read_matrix()[0]
        self.assertNotIn("fkb_commit", entry)

    def test_fkb_attached_when_snapshot_records_it(self) -> None:
        """Scenario: an entry with a snapshot FKB gets `fkb_commit` on its matrix row (both modes)."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="Toric, Toric --merge-branch",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        include = self._read_matrix()
        # FKB enrichment applies to both modes when the snapshot records it.
        for entry in include:
            self.assertEqual(entry["fkb_commit"], "b" * 40)

    def test_fkb_absent_when_snapshot_has_no_regression(self) -> None:
        """Scenario: a downstream with `first_known_bad_commit: null` gets no `fkb_commit` field."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        self.assertNotIn("fkb_commit", self._read_matrix()[0])

    def test_rev_attached_to_entry(self) -> None:
        """Scenario: `FLT@v1.2.3` produces rev + slug fields on the matrix entry."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT@v1.2.3",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        entry = self._read_matrix()[0]
        self.assertEqual(entry["rev"], "v1.2.3")
        self.assertEqual(entry["rev_slug"], "v1.2.3")

    def test_mixed_request_distinct_entries(self) -> None:
        """Scenario: rev + flag combos that resolve differently get distinct matrix entries."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT, FLT@main --merge-branch, Toric",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        include = self._read_matrix()
        triples = [(e["name"], e["rev"], e["mode"]) for e in include]
        self.assertEqual(
            triples,
            [
                ("FLT", "", "lkg"),
                ("FLT", "main", "merge"),
                ("Toric", "", "lkg"),
            ],
        )

    def test_duplicate_identical_entries_dedup(self) -> None:
        """Scenario: identical entries (same name, rev, mode) are silently collapsed."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="FLT, FLT, FLT@main, FLT@main",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 0)
        names = [(e["name"], e["rev"]) for e in self._read_matrix()]
        self.assertEqual(names, [("FLT", ""), ("FLT", "main")])

    def test_unknown_name_returns_nonzero(self) -> None:
        """Scenario: a name not in the inventory is rejected before snapshot fetch."""
        with patch.object(build_matrix, "_fetch_lkg_snapshot") as fetch_mock:
            rc = _run_main(
                inventory=self.inventory,
                names="MissingProject",
                output=self.output,
            )
        self.assertEqual(rc, 1)
        fetch_mock.assert_not_called()

    def test_unknown_flag_returns_nonzero(self) -> None:
        """Scenario: any flag other than --merge-branch is rejected."""
        rc = _run_main(
            inventory=self.inventory,
            names="FLT --bogus",
            output=self.output,
        )
        self.assertEqual(rc, 1)

    def test_missing_lkg_for_lkg_mode_returns_nonzero(self) -> None:
        """Scenario: an LKG-mode entry whose snapshot has null LKG fails fast."""
        snapshot = _write_snapshot(self.tmpdir)
        rc = _run_main(
            inventory=self.inventory,
            names="newcomer",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )
        self.assertEqual(rc, 1)

    def test_missing_lkg_acceptable_in_merge_mode(self) -> None:
        """Scenario: `--merge-branch` doesn't care about the LKG snapshot."""
        rc = _run_main(
            inventory=self.inventory,
            names="newcomer --merge-branch",
            output=self.output,
        )
        self.assertEqual(rc, 0)

    def test_snapshot_fetch_failure_returns_nonzero(self) -> None:
        """Scenario: LKG snapshot fetch transport error surfaces a clean error."""
        rc = _run_main(
            inventory=self.inventory,
            names="FLT",
            output=self.output,
            snapshot_url="file:///nonexistent/path/lkg.json",
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
