#!/usr/bin/env python3
"""
Tests for: scripts.cache

Coverage scope:
    - ``github_cache_scope`` — resolves an inventory ``repo`` value to
      the ``owner/name`` cache scope mathlib's shared Azure cache uses,
      or ``None`` for repositories that cannot share cache.
    - ``cache_env`` — builds the subprocess environment for hopscotch /
      lake invocations: pins ``MATHLIB_CACHE_DIR`` to a deterministic
      location and strips the CI-secret denylist.
    - ``warm_downstream_cache`` — best-effort ``lake cache get`` warmup
      that runs before validation, scoped to the downstream's GitHub
      repo when one is available.

Out of scope:
    - ``mathlib`` cache writes (the warming pipeline lives in
      ``warm-mathlib-cache.yml`` and is not exercised here).
    - The ``elan`` toolchain itself; tests assert the command line that
      would be executed without actually invoking elan/lake.

Why this matters
----------------
The cache helpers are the in-process layer of the project's secret-
stripping invariant: any job that runs hopscotch (or ``lake build``)
must have no CI secrets in its subprocess environment.  The job
boundary is the *primary* guarantee; ``cache_env``'s denylist is the
secondary, in-process layer that catches accidental forwarding.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cache import cache_env, github_cache_scope, warm_downstream_cache
from scripts.models import DownstreamConfig


class GitHubCacheScopeTests(unittest.TestCase):
    """Resolve the cache scope mathlib-compatible projects use."""

    def test_owner_name_shorthand_is_supported(self) -> None:
        """``owner/name`` shorthand maps directly to itself as the cache scope.

        The shared Azure cache is keyed by GitHub ``owner/name``, so any
        downstream that reads/writes through ``lake cache get`` must use
        that exact form.
        """
        # Arrange / Act
        scope = github_cache_scope("leanprover-community/physlib")

        # Assert
        self.assertEqual("leanprover-community/physlib", scope)

    def test_github_https_url_is_supported(self) -> None:
        """An explicit GitHub HTTPS remote is normalised to ``owner/name``.

        Inventories sometimes record an explicit URL (for forks or
        mirrors); the same scope resolves regardless of which form is
        used.
        """
        # Arrange / Act
        scope = github_cache_scope("https://github.com/leanprover-community/physlib.git")

        # Assert
        self.assertEqual("leanprover-community/physlib", scope)

    def test_local_paths_and_non_github_urls_are_skipped(self) -> None:
        """Local paths and non-GitHub URLs do not attempt remote cache fetches.

        ``warm_downstream_cache`` skips the warmup when this returns
        ``None`` — a downstream pointed at a filesystem checkout (or a
        non-GitHub host) has no shared cache to read from.
        """
        # Arrange — local path
        with tempfile.TemporaryDirectory() as tmp:
            local_repo = Path(tmp) / "local-downstream"
            local_repo.mkdir()

            # Act / Assert
            self.assertIsNone(github_cache_scope(str(local_repo)))

        # Act / Assert — non-GitHub URL
        self.assertIsNone(
            github_cache_scope("https://example.com/leanprover-community/physlib.git")
        )


class CacheEnvTests(unittest.TestCase):
    """Subprocess environment construction for hopscotch / lake."""

    def test_cache_env_sets_only_mathlib_cache_dir(self) -> None:
        """``cache_env`` sets ``MATHLIB_CACHE_DIR`` and nothing Lake-cache-related.

        Lake's general artifact cache is intentionally NOT enabled —
        the workflow only opts into mathlib's specific ``.ltar`` cache
        because that is what the shared Azure cache populates.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"

            # Act
            env = cache_env(cache_dir)

            # Assert
            self.assertNotIn(
                "LAKE_ARTIFACT_CACHE",
                env,
                msg="Lake artifact cache must not be enabled by cache_env",
            )
            self.assertNotIn("LAKE_CACHE_DIR", env)
            self.assertEqual(str(cache_dir / "mathlib"), env["MATHLIB_CACHE_DIR"])

    def test_cache_env_strips_ci_secrets(self) -> None:
        """CI secrets in ``os.environ`` do not appear in the returned env.

        The denylist (``GITHUB_TOKEN``, ``POSTGRES_DSN``,
        ``ZULIP_API_KEY``, ``ZULIP_EMAIL``) is the in-process safety net
        for the secret-stripping invariant: even if a job boundary mis-
        configures ``env:`` blocks, hopscotch's child processes still
        cannot see the secrets.
        """
        # Arrange
        secret_env = {
            "GITHUB_TOKEN": "ghs_fake",
            "POSTGRES_DSN": "postgresql://user:pass@host/db",
            "ZULIP_API_KEY": "zulip_fake",
            "ZULIP_EMAIL": "bot@example.com",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, secret_env):
                # Act
                env = cache_env(Path(tmp) / "cache")

            # Assert
            for key in secret_env:
                self.assertNotIn(
                    key,
                    env,
                    msg=f"{key} must not reach validation subprocesses",
                )


class WarmDownstreamCacheTests(unittest.TestCase):
    """Best-effort ``lake cache get`` invocation before validation."""

    def test_warm_cache_uses_downstream_toolchain_and_repo_scope(self) -> None:
        """Warmup invokes ``lake cache get`` via ``elan`` with the downstream toolchain.

        The exact command line is the contract: the toolchain is read
        from the downstream's ``lean-toolchain`` file and the repo
        scope is the GitHub ``owner/name`` resolved by
        ``github_cache_scope``.  Either field changing silently would
        break cache lookups.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "downstream"
            project_dir.mkdir()
            (project_dir / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
            output_dir = Path(tmp) / "artifacts"
            output_dir.mkdir()
            config = DownstreamConfig(
                name="physlib",
                repo="leanprover-community/physlib",
                default_branch="master",
            )
            env = cache_env(Path(tmp) / "cache")

            with patch("scripts.cache.run") as mock_run:
                mock_run.return_value = subprocess.CompletedProcess(
                    ["elan"], 0, stdout="cache fetched\n", stderr=""
                )

                # Act
                warm_downstream_cache(
                    config, project_dir=project_dir, output_dir=output_dir, env=env
                )

            # Assert
            mock_run.assert_called_once_with(
                [
                    "elan",
                    "run",
                    "leanprover/lean4:v4.20.0",
                    "lake",
                    "cache",
                    "get",
                    "--repo=leanprover-community/physlib",
                ],
                cwd=project_dir,
                check=False,
                env=env,
            )
            log_text = (output_dir / "downstream-cache-get.log").read_text()
            self.assertIn(
                "exit_code: 0",
                log_text,
                msg="The exit code is recorded so post-mortems can spot a failed warmup",
            )

    def test_warm_cache_skips_non_github_repos(self) -> None:
        """Local-path downstreams record a skip note instead of fetching.

        A downstream pointed at a filesystem checkout has no shared
        Azure cache row to read.  Skipping (and logging the reason)
        rather than crashing keeps local-development setups working.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "downstream"
            project_dir.mkdir()
            (project_dir / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
            output_dir = Path(tmp) / "artifacts"
            output_dir.mkdir()
            local_repo = Path(tmp) / "remote.git"
            local_repo.mkdir()
            config = DownstreamConfig(
                name="sandbox-downstream",
                repo=str(local_repo),
                default_branch="master",
            )

            with patch("scripts.cache.run") as mock_run:
                # Act
                warm_downstream_cache(
                    config,
                    project_dir=project_dir,
                    output_dir=output_dir,
                    env=cache_env(Path(tmp) / "cache"),
                )

            # Assert
            mock_run.assert_not_called()
            self.assertIn(
                "Skipped `lake cache get`",
                (output_dir / "downstream-cache-get.log").read_text(),
                msg="Skip reason must be recorded so the workflow log explains the no-op",
            )


if __name__ == "__main__":
    unittest.main()
