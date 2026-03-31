#!/usr/bin/env python3
"""Focused tests for downstream cache setup in `run_downstream_regression.py`."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def load_module():
    """Load the workflow runner script as a Python module for testing."""

    script_path = Path(__file__).with_name("run_downstream_regression.py")
    spec = importlib.util.spec_from_file_location("run_downstream_regression", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = load_module()


class GitHubCacheScopeTests(unittest.TestCase):
    """Scenarios for discovering which downstreams can use `lake cache get`."""

    def test_owner_name_shorthand_is_supported(self) -> None:
        """Scenario: GitHub `owner/name` shorthand maps directly to the cache scope."""

        self.assertEqual("leanprover-community/physlib", MODULE.github_cache_scope("leanprover-community/physlib"))

    def test_github_https_url_is_supported(self) -> None:
        """Scenario: an explicit GitHub HTTPS remote maps to the same cache scope."""

        self.assertEqual(
            "leanprover-community/physlib",
            MODULE.github_cache_scope("https://github.com/leanprover-community/physlib.git"),
        )

    def test_local_paths_and_non_github_urls_are_skipped(self) -> None:
        """Scenario: local and non-GitHub repos do not attempt remote cache fetches."""

        with tempfile.TemporaryDirectory() as tmp:
            local_repo = Path(tmp) / "local-downstream"
            local_repo.mkdir()
            self.assertIsNone(MODULE.github_cache_scope(str(local_repo)))
        self.assertIsNone(MODULE.github_cache_scope("https://example.com/leanprover-community/physlib.git"))


class WarmCacheTests(unittest.TestCase):
    """Scenarios for best-effort downstream cache warmup before validation."""

    def test_cache_env_sets_only_mathlib_cache_dir(self) -> None:
        """Scenario: the workflow keeps mathlib's `.ltar` cache local without enabling Lake's artifact cache."""

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            env = MODULE.cache_env(cache_dir)
            self.assertNotIn("LAKE_ARTIFACT_CACHE", env)
            self.assertNotIn("LAKE_CACHE_DIR", env)
            self.assertEqual(str(cache_dir / "mathlib"), env["MATHLIB_CACHE_DIR"])

    def test_warm_cache_uses_downstream_toolchain_and_repo_scope(self) -> None:
        """Scenario: warmup runs `lake cache get` through `elan` with the downstream toolchain."""

        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "downstream"
            project_dir.mkdir()
            (project_dir / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
            output_dir = Path(tmp) / "artifacts"
            output_dir.mkdir()
            config = MODULE.DownstreamConfig(
                name="physlib",
                repo="leanprover-community/physlib",
                default_branch="master",
            )
            env = MODULE.cache_env(Path(tmp) / "cache")

            with patch.object(MODULE, "run") as mock_run:
                mock_run.return_value = MODULE.subprocess.CompletedProcess(
                    ["elan"], 0, stdout="cache fetched\n", stderr=""
                )
                MODULE.warm_downstream_cache(config, project_dir=project_dir, output_dir=output_dir, env=env)

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
            self.assertIn("exit_code: 0", log_text)

    def test_warm_cache_skips_non_github_repos(self) -> None:
        """Scenario: local-path downstreams record a skip instead of attempting remote fetch."""

        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "downstream"
            project_dir.mkdir()
            (project_dir / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n")
            output_dir = Path(tmp) / "artifacts"
            output_dir.mkdir()
            local_repo = Path(tmp) / "remote.git"
            local_repo.mkdir()
            config = MODULE.DownstreamConfig(
                name="sandbox-downstream",
                repo=str(local_repo),
                default_branch="master",
            )

            with patch.object(MODULE, "run") as mock_run:
                MODULE.warm_downstream_cache(
                    config,
                    project_dir=project_dir,
                    output_dir=output_dir,
                    env=MODULE.cache_env(Path(tmp) / "cache"),
                )

            mock_run.assert_not_called()
            self.assertIn("Skipped `lake cache get`", (output_dir / "downstream-cache-get.log").read_text())


class InvokeToolTests(unittest.TestCase):
    """Scenarios for choosing how the workflow invokes the validator executable."""

    def test_prebuilt_tool_binary_is_used_when_provided(self) -> None:
        """Scenario: a workflow-provided binary avoids `lake exe` rebuild checks."""

        with tempfile.TemporaryDirectory() as tmp:
            tool_root = Path(tmp) / "tool"
            tool_root.mkdir()
            project_dir = Path(tmp) / "downstream"
            project_dir.mkdir()
            output_dir = Path(tmp) / "artifacts"
            config = MODULE.DownstreamConfig(
                name="physlib",
                repo="leanprover-community/physlib",
                default_branch="master",
            )
            tool_exe = Path(tmp) / "hopscotch"
            tool_exe.write_text("")

            mock_process = unittest.mock.Mock()
            mock_process.stdout = iter([])
            mock_process.wait.return_value = 0
            mock_process.args = [str(tool_exe)]

            with patch.object(MODULE.subprocess, "Popen", return_value=mock_process) as mock_popen:
                MODULE.invoke_tool(
                    tool_root,
                    config,
                    "deadbeef00000000000000000000000000000000",
                    "cafebabe00000000000000000000000000000000",
                    project_dir,
                    output_dir,
                    {"LAKE_CACHE_DIR": str(Path(tmp) / "cache")},
                    tool_exe,
                )

            invoked_command = mock_popen.call_args.args[0]
            self.assertEqual(str(tool_exe), invoked_command[0])
            self.assertNotIn("lake", invoked_command)
            self.assertIn("--from", invoked_command)
            self.assertIn("--to", invoked_command)
            self.assertNotIn("--commits-file", invoked_command)


class CommitPlanArtifactTests(unittest.TestCase):
    """Scenarios for runner-side commit-plan logging and artifacts."""

    def test_commit_plan_artifact_keeps_full_commit_list_while_stdout_stays_brief(self) -> None:
        """Scenario: stdout shows only counts while the artifact file keeps the full ordered list."""

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifacts"
            commits = [
                MODULE.CommitDetail(sha="a" * 40, title="first title"),
                MODULE.CommitDetail(sha="b" * 40, title="second title"),
            ]

            MODULE.append_commit_plan_artifact(
                output_dir=output_dir,
                label="bisect window (oldest to newest)",
                commits=commits,
                bisect_window=True,
            )

            artifact_path = MODULE.commit_plan_artifact_path(output_dir)
            artifact_text = artifact_path.read_text()
            self.assertIn("bisect window (oldest to newest)", artifact_text)
            self.assertIn(f"- {'a' * 40} first title", artifact_text)
            self.assertIn(f"- {'b' * 40} second title", artifact_text)

            with patch("builtins.print") as mock_print:
                MODULE.print_commit_plan_summary(
                    downstream="PrimeNumberTheoremAnd",
                    label="bisect window (oldest to newest)",
                    commits=commits,
                    artifact_path=artifact_path,
                )

            mock_print.assert_called_once_with(
                "[PrimeNumberTheoremAnd] bisect window (oldest to newest): 2 commits (full list: tested-commits.txt)"
            )


class WindowSelectionArtifactTests(unittest.TestCase):
    """Scenarios for the workflow handoff between selection and probe steps."""

    def test_selection_artifact_round_trip_preserves_probe_metadata(self) -> None:
        """Scenario: the selector can persist a bisect plan that the probe step reloads unchanged."""

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifacts"
            selection = MODULE.WindowSelection(
                needs_probe=True,
                downstream="PrimeNumberTheoremAnd",
                repo="AlexKontorovich/PrimeNumberTheoremAnd",
                default_branch="master",
                dependency_name="mathlib",
                downstream_commit="downstream-head",
                upstream_ref="master",
                target_commit="bad" * 10,
                search_mode="bisect",
                tested_commits=["good" * 10, "bad" * 10],
                tested_commit_details=[
                    MODULE.CommitDetail(sha="good" * 10, title="good title"),
                    MODULE.CommitDetail(sha="bad" * 10, title="bad title"),
                ],
                commit_window_truncated=True,
                head_probe_outcome="failed",
                head_probe_failure_stage="build",
                head_probe_summary="head failed",
                selected_lower_bound_commit="good" * 10,
                decision_reason="A usable window exists.",
                next_action="Run the probe task.",
            )

            artifact_path = MODULE.selection_artifact_path(output_dir)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            MODULE.write_selection(artifact_path, selection)
            loaded = MODULE.load_selection(artifact_path)

            self.assertTrue(loaded.needs_probe)
            self.assertEqual(loaded.search_mode, "bisect")
            self.assertEqual(loaded.tested_commits, ["good" * 10, "bad" * 10])
            self.assertEqual(loaded.tested_commit_details[0].title, "good title")
            self.assertEqual(loaded.head_probe_failure_stage, "build")
            self.assertEqual(loaded.selected_lower_bound_commit, "good" * 10)
            self.assertEqual(loaded.decision_reason, "A usable window exists.")
            self.assertEqual(loaded.next_action, "Run the probe task.")

    def test_selection_summary_explains_skipped_probe(self) -> None:
        """Scenario: the selector summary explains why the probe task will not run."""

        selection = MODULE.WindowSelection(
            downstream="PrimeNumberTheoremAnd",
            upstream_ref="master",
            target_commit="bad" * 10,
            head_probe_outcome="passed",
            decision_reason="The upper endpoint passed, so there is no failing window to bisect.",
            next_action="Skip the probe task and report the passing head-only result.",
        )

        summary = MODULE.render_selection_summary(selection)

        self.assertIn("Window Selection Summary", summary)
        self.assertIn("Head probe outcome: `passed`", summary)
        self.assertIn("Decision:", summary)
        self.assertIn("there is no failing window to bisect", summary)
        self.assertIn("Skip the probe task", summary)


if __name__ == "__main__":
    unittest.main()
