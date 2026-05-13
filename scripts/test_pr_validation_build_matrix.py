#!/usr/bin/env python3
"""
Tests for: scripts.pr_validation.build_matrix

Coverage scope:
    - ``_parse_entry`` — the comment-grammar parser for one entry
      (``<name-or-slug>[@<rev>] [--merge-branch]``).
    - ``_slugify_rev`` — the rev → filesystem-safe slug used in artifact
      names and the workflow job display title.
    - ``main`` end-to-end — inventory resolution (both short name and
      ``owner/repo`` slug forms), LKG snapshot enrichment, FKB
      attachment, dedup semantics, error paths.

Out of scope:
    - The published LKG snapshot itself.  Tests point at on-disk JSON
      fixtures via ``--lkg-snapshot-url`` (``file://`` URI) so the
      production URL is never reached.
    - The downstream side of the validation (validate.py, post_results).
      Those have their own files.

Why this matters
----------------
``build_matrix.py`` is the single source of truth for downstream-name
resolution: ``mathlib-ci`` passes the user's literal token through
verbatim, and this script decides whether ``leanprover-community/FLT``
and ``FLT`` are the same matrix row.  A regression that misroutes the
two forms would either silently build the wrong downstream or report
"unknown downstream" for a perfectly valid request.  The LKG / FKB
attachment is what powers the dispatch comment's master-health
framing — missing it would make every fail look like the PR's fault
even when master is already broken.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.conftest import SHA_F
from scripts.pr_validation import build_matrix


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

# LKG = "f" * 40 (SHA_F from conftest), FKB = "b" * 40, "zero" = "0" * 40.
# We use the conftest constant where the value carries semantic meaning
# (a healthy LKG); the others stay literal because they're local fixtures.
_LKG_SNAPSHOT_JSON = {
    "schema_version": 1,
    "exported_at": "2026-05-01T00:00:00Z",
    "upstream": "leanprover-community/mathlib4",
    "source_run": {"run_id": "1", "run_url": "https://example/runs/1"},
    "downstreams": {
        "FLT": {
            "repo": "leanprover-community/FLT",
            "dependency_name": "mathlib",
            "last_known_good_commit": SHA_F,
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


class TestParseEntry:
    """`_parse_entry` covers the comment grammar.

    Each test pins one axis of the
    ``<name-or-slug>[@<rev>] [--merge-branch]`` grammar against a
    representative entry; together they form a complete table over the
    grammar's three optional tokens.
    """

    def test_bare_name_defaults_to_lkg_mode_and_no_rev(self) -> None:
        """A bare ``<name>`` defaults to LKG mode with ``rev=None``.

        LKG is the user-facing default since the rebase-onto-LKG flow
        gives a verdict independent of current master health; this test
        pins that default at the parser level.
        """
        # Arrange / Act / Assert
        assert build_matrix._parse_entry("FLT") == ("FLT", None, "lkg")

    def test_rev_suffix_attaches_to_entry(self) -> None:
        """`<name>@<rev>` captures the rev separately from the name."""
        # Arrange / Act / Assert
        assert build_matrix._parse_entry("FLT@v1.2.3") == (
            "FLT",
            "v1.2.3",
            "lkg",
        )

    def test_merge_branch_flag_flips_mode(self) -> None:
        """Trailing `--merge-branch` flips that entry to merge mode."""
        # Arrange / Act / Assert
        assert build_matrix._parse_entry("FLT --merge-branch") == (
            "FLT",
            None,
            "merge",
        )

    def test_rev_and_merge_branch_combine(self) -> None:
        """Rev + flag work together (the grammar's most expressive form)."""
        # Arrange / Act / Assert
        assert build_matrix._parse_entry("FLT@v1.2.3 --merge-branch") == (
            "FLT",
            "v1.2.3",
            "merge",
        )

    def test_unknown_flag_raises(self) -> None:
        """Any flag other than `--merge-branch` is rejected.

        Strict grammar prevents the dispatcher from silently ignoring a
        token the user almost certainly meant to be acted on (e.g. a
        misspelling of `--merge-branch`).  The error message names the
        only allowed flag so a typo is easy to fix.
        """
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="--merge-branch"):
            build_matrix._parse_entry("FLT --bogus")

    def test_empty_name_raises(self) -> None:
        """`@v1` (no bare name) is rejected."""
        # Arrange / Act / Assert
        with pytest.raises(ValueError):
            build_matrix._parse_entry("@v1")

    def test_empty_rev_after_at_raises(self) -> None:
        """`FLT@` (with an `@` and nothing after) is rejected."""
        # Arrange / Act / Assert
        with pytest.raises(ValueError):
            build_matrix._parse_entry("FLT@")


# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------


class TestSlugifyRev:
    """`_slugify_rev` maps revs to filesystem-safe slugs.

    The slug ends up in artifact names and the workflow job display
    title; anything outside ``[A-Za-z0-9._-]`` collapses to ``_`` so
    GitHub doesn't reject the names.
    """

    def test_none_yields_default(self) -> None:
        """No rev → the literal sentinel ``default``."""
        # Arrange / Act / Assert
        assert build_matrix._slugify_rev(None) == "default"
        assert build_matrix._slugify_rev("") == "default"

    def test_simple_branch_unchanged(self) -> None:
        """`main`, `v1.2.3` pass through with no rewrite."""
        # Arrange / Act / Assert
        assert build_matrix._slugify_rev("main") == "main"
        assert build_matrix._slugify_rev("v1.2.3") == "v1.2.3"

    def test_slashes_replaced(self) -> None:
        """`feature/foo` is sanitised so the slug is safe for artifact paths."""
        # Arrange / Act / Assert
        assert build_matrix._slugify_rev("feature/foo") == "feature_foo"

    def test_special_chars_collapsed(self) -> None:
        """Runs of unsafe chars collapse into a single underscore."""
        # Arrange / Act / Assert
        assert build_matrix._slugify_rev("foo bar*baz") == "foo_bar_baz"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestBuildMatrixCLI:
    """End-to-end exercise of ``build_matrix.main()``.

    Each test sets up a temp dir with the on-disk inventory + (when
    relevant) the LKG snapshot, runs main with patched argv, and reads
    the resulting matrix.json to assert on its shape.
    """

    def setup_method(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self.inventory = _write_inventory(self.tmpdir)
        self.output = self.tmpdir / "matrix.json"

    def teardown_method(self) -> None:
        self._tmp.cleanup()

    def _read_matrix(self) -> list[dict]:
        return json.loads(self.output.read_text())["include"]

    def test_bare_name_picks_lkg_and_fetches_snapshot(self) -> None:
        """A bare name defaults to LKG mode and the snapshot is fetched.

        LKG mode requires the snapshot's ``last_known_good_commit`` to
        rebase onto; ``main`` must fetch the snapshot whenever any entry
        is in LKG mode.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        include = self._read_matrix()
        assert len(include) == 1
        entry = include[0]
        assert entry["name"] == "FLT"
        assert entry["mode"] == "lkg"
        assert entry["rev"] == ""
        assert entry["rev_slug"] == "default"
        assert entry["lkg_commit"] == SHA_F

    def test_merge_branch_entry_has_no_lkg_commit_field(self) -> None:
        """A `--merge-branch` entry resolves without a `lkg_commit` field.

        Merge mode builds against the PR's would-be-merged tree directly
        and doesn't need the LKG anchor; the field's absence is the
        validate step's signal to take the merge-mode code path.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT --merge-branch",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        entry = self._read_matrix()[0]
        assert entry["mode"] == "merge"
        assert "lkg_commit" not in entry

    def test_merge_only_dispatch_tolerates_snapshot_fetch_failure(self) -> None:
        """A merge-only dispatch still succeeds when the LKG snapshot is unreachable.

        With no LKG-mode entries the snapshot is FKB-enrichment-only; we
        warn and proceed.  This keeps the workflow usable even if the
        published static-site URL temporarily 404s.
        """
        # Arrange / Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT --merge-branch",
            output=self.output,
            snapshot_url="file:///nonexistent/path/lkg.json",
        )

        # Assert
        assert rc == 0
        entry = self._read_matrix()[0]
        assert "fkb_commit" not in entry

    def test_fkb_attached_when_snapshot_records_it(self) -> None:
        """An entry with a snapshot FKB gets ``fkb_commit`` on its matrix row (both modes).

        FKB enrichment applies independent of mode — the comment
        renderer uses it for definitive master-health framing in both
        LKG and merge passes / fails.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="Toric, Toric --merge-branch",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        include = self._read_matrix()
        for entry in include:
            assert entry["fkb_commit"] == "b" * 40

    def test_fkb_absent_when_snapshot_has_no_regression(self) -> None:
        """A downstream with ``first_known_bad_commit: null`` gets no ``fkb_commit`` field.

        Master is healthy for this downstream; the comment renderer
        should not invent an FKB caveat.  Field's absence triggers the
        "master is currently known to build with X" framing.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        assert "fkb_commit" not in self._read_matrix()[0]

    def test_rev_attached_to_entry(self) -> None:
        """`FLT@v1.2.3` produces rev + slug fields on the matrix entry."""
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT@v1.2.3",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        entry = self._read_matrix()[0]
        assert entry["rev"] == "v1.2.3"
        assert entry["rev_slug"] == "v1.2.3"

    def test_mixed_request_distinct_entries(self) -> None:
        """Rev + flag combos that resolve differently get distinct matrix entries.

        ``(name, rev, mode)`` is the dedup key, so ``FLT`` and
        ``FLT@main --merge-branch`` are two rows in the same dispatch.
        Stable order matches the user's input order.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT, FLT@main --merge-branch, Toric",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        include = self._read_matrix()
        triples = [(e["name"], e["rev"], e["mode"]) for e in include]
        assert triples == [
            ("FLT", "", "lkg"),
            ("FLT", "main", "merge"),
            ("Toric", "", "lkg"),
        ]

    def test_duplicate_identical_entries_dedup(self) -> None:
        """Identical entries (same name, rev, mode) are silently collapsed."""
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT, FLT, FLT@main, FLT@main",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        names = [(e["name"], e["rev"]) for e in self._read_matrix()]
        assert names == [("FLT", ""), ("FLT", "main")]

    def test_unknown_name_returns_nonzero(self) -> None:
        """A name not in the inventory is rejected before snapshot fetch.

        The snapshot fetch is the most expensive part of ``main`` (HTTP
        round-trip).  Rejecting unknown names first keeps a typo cheap.
        """
        # Arrange / Act
        with patch.object(build_matrix, "_fetch_lkg_snapshot") as fetch_mock:
            rc = _run_main(
                inventory=self.inventory,
                names="MissingProject",
                output=self.output,
            )

        # Assert
        assert rc == 1
        fetch_mock.assert_not_called()

    def test_slug_form_resolves_to_inventory_entry(self) -> None:
        """An ``owner/repo`` slug resolves to the same canonical entry as the short name.

        The canonical name flows into all internal fields (artifacts,
        prose, dedup) while the user's literal slug survives on
        ``requested_name`` for the displayed entry label.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="leanprover-community/FLT",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        entry = self._read_matrix()[0]
        assert entry["name"] == "FLT"
        assert entry["repo"] == "leanprover-community/FLT"
        assert entry["requested_name"] == "leanprover-community/FLT"

    def test_slug_match_is_case_insensitive(self) -> None:
        """GitHub slugs are case-insensitive, so we accept any casing.

        GitHub URLs treat ``LeanProver-Community/flt`` and
        ``leanprover-community/FLT`` as the same repo; the resolver
        mirrors that semantics so users don't have to remember exact
        case.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="LeanProver-Community/flt",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        entry = self._read_matrix()[0]
        assert entry["name"] == "FLT"
        assert entry["requested_name"] == "LeanProver-Community/flt"

    def test_short_name_and_slug_collapse_into_one_row(self) -> None:
        """`FLT` and `leanprover-community/FLT` resolve to the same matrix row.

        Dedup runs on the canonical name, so the two forms can't trigger
        the same build twice; the first form the user typed wins as the
        displayed token.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT, leanprover-community/FLT",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        include = self._read_matrix()
        assert len(include) == 1
        assert include[0]["requested_name"] == "FLT"

    def test_requested_name_omitted_when_equal_to_canonical(self) -> None:
        """When the user typed the short name, ``requested_name`` mirrors ``name``.

        The display path falls back to ``downstream`` (the canonical
        name) when ``requested_name`` is unset, so we only need to
        record the slug form; the short-name case is recorded for
        symmetry but doesn't affect display.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 0
        entry = self._read_matrix()[0]
        assert entry["requested_name"] == "FLT"

    def test_unknown_slug_returns_nonzero(self) -> None:
        """A slug that matches no inventory entry is rejected."""
        # Arrange / Act
        rc = _run_main(
            inventory=self.inventory,
            names="some-org/nonexistent",
            output=self.output,
        )

        # Assert
        assert rc == 1

    def test_unknown_flag_returns_nonzero(self) -> None:
        """Any flag other than `--merge-branch` is rejected at the CLI level."""
        # Arrange / Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT --bogus",
            output=self.output,
        )

        # Assert
        assert rc == 1

    def test_missing_lkg_for_lkg_mode_returns_nonzero(self) -> None:
        """An LKG-mode entry whose snapshot has null LKG fails fast.

        The rebase-onto-LKG flow can't proceed without a real LKG SHA;
        we surface this as an error instead of silently fast-forwarding
        to ``HEAD``.
        """
        # Arrange
        snapshot = _write_snapshot(self.tmpdir)

        # Act
        rc = _run_main(
            inventory=self.inventory,
            names="newcomer",
            output=self.output,
            snapshot_url=snapshot.as_uri(),
        )

        # Assert
        assert rc == 1

    def test_missing_lkg_acceptable_in_merge_mode(self) -> None:
        """`--merge-branch` doesn't care about the LKG snapshot.

        Merge mode doesn't need a rebase anchor; users can still
        validate a freshly-onboarded downstream that has no recorded
        LKG yet.
        """
        # Arrange / Act
        rc = _run_main(
            inventory=self.inventory,
            names="newcomer --merge-branch",
            output=self.output,
        )

        # Assert
        assert rc == 0

    def test_snapshot_fetch_failure_returns_nonzero(self) -> None:
        """LKG snapshot fetch transport error surfaces a clean error.

        When *any* entry is in LKG mode, the snapshot is a hard
        requirement.  A 404 / network failure must fail the plan job
        explicitly rather than silently dispatch with empty LKG fields.
        """
        # Arrange / Act
        rc = _run_main(
            inventory=self.inventory,
            names="FLT",
            output=self.output,
            snapshot_url="file:///nonexistent/path/lkg.json",
        )

        # Assert
        assert rc == 1
