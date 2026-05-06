#!/usr/bin/env python3
"""
Tests for: scripts.git_ops

Coverage scope:
    - ``latest_reachable_tag`` — find the newest semver-shaped tag in a
      commit's first-parent ancestry.  This drives
      ``last_good_release`` / ``last_good_release_commit`` enrichment in
      ``apply_result``.
    - ``resolve_tag`` — resolve a tag name to its 40-char commit SHA.
    - ``RELEASE_TAG_GLOB`` — pattern that filters out non-semver tags
      (e.g. ``master-YYYY-MM-DD`` daily branches mathlib publishes).
    - ``repo_clone_source`` — pure-Python URL resolver (no git invocation).

Out of scope:
    - ``run`` / ``git`` — thin subprocess wrappers; exercised transitively
      by every other function tested here.
    - ``clone_upstream`` / ``clone_downstream`` / ``ensure_clean_dir`` —
      they shell out over the network or to large local directories; the
      regression workflow itself is the integration test.
    - ``resolve_upstream_target`` / ``commit_title`` — exercised in the
      regression workflow against the real upstream repo.
    - ``build_commit_window`` and the bisect window helpers — covered by
      the integration of ``select_downstream_regression_window`` against
      a real cloned upstream.
    - ``pinned_from_manifest_payload`` — covered in
      ``test_check_downstream_manifests`` where the manifest-watcher
      consumes it.

Why this matters
----------------
``latest_reachable_tag`` is what users see in the public site and the
LKG snapshot ("compatible with v4.13.0").  Returning a non-semver tag
(or an unreachable tag) would advertise a release that downstream
projects cannot actually pin against.  The fixture repo is small but
deliberately includes a non-semver tag co-located with a semver tag so
the filter is exercised end-to-end.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.git_ops import (
    RELEASE_TAG_GLOB,
    latest_reachable_tag,
    repo_clone_source,
    resolve_tag,
    run,
)


def _git(repo: Path, *args: str) -> str:
    """Run a git command in *repo* and return its trimmed stdout.

    Tiny shim around ``run`` — exists only so the inline fixture-builder
    reads as a sequence of ``_git(repo, "init")`` calls rather than the
    longer ``run(["git", "init"], cwd=repo)`` form.
    """
    return run(["git", *args], cwd=repo).stdout.strip()


def _make_fixture_repo() -> tempfile.TemporaryDirectory:
    """Create a temporary git repo with a linear history and several tags.

    What state it provides
    ----------------------
    Commit graph (oldest → newest), with each commit's tag annotation::

        c0  (no tag)            ← root commit
        c1  ← v4.11.0            (semver)
        c2  ← v4.12.0,           (semver)
              master-2026-04-15  (non-semver — daily-branch shape)
        c3  (HEAD, no tag)

    Why this shape
    --------------
    * Two semver tags so ``latest_reachable_tag`` has something to pick
      *between*, exercising the "newest reachable" logic rather than
      just "the only tag".
    * A non-semver tag co-located with a semver tag at c2 so
      ``RELEASE_TAG_GLOB`` is exercised at the same commit as a positive
      match — without this, the filter could be a no-op and tests would
      still pass.
    * c0 (untagged root) lets us test the "before any tag" case without
      needing a separate fixture.
    * c3 (HEAD past all tags) is the realistic case: the downstream just
      bumped to a master commit ahead of the latest release.
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


# ----------------------------------------------------------------------
# unittest fixtures: the repo is read-only after construction, so we
# build it once per class and tear it down in tearDownClass.  Module
# scope would be marginally faster but couples teardown to interpreter
# shutdown; class scope is the conservative choice.
# ----------------------------------------------------------------------


class TestLatestReachableTag(unittest.TestCase):
    """Tests for ``latest_reachable_tag`` against the fixture repo."""

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

    def test_latest_reachable_tag_from_head_returns_newest_semver_tag(self) -> None:
        """
        From a commit past both v4.11.0 and v4.12.0, the function returns
        v4.12.0.  This is the steady-state path: a downstream sits on
        master and wants to know the most recent release tag in its
        ancestry, not the oldest.

        If "newest" were ever broken (e.g. the function returned the
        first tag yielded by ``git tag --merged`` instead of the
        last/newest), the public site would advertise an old release
        long after newer ones shipped.
        """
        # Arrange — fixture state set up in setUpClass.

        # Act
        tag = latest_reachable_tag(self._repo, self._head)

        # Assert
        self.assertEqual(
            tag,
            "v4.12.0",
            msg="HEAD is past both semver tags; v4.12.0 is the newest reachable",
        )

    def test_latest_reachable_tag_with_non_semver_tag_co_located_excludes_it(
        self,
    ) -> None:
        """
        ``master-2026-04-15`` lives on the same commit as ``v4.12.0``.  If
        ``RELEASE_TAG_GLOB`` were ever broken to a permissive ``*``, the
        function might return the daily-branch tag — which downstreams
        cannot pin against as a release.

        This test pairs with the *positive* assertion above; together
        they confirm v4.12.0 is selected *because* of the semver filter,
        not coincidentally.
        """
        # Arrange — fixture state set up in setUpClass.

        # Act
        tag = latest_reachable_tag(self._repo, self._head)

        # Assert
        self.assertNotEqual(
            tag,
            "master-2026-04-15",
            msg=(
                "Daily-branch tags must be excluded by RELEASE_TAG_GLOB; "
                "publishing one as last_good_release would be wrong"
            ),
        )

    def test_latest_reachable_tag_at_v4_11_returns_v4_11(self) -> None:
        """
        From the commit *at* v4.11.0 (c1), v4.12.0 is not yet in the
        ancestry, so v4.11.0 is the answer.  This is the case that
        matters for downstreams whose LKG sits between two releases —
        we must return the older release that *is* reachable, not the
        newer one that isn't.
        """
        # Arrange — fixture state set up in setUpClass.

        # Act
        tag = latest_reachable_tag(self._repo, self._c1)

        # Assert
        self.assertEqual(
            tag,
            "v4.11.0",
            msg="Commit at v4.11.0 cannot reach v4.12.0; older tag is correct",
        )

    def test_latest_reachable_tag_before_any_tag_returns_none(self) -> None:
        """
        From a commit older than every tag, return ``None`` — there is
        no release the downstream could honestly claim compatibility
        with.  Downstream consumers (the snapshot, the site) treat
        ``None`` as "no release info" rather than rendering an empty
        string.
        """
        # Arrange — c0 predates v4.11.0 and v4.12.0.

        # Act
        tag = latest_reachable_tag(self._repo, self._c0)

        # Assert
        self.assertIsNone(
            tag,
            msg="No tag is reachable from the root commit; result must be None",
        )


class TestResolveTag(unittest.TestCase):
    """Tests for ``resolve_tag``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = _make_fixture_repo()
        cls._repo = Path(cls._tmpdir.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def test_resolve_tag_returns_full_40_char_sha(self) -> None:
        """
        The snapshot stores the resolved commit alongside the tag name
        (``last_good_release_commit``) so the public site can render a
        permanent commit link even after the tag is moved or deleted.
        Validating both length and the hex-only character set guards
        against the function ever returning a short SHA or an error
        string.
        """
        # Arrange — fixture state set up in setUpClass.

        # Act
        sha = resolve_tag(self._repo, "v4.12.0")

        # Assert
        self.assertEqual(
            len(sha), 40, msg="Full SHAs are 40 hex chars; short SHAs are not safe to store"
        )
        self.assertTrue(
            all(ch in "0123456789abcdef" for ch in sha),
            msg=f"Resolved SHA {sha!r} contains non-hex characters",
        )

    def test_resolve_tag_matches_git_rev_list_output(self) -> None:
        """
        The function must produce the same SHA that ``git rev-list -n 1
        <tag>`` does.  Equivalence with the canonical git CLI invocation
        is the strongest contract we can assert here without coupling
        the test to internal implementation details.
        """
        # Arrange
        expected = _git(self._repo, "rev-list", "-n", "1", "v4.12.0")

        # Act
        actual = resolve_tag(self._repo, "v4.12.0")

        # Assert
        self.assertEqual(
            actual,
            expected,
            msg="resolve_tag must agree with `git rev-list -n 1 <tag>`",
        )

    def test_resolve_tag_for_unknown_tag_raises_called_process_error(self) -> None:
        """
        A typo in a tag name must surface loudly, not silently return
        ``None`` or an empty string.  The downstream caller
        (``apply_result`` enrichment) treats *missing tag* as a
        normal "no release" case via ``latest_reachable_tag``; if a
        tag is supposedly there but cannot be resolved, that is a
        repository-state bug and we want it to crash the run rather
        than corrupt ``last_good_release_commit``.
        """
        # Arrange — no tag named v9.99.99 in the fixture.

        # Act / Assert
        with self.assertRaises(
            subprocess.CalledProcessError,
            msg="Resolving a missing tag must raise rather than return a sentinel",
        ):
            resolve_tag(self._repo, "v9.99.99")


class TestReleaseTagGlob(unittest.TestCase):
    """Tests pinning the ``RELEASE_TAG_GLOB`` constant."""

    def test_release_tag_glob_is_v_prefix_pattern(self) -> None:
        """
        The glob is the contract with mathlib's tagging convention
        (``v<major>.<minor>.<patch>``).  Pinning the literal value here
        means a maintainer who changes the glob is forced to update this
        test, which forces them to think about whether downstream
        consumers (the snapshot, the issue tracker) will still recognise
        the new shape.
        """
        # Arrange / Act / Assert
        self.assertEqual(
            RELEASE_TAG_GLOB,
            "v[0-9]*",
            msg="RELEASE_TAG_GLOB is a public contract with mathlib's tag convention",
        )


# ----------------------------------------------------------------------
# repo_clone_source — pure function, no git invocation.  Tested as plain
# pytest functions with parametrize to keep the cases tabular.
# ----------------------------------------------------------------------


class TestRepoCloneSource:
    """Tests for ``repo_clone_source`` — inventory ``repo`` resolver."""

    @pytest.mark.parametrize(
        "repo,expected",
        [
            pytest.param(
                "leanprover-community/mathlib4",
                "https://github.com/leanprover-community/mathlib4.git",
                id="github_owner_name_shorthand",
            ),
            pytest.param(
                "https://github.com/foo/bar.git",
                "https://github.com/foo/bar.git",
                id="explicit_https_url_passthrough",
            ),
            pytest.param(
                "git@github.com:foo/bar.git",
                "git@github.com:foo/bar.git",
                id="ssh_url_passthrough",
            ),
        ],
    )
    def test_repo_clone_source_resolves_remote_forms(self, repo: str, expected: str) -> None:
        """
        The inventory's ``repo`` field is intentionally permissive — both
        ``owner/name`` shorthand (the common case) and explicit URLs
        (for forks, mirrors, or non-GitHub hosts).  These cases pin the
        three documented forms so a refactor that breaks any one of them
        is caught immediately.
        """
        # Arrange / Act
        result = repo_clone_source(repo)

        # Assert
        assert result == expected, (
            f"repo_clone_source({repo!r}) should produce {expected!r}, got {result!r}"
        )

    def test_repo_clone_source_with_existing_local_path_returns_resolved_path(
        self, tmp_path: Path
    ) -> None:
        """
        If the ``repo`` value points at an existing filesystem path, we
        resolve it and use it directly.  This is the local-development
        path: a developer can point the inventory at a checkout on disk
        and run the regression scripts without going over the network.

        Using ``tmp_path`` (a pytest builtin) keeps the test independent
        of the repo's real layout.
        """
        # Arrange
        local = tmp_path / "fake_repo"
        local.mkdir()

        # Act
        result = repo_clone_source(str(local))

        # Assert
        assert result == str(local.resolve()), (
            "Existing local paths must be returned in resolved form so callers "
            "can pass them straight to `git clone`"
        )


if __name__ == "__main__":
    unittest.main()
