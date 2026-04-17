#!/usr/bin/env python3
"""Tests for git_ops helpers — latest_reachable_tag, resolve_tag, RELEASE_TAG_GLOB."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.git_ops import RELEASE_TAG_GLOB, latest_reachable_tag, resolve_tag, run


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).stdout.strip()


def _make_fixture_repo() -> tempfile.TemporaryDirectory:
    """Create a temporary git repo with a linear history and several tags.

    Commit graph (oldest → newest):
        c0  (no tag)
        c1  ← v4.11.0
        c2  ← v4.12.0, master-2026-04-15  (non-semver tag on same commit)
        c3  (HEAD, no tag)
    """
    tmpdir = tempfile.TemporaryDirectory()
    repo = Path(tmpdir.name)
    run(["git", "init", "-b", "main", str(repo)])
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    for msg in ("c0", "c1", "c2", "c3"):
        (repo / "file.txt").write_text(msg)
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-m", msg)
        if msg == "c1":
            _git(repo, "tag", "v4.11.0")
        elif msg == "c2":
            _git(repo, "tag", "v4.12.0")
            _git(repo, "tag", "master-2026-04-15")

    return tmpdir


class LatestReachableTagTests(unittest.TestCase):
    """Tests for latest_reachable_tag()."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = _make_fixture_repo()
        cls._repo = Path(cls._tmpdir.name)
        cls._head = _git(cls._repo, "rev-parse", "HEAD")
        cls._c2 = _git(cls._repo, "rev-parse", "v4.12.0^{}")
        cls._c1 = _git(cls._repo, "rev-parse", "v4.11.0^{}")
        cls._c0 = _git(cls._repo, "rev-list", "--max-parents=0", "HEAD")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_returns_latest_semver_tag_from_head(self) -> None:
        """Scenario: HEAD is past both semver tags — v4.12.0 is returned."""
        tag = latest_reachable_tag(self._repo, self._head)
        self.assertEqual(tag, "v4.12.0")

    def test_semver_pattern_excludes_daily_tags(self) -> None:
        """Scenario: RELEASE_TAG_GLOB excludes master-YYYY-MM-DD style tags."""
        tag = latest_reachable_tag(self._repo, self._head)
        self.assertNotEqual(tag, "master-2026-04-15")

    def test_returns_correct_tag_from_c1(self) -> None:
        """Scenario: commit at v4.11.0 returns v4.11.0 (v4.12.0 not yet reachable)."""
        tag = latest_reachable_tag(self._repo, self._c1)
        self.assertEqual(tag, "v4.11.0")

    def test_returns_none_before_any_tag(self) -> None:
        """Scenario: commit before any tag returns None."""
        tag = latest_reachable_tag(self._repo, self._c0)
        self.assertIsNone(tag)


class ResolveTagTests(unittest.TestCase):
    """Tests for resolve_tag()."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = _make_fixture_repo()
        cls._repo = Path(cls._tmpdir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_resolve_tag_returns_commit_sha(self) -> None:
        """Scenario: resolve_tag returns a full 40-char SHA for an existing tag."""
        sha = resolve_tag(self._repo, "v4.12.0")
        self.assertEqual(len(sha), 40)
        self.assertTrue(sha.isalnum())

    def test_resolve_tag_matches_rev_parse(self) -> None:
        """Scenario: resolve_tag result matches git rev-list -n 1 output."""
        expected = _git(self._repo, "rev-list", "-n", "1", "v4.12.0")
        self.assertEqual(resolve_tag(self._repo, "v4.12.0"), expected)


if __name__ == "__main__":
    unittest.main()
