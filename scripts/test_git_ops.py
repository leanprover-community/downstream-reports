#!/usr/bin/env python3
"""Tests for git_ops helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.git_ops import (
    RELEASE_TAG_GLOB,
    build_commit_window,
    git_url_from_manifest,
    is_strict_ancestor,
    latest_reachable_tag,
    parent_commit,
    pinned_commit_from_manifest,
    repo_clone_source,
    resolve_search_base_commit,
    resolve_tag,
    run,
    should_run_boundary_search,
)


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo).stdout.strip()


def _make_fixture_repo() -> tempfile.TemporaryDirectory:
    """Create a temporary git repo with a linear main branch and an orphan branch.

    Commit graph (oldest → newest, on `main`):
        c0  (no tag)
        c1  ← v4.11.0
        c2  ← v4.12.0, master-2026-04-15  (non-semver tag on same commit)
        c3  (HEAD on main)

    Plus a separate `stranded` orphan branch with one commit that shares no
    history with main, used to verify is_strict_ancestor returns False for
    unrelated commits.
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

    _git(repo, "checkout", "--orphan", "stranded")
    (repo / "file.txt").write_text("stranded")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "stranded")
    _git(repo, "checkout", "main")

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
        cls._c0 = _git(cls._repo, "rev-list", "--max-parents=0", "main")

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


class IsStrictAncestorTests(unittest.TestCase):
    """Tests for is_strict_ancestor() — load-bearing for try_skip_known_bad_bisect."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = _make_fixture_repo()
        cls._repo = Path(cls._tmpdir.name)
        cls._c0 = _git(cls._repo, "rev-list", "--max-parents=0", "main")
        cls._c1 = _git(cls._repo, "rev-parse", "v4.11.0^{}")
        cls._c2 = _git(cls._repo, "rev-parse", "v4.12.0^{}")
        cls._head = _git(cls._repo, "rev-parse", "main")
        cls._stranded = _git(cls._repo, "rev-parse", "stranded")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_older_commit_is_strict_ancestor_of_newer(self) -> None:
        """Scenario: c1 is reachable from HEAD via parent links → True."""
        self.assertTrue(is_strict_ancestor(self._repo, self._c1, self._head))

    def test_root_is_strict_ancestor_of_head(self) -> None:
        """Scenario: the root commit is an ancestor of every later commit."""
        self.assertTrue(is_strict_ancestor(self._repo, self._c0, self._head))

    def test_same_commit_is_not_strict_ancestor(self) -> None:
        """Scenario: a commit is not its own *strict* ancestor."""
        self.assertFalse(is_strict_ancestor(self._repo, self._head, self._head))

    def test_descendant_is_not_ancestor_of_older(self) -> None:
        """Scenario: HEAD is newer than c1, so HEAD is not an ancestor of c1."""
        self.assertFalse(is_strict_ancestor(self._repo, self._head, self._c1))

    def test_unrelated_branch_is_not_ancestor(self) -> None:
        """Scenario: orphan-branch commit shares no history with main → False."""
        self.assertFalse(is_strict_ancestor(self._repo, self._stranded, self._head))


class ParentCommitTests(unittest.TestCase):
    """Tests for parent_commit() — derives the exclusive --from for hopscotch."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = _make_fixture_repo()
        cls._repo = Path(cls._tmpdir.name)
        cls._c1 = _git(cls._repo, "rev-parse", "v4.11.0^{}")
        cls._c2 = _git(cls._repo, "rev-parse", "v4.12.0^{}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_parent_of_c2_is_c1(self) -> None:
        """Scenario: parent_commit(c2) returns c1's SHA (linear history)."""
        self.assertEqual(parent_commit(self._repo, self._c2), self._c1)


class BuildCommitWindowTests(unittest.TestCase):
    """Tests for build_commit_window() — shapes the range handed to hopscotch."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = _make_fixture_repo()
        cls._repo = Path(cls._tmpdir.name)
        cls._c0 = _git(cls._repo, "rev-list", "--max-parents=0", "main")
        cls._c1 = _git(cls._repo, "rev-parse", "v4.11.0^{}")
        cls._c2 = _git(cls._repo, "rev-parse", "v4.12.0^{}")
        cls._c3 = _git(cls._repo, "rev-parse", "main")
        cls._stranded = _git(cls._repo, "rev-parse", "stranded")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_no_base_returns_target_only(self) -> None:
        """Scenario: base_commit=None → window is just [target], not truncated."""
        window, truncated = build_commit_window(self._repo, self._c3, None, max_commits=10)
        self.assertEqual(window, [self._c3])
        self.assertFalse(truncated)

    def test_base_equals_target_returns_target_only(self) -> None:
        """Scenario: base == target → no commits between them, window is [target]."""
        window, truncated = build_commit_window(self._repo, self._c3, self._c3, max_commits=10)
        self.assertEqual(window, [self._c3])
        self.assertFalse(truncated)

    def test_base_not_ancestor_returns_target_only(self) -> None:
        """Scenario: base on unrelated branch → falls back to target-only."""
        window, truncated = build_commit_window(
            self._repo, self._c3, self._stranded, max_commits=10
        )
        self.assertEqual(window, [self._c3])
        self.assertFalse(truncated)

    def test_full_window_within_limit(self) -> None:
        """Scenario: c0→c3 has 3 intermediate commits; max=10 → full chronological window."""
        window, truncated = build_commit_window(
            self._repo, self._c3, self._c0, max_commits=10
        )
        self.assertEqual(window, [self._c1, self._c2, self._c3])
        self.assertFalse(truncated)

    def test_truncation_keeps_target_as_last(self) -> None:
        """Scenario: window exceeds max — output is commits[:max-1] + [target] and flagged truncated."""
        window, truncated = build_commit_window(
            self._repo, self._c3, self._c0, max_commits=2
        )
        self.assertTrue(truncated)
        self.assertEqual(len(window), 2)
        self.assertEqual(window[-1], self._c3)
        self.assertEqual(window[0], self._c1)


class ShouldRunBoundarySearchTests(unittest.TestCase):
    """Tests for should_run_boundary_search()."""

    def test_failing_head_with_multi_commit_window_runs_search(self) -> None:
        """Scenario: HEAD fails (exit 1) and there's a range to bisect → True."""
        self.assertTrue(should_run_boundary_search(1, ["a", "b"]))

    def test_passing_head_skips_search(self) -> None:
        """Scenario: HEAD passes (exit 0) → no need to bisect."""
        self.assertFalse(should_run_boundary_search(0, ["a", "b"]))

    def test_single_commit_window_skips_search(self) -> None:
        """Scenario: only one commit available → nothing to bisect."""
        self.assertFalse(should_run_boundary_search(1, ["a"]))


class RepoCloneSourceTests(unittest.TestCase):
    """Tests for repo_clone_source() — owner/name vs URL vs local path."""

    def test_owner_slash_name_becomes_github_https_url(self) -> None:
        """Scenario: 'owner/name' → 'https://github.com/owner/name.git'."""
        self.assertEqual(
            repo_clone_source("leanprover-community/physlib"),
            "https://github.com/leanprover-community/physlib.git",
        )

    def test_explicit_https_url_is_returned_unchanged(self) -> None:
        """Scenario: 'https://...' URL is passed through."""
        url = "https://example.com/foo/bar.git"
        self.assertEqual(repo_clone_source(url), url)

    def test_ssh_url_is_returned_unchanged(self) -> None:
        """Scenario: 'git@host:owner/repo' is passed through."""
        url = "git@github.com:owner/repo.git"
        self.assertEqual(repo_clone_source(url), url)

    def test_existing_local_path_is_resolved_absolute(self) -> None:
        """Scenario: a local path that exists is resolved to its absolute form."""
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "fakerepo"
            local.mkdir()
            resolved = repo_clone_source(str(local))
            self.assertEqual(resolved, str(local.resolve()))


class PinnedCommitFromManifestTests(unittest.TestCase):
    """Tests for pinned_commit_from_manifest() — Lake lock-file parsing."""

    def _write_manifest(self, project_dir: Path, payload: object) -> None:
        (project_dir / "lake-manifest.json").write_text(json.dumps(payload))

    def test_returns_none_when_manifest_missing(self) -> None:
        """Scenario: no lake-manifest.json present → None."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pinned_commit_from_manifest(Path(tmp), "mathlib"))

    def test_returns_none_on_malformed_json(self) -> None:
        """Scenario: file exists but is not valid JSON → None (no exception)."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lake-manifest.json").write_text("{not json")
            self.assertIsNone(pinned_commit_from_manifest(Path(tmp), "mathlib"))

    def test_returns_rev_for_matching_git_dependency(self) -> None:
        """Scenario: dependency listed with type=git and rev set → rev is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_manifest(
                Path(tmp),
                {
                    "packages": [
                        {"name": "mathlib", "type": "git", "rev": "deadbeef" * 5},
                        {"name": "std", "type": "git", "rev": "cafebabe" * 5},
                    ]
                },
            )
            self.assertEqual(
                pinned_commit_from_manifest(Path(tmp), "mathlib"), "deadbeef" * 5
            )

    def test_returns_none_when_dependency_absent(self) -> None:
        """Scenario: requested dependency not in manifest → None."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_manifest(Path(tmp), {"packages": [{"name": "std", "type": "git", "rev": "x"}]})
            self.assertIsNone(pinned_commit_from_manifest(Path(tmp), "mathlib"))

    def test_returns_none_for_non_git_dependency(self) -> None:
        """Scenario: dependency exists but type != 'git' (e.g. 'path') → None."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_manifest(
                Path(tmp),
                {"packages": [{"name": "mathlib", "type": "path", "dir": "../mathlib"}]},
            )
            self.assertIsNone(pinned_commit_from_manifest(Path(tmp), "mathlib"))

    def test_returns_none_when_rev_field_empty(self) -> None:
        """Scenario: dependency listed but rev is empty/missing → None."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_manifest(
                Path(tmp), {"packages": [{"name": "mathlib", "type": "git", "rev": ""}]}
            )
            self.assertIsNone(pinned_commit_from_manifest(Path(tmp), "mathlib"))


class GitUrlFromManifestTests(unittest.TestCase):
    """Tests for git_url_from_manifest()."""

    def test_returns_none_when_manifest_missing(self) -> None:
        """Scenario: no lake-manifest.json present → None."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(git_url_from_manifest(Path(tmp), "mathlib"))

    def test_returns_url_for_matching_git_dependency(self) -> None:
        """Scenario: dependency present with url field → URL is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "mathlib",
                                "type": "git",
                                "rev": "abc",
                                "url": "https://github.com/leanprover-community/mathlib4",
                            }
                        ]
                    }
                )
            )
            self.assertEqual(
                git_url_from_manifest(Path(tmp), "mathlib"),
                "https://github.com/leanprover-community/mathlib4",
            )

    def test_returns_none_for_non_git_dependency(self) -> None:
        """Scenario: dependency exists but type != 'git' → None."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "lake-manifest.json").write_text(
                json.dumps(
                    {"packages": [{"name": "mathlib", "type": "path", "dir": "../mathlib"}]}
                )
            )
            self.assertIsNone(git_url_from_manifest(Path(tmp), "mathlib"))


class ResolveSearchBaseCommitTests(unittest.TestCase):
    """Tests for resolve_search_base_commit() — manifest pin takes precedence over LKG."""

    def _write_manifest(self, project_dir: Path, rev: str | None) -> None:
        if rev is None:
            return
        (project_dir / "lake-manifest.json").write_text(
            json.dumps({"packages": [{"name": "mathlib", "type": "git", "rev": rev}]})
        )

    def test_manifest_pin_takes_precedence(self) -> None:
        """Scenario: manifest has a pin → returns it even when LKG is also set."""
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as up:
            self._write_manifest(Path(proj), "manifest_sha")
            result = resolve_search_base_commit(
                project_dir=Path(proj),
                dependency_name="mathlib",
                upstream_dir=Path(up),
                last_known_good="lkg_sha",
            )
            self.assertEqual(result, "manifest_sha")

    def test_falls_back_to_lkg_when_no_manifest(self) -> None:
        """Scenario: no manifest → returns last_known_good."""
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as up:
            result = resolve_search_base_commit(
                project_dir=Path(proj),
                dependency_name="mathlib",
                upstream_dir=Path(up),
                last_known_good="lkg_sha",
            )
            self.assertEqual(result, "lkg_sha")

    def test_returns_none_when_neither_source_available(self) -> None:
        """Scenario: no manifest pin and no LKG → None."""
        with tempfile.TemporaryDirectory() as proj, tempfile.TemporaryDirectory() as up:
            result = resolve_search_base_commit(
                project_dir=Path(proj),
                dependency_name="mathlib",
                upstream_dir=Path(up),
                last_known_good=None,
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
