#!/usr/bin/env python3
"""
Tests for: scripts.cache, scripts.validation, scripts.select_downstream_regression_window,
           scripts.probe_downstream_regression_window

This file pre-dates the select/probe split — it was written when the
regression workflow was a single script, and now exercises pieces of
four production modules.  The file name is preserved (rather than
splitting into ``test_cache.py`` / ``test_validation.py`` / etc.)
because the existing CI invocations and documentation reference it,
and the tests are organised into clearly-scoped classes that read
naturally as a single suite.

# NOTE: a future refactor could split this file into one test module per
# production module.  Left consolidated for now — the class boundaries
# below are the de-facto split:
#   GitHubCacheScopeTests / WarmCacheTests        → cache.py
#   InvokeToolTests / CommitPlanArtifactTests /
#     WindowSelectionArtifactTests /
#     ClassifyExitCodeTests / BuildSkipResultTests → validation.py
#   TrySkipAlreadyGoodTests                       → select_downstream_regression_window.py
#   TrySkipKnownBadBisectTests / TryCulpritProbeTests → probe_downstream_regression_window.py
#   SkipOptimisationFlagsTests                    → DownstreamConfig + both parsers

Coverage scope:
    - Cache scope resolution (``github_cache_scope``) and environment
      stripping (``cache_env``) — the defence-in-depth that ensures
      hopscotch subprocesses on the self-hosted runner cannot see CI
      secrets.
    - ``warm_downstream_cache`` — the lake-cache warmup invoked before
      hopscotch.
    - Tool invocation (``invoke_tool``) — prefers a prebuilt binary,
      falls back to ``lake exe``.
    - Selection / commit-plan / result artifacts — round-trip
      serialisation.
    - Skip heuristics — ``try_skip_already_good`` (select side) and
      ``try_skip_known_bad_bisect`` + ``run_culprit_probe`` (probe
      side).  These are both opt-in optimisations; their CLI flags
      and inventory flags are pinned in ``SkipOptimisationFlagsTests``.

Out of scope:
    - The hopscotch tool itself; tests mock ``run_validation_attempt``
      where it would otherwise shell out.
    - The cache push to Azure Blob Storage; ``warm_downstream_cache``
      is a ``lake exe cache get`` (read), not a put.
    - SQL backend interactions; the skip heuristics work entirely from
      a ``DownstreamStatusRecord`` constructed in-memory by the select
      step (no DB access from probe).

Why this matters
----------------
The skip heuristics gate the most expensive step in the regression
workflow — the bisect.  A wrong skip decision is a silent failure: the
report would persist a stale culprit attribution rather than running a
fresh bisect, and the public site would advertise a wrong FKB.  The
``downstream_commit`` guard is the contract that makes the heuristics
safe; ``TrySkipKnownBadBisectTests.test_returns_none_when_downstream_changed``
is the executable form of that guard.

The cache scope and ``cache_env`` tests are the defence-in-depth layer
for the project's secret-stripping invariant: any job that runs
hopscotch (or ``lake build``) must have no secrets in its ``env:``
blocks.  The job boundary is the *primary* guarantee; ``cache_env``'s
denylist in ``scripts/cache.py`` is the secondary, in-process layer.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import unittest.mock
from unittest.mock import patch, Mock

# Ensure the repo root is on sys.path so `scripts.*` imports work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cache import cache_env, github_cache_scope, warm_downstream_cache
from scripts.models import CommitDetail, DownstreamConfig, WindowSelection, load_inventory
from scripts.validation import (
    append_commit_plan_artifact,
    build_skip_result,
    classify_exit_code,
    commit_plan_artifact_path,
    invoke_tool,
    load_selection,
    print_commit_plan_summary,
    render_selection_summary,
    selection_artifact_path,
    write_selection,
)
from scripts.models import Outcome
from scripts.storage import DownstreamStatusRecord
from scripts.select_downstream_regression_window import (
    build_parser as select_build_parser,
    try_skip_already_good,
)
from scripts.probe_downstream_regression_window import (
    build_parser as probe_build_parser,
    run_culprit_probe,
    try_skip_known_bad_bisect,
)


class GitHubCacheScopeTests(unittest.TestCase):
    """Scenarios for discovering which downstreams can use `lake cache get`."""

    def test_owner_name_shorthand_is_supported(self) -> None:
        """Scenario: GitHub `owner/name` shorthand maps directly to the cache scope."""

        self.assertEqual("leanprover-community/physlib", github_cache_scope("leanprover-community/physlib"))

    def test_github_https_url_is_supported(self) -> None:
        """Scenario: an explicit GitHub HTTPS remote maps to the same cache scope."""

        self.assertEqual(
            "leanprover-community/physlib",
            github_cache_scope("https://github.com/leanprover-community/physlib.git"),
        )

    def test_local_paths_and_non_github_urls_are_skipped(self) -> None:
        """Scenario: local and non-GitHub repos do not attempt remote cache fetches."""

        with tempfile.TemporaryDirectory() as tmp:
            local_repo = Path(tmp) / "local-downstream"
            local_repo.mkdir()
            self.assertIsNone(github_cache_scope(str(local_repo)))
        self.assertIsNone(github_cache_scope("https://example.com/leanprover-community/physlib.git"))


class WarmCacheTests(unittest.TestCase):
    """Scenarios for best-effort downstream cache warmup before validation."""

    def test_cache_env_sets_only_mathlib_cache_dir(self) -> None:
        """Scenario: the workflow keeps mathlib's `.ltar` cache local without enabling Lake's artifact cache."""

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            env = cache_env(cache_dir)
            self.assertNotIn("LAKE_ARTIFACT_CACHE", env)
            self.assertNotIn("LAKE_CACHE_DIR", env)
            self.assertEqual(str(cache_dir / "mathlib"), env["MATHLIB_CACHE_DIR"])

    def test_cache_env_strips_ci_secrets(self) -> None:
        """Scenario: CI secrets are never forwarded to hopscotch or lake build subprocesses."""

        import os
        with tempfile.TemporaryDirectory() as tmp:
            secret_env = {
                "GITHUB_TOKEN": "ghs_fake",
                "POSTGRES_DSN": "postgresql://user:pass@host/db",
                "ZULIP_API_KEY": "zulip_fake",
                "ZULIP_EMAIL": "bot@example.com",
            }
            with unittest.mock.patch.dict(os.environ, secret_env):
                env = cache_env(Path(tmp) / "cache")
            for key in secret_env:
                self.assertNotIn(key, env, f"{key} must not reach subprocesses")

    def test_warm_cache_uses_downstream_toolchain_and_repo_scope(self) -> None:
        """Scenario: warmup runs `lake cache get` through `elan` with the downstream toolchain."""

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
                warm_downstream_cache(config, project_dir=project_dir, output_dir=output_dir, env=env)

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
            config = DownstreamConfig(
                name="sandbox-downstream",
                repo=str(local_repo),
                default_branch="master",
            )

            with patch("scripts.cache.run") as mock_run:
                warm_downstream_cache(
                    config,
                    project_dir=project_dir,
                    output_dir=output_dir,
                    env=cache_env(Path(tmp) / "cache"),
                )

            mock_run.assert_not_called()
            self.assertIn("Skipped `lake cache get`", (output_dir / "downstream-cache-get.log").read_text())


class InvokeToolTests(unittest.TestCase):
    """Scenarios for choosing how the workflow invokes the validator executable."""

    def test_prebuilt_tool_binary_is_used_when_provided(self) -> None:
        """Scenario: a workflow-provided binary avoids `lake exe` rebuild checks."""

        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "downstream"
            project_dir.mkdir()
            output_dir = Path(tmp) / "artifacts"
            config = DownstreamConfig(
                name="physlib",
                repo="leanprover-community/physlib",
                default_branch="master",
            )
            tool_exe = Path(tmp) / "hopscotch"
            tool_exe.write_text("")

            mock_process = Mock()
            mock_process.stdout = iter([])
            mock_process.wait.return_value = 0
            mock_process.args = [str(tool_exe)]

            with patch("scripts.validation.subprocess.Popen", return_value=mock_process) as mock_popen:
                invoke_tool(
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
                CommitDetail(sha="a" * 40, title="first title"),
                CommitDetail(sha="b" * 40, title="second title"),
            ]

            append_commit_plan_artifact(
                output_dir=output_dir,
                label="bisect window (oldest to newest)",
                commits=commits,
                bisect_window=True,
            )

            artifact_path = commit_plan_artifact_path(output_dir)
            artifact_text = artifact_path.read_text()
            self.assertIn("bisect window (oldest to newest)", artifact_text)
            self.assertIn(f"- {'a' * 40} first title", artifact_text)
            self.assertIn(f"- {'b' * 40} second title", artifact_text)

            with patch("builtins.print") as mock_print:
                print_commit_plan_summary(
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
            selection = WindowSelection(
                has_bisect_window=True,
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
                    CommitDetail(sha="good" * 10, title="good title"),
                    CommitDetail(sha="bad" * 10, title="bad title"),
                ],
                commit_window_truncated=True,
                head_probe_outcome="failed",
                head_probe_failure_stage="build",
                head_probe_summary="head failed",
                selected_lower_bound_commit="good" * 10,
                decision_reason="A usable window exists.",
                next_action="Run the probe task.",
            )

            artifact_path = selection_artifact_path(output_dir)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            write_selection(artifact_path, selection)
            loaded = load_selection(artifact_path)

            self.assertTrue(loaded.has_bisect_window)
            self.assertEqual(loaded.search_mode, "bisect")
            self.assertEqual(loaded.tested_commits, ["good" * 10, "bad" * 10])
            self.assertEqual(loaded.tested_commit_details[0].title, "good title")
            self.assertEqual(loaded.head_probe_failure_stage, "build")
            self.assertEqual(loaded.selected_lower_bound_commit, "good" * 10)
            self.assertEqual(loaded.decision_reason, "A usable window exists.")
            self.assertEqual(loaded.next_action, "Run the probe task.")

    def test_selection_round_trip_preserves_previous_episode_state(self) -> None:
        """Scenario: prior episode state (LKG, FKB, downstream_commit) survives serialisation."""

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifacts"
            selection = WindowSelection(
                downstream="TestProject",
                repo="owner/TestProject",
                default_branch="main",
                upstream_ref="master",
                target_commit="t" * 40,
                previous_first_known_bad_commit="f" * 40,
                previous_downstream_commit="d" * 40,
                previous_last_known_good_commit="g" * 40,
            )

            artifact_path = selection_artifact_path(output_dir)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            write_selection(artifact_path, selection)
            loaded = load_selection(artifact_path)

            self.assertEqual(loaded.previous_first_known_bad_commit, "f" * 40)
            self.assertEqual(loaded.previous_downstream_commit, "d" * 40)
            self.assertEqual(loaded.previous_last_known_good_commit, "g" * 40)

    def test_selection_round_trip_preserves_skip_known_bad_bisect(self) -> None:
        """Scenario: per-downstream skip_known_bad_bisect=False survives serialisation."""

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifacts"
            selection = WindowSelection(
                downstream="SlowProject",
                repo="owner/SlowProject",
                default_branch="main",
                upstream_ref="master",
                target_commit="t" * 40,
                skip_known_bad_bisect=False,
            )

            artifact_path = selection_artifact_path(output_dir)
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            write_selection(artifact_path, selection)
            loaded = load_selection(artifact_path)

            self.assertFalse(loaded.skip_known_bad_bisect)

    def test_selection_summary_explains_skipped_probe(self) -> None:
        """Scenario: the selector summary explains why the probe task will not run."""

        selection = WindowSelection(
            downstream="PrimeNumberTheoremAnd",
            upstream_ref="master",
            target_commit="bad" * 10,
            head_probe_outcome="passed",
            decision_reason="The upper endpoint passed, so there is no failing window to bisect.",
            next_action="Skip the probe task and report the passing head-only result.",
        )

        summary = render_selection_summary(selection)

        self.assertIn("Window Selection Summary", summary)
        self.assertIn("Head probe outcome: `passed`", summary)
        self.assertIn("Decision:", summary)
        self.assertIn("there is no failing window to bisect", summary)
        self.assertIn("Skip the probe task", summary)


class ClassifyExitCodeTests(unittest.TestCase):
    """Scenarios for mapping hopscotch exit codes to outcomes."""

    def test_zero_is_passed(self) -> None:
        self.assertEqual(classify_exit_code(0), Outcome.PASSED)

    def test_one_is_failed(self) -> None:
        self.assertEqual(classify_exit_code(1), Outcome.FAILED)

    def test_other_codes_are_error(self) -> None:
        self.assertEqual(classify_exit_code(2), Outcome.ERROR)
        self.assertEqual(classify_exit_code(137), Outcome.ERROR)


class BuildSkipResultTests(unittest.TestCase):
    """Scenarios for synthetic skip results."""

    def test_skip_result_has_correct_outcome_and_search_mode(self) -> None:
        """Scenario: a synthetic skip result carries the caller-supplied outcome and search_mode tag."""

        config = DownstreamConfig(
            name="physlib",
            repo="leanprover-community/physlib",
            default_branch="master",
        )
        result = build_skip_result(
            config=config,
            downstream_commit="ds_abc",
            upstream_ref="master",
            target_commit="target_abc",
            search_mode="skipped-already-good",
            outcome=Outcome.PASSED,
            last_successful_commit="target_abc",
            summary="Skipped.",
        )
        self.assertEqual(result.outcome, Outcome.PASSED)
        self.assertEqual(result.search_mode, "skipped-already-good")
        self.assertEqual(result.downstream_commit, "ds_abc")
        self.assertEqual(result.target_commit, "target_abc")
        self.assertEqual(result.tested_commits, ["target_abc"])
        self.assertIsNone(result.error)
        self.assertIsNone(result.first_failing_commit)
        self.assertEqual(result.last_successful_commit, "target_abc")

    def test_skip_result_with_no_target_has_empty_tested_commits(self) -> None:
        """Scenario: when no target commit is known, tested_commits is empty rather than [None]."""

        config = DownstreamConfig(
            name="physlib",
            repo="leanprover-community/physlib",
            default_branch="master",
        )
        result = build_skip_result(
            config=config,
            downstream_commit=None,
            upstream_ref="master",
            target_commit=None,
            search_mode="skipped-cached",
            outcome=Outcome.PASSED,
            summary="Cached.",
        )
        self.assertEqual(result.tested_commits, [])


_PHYSLIB_CONFIG = DownstreamConfig(
    name="physlib",
    repo="leanprover-community/physlib",
    default_branch="master",
)


def _make_selection(**kwargs) -> WindowSelection:
    """Build a WindowSelection with sensible defaults for skip-heuristic tests."""
    defaults = dict(
        downstream="physlib",
        repo="leanprover-community/physlib",
        default_branch="master",
        upstream_ref="master",
        target_commit="t" * 40,
        downstream_commit="d" * 40,
        pinned_commit="p" * 40,
    )
    defaults.update(kwargs)
    return WindowSelection(**defaults)


class TrySkipAlreadyGoodTests(unittest.TestCase):
    """Scenarios for the already-good skip heuristic."""

    def test_returns_none_when_disabled(self) -> None:
        """Scenario: the heuristic is bypassed entirely when skip_enabled=False, even if conditions would match."""

        selection = _make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit=selection.target_commit,
            downstream_commit=selection.downstream_commit,
        )
        result = try_skip_already_good(
            skip_enabled=False, selection=selection, previous=previous,
            config=_PHYSLIB_CONFIG, upstream_ref="master",
        )
        self.assertIsNone(result)

    def test_returns_none_when_no_previous(self) -> None:
        """Scenario: without a prior status record there is no baseline to compare against."""

        result = try_skip_already_good(
            skip_enabled=True, selection=_make_selection(), previous=None,
            config=_PHYSLIB_CONFIG, upstream_ref="master",
        )
        self.assertIsNone(result)

    def test_returns_none_when_target_differs(self) -> None:
        """Scenario: the target commit has moved since the last run, so we cannot skip validation."""

        selection = _make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit="other" * 8,
            downstream_commit=selection.downstream_commit,
        )
        result = try_skip_already_good(
            skip_enabled=True, selection=selection, previous=previous,
            config=_PHYSLIB_CONFIG, upstream_ref="master",
        )
        self.assertIsNone(result)

    def test_returns_none_when_downstream_changed(self) -> None:
        """Scenario: the downstream repo itself has new commits, so the prior result may not apply."""

        selection = _make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit=selection.target_commit,
            downstream_commit="other" * 8,
        )
        result = try_skip_already_good(
            skip_enabled=True, selection=selection, previous=previous,
            config=_PHYSLIB_CONFIG, upstream_ref="master",
        )
        self.assertIsNone(result)

    def test_returns_passing_result_when_conditions_match(self) -> None:
        """Scenario: target equals last-known-good and downstream is unchanged — safe to skip all probes."""

        selection = _make_selection()
        previous = DownstreamStatusRecord(
            last_known_good_commit=selection.target_commit,
            downstream_commit=selection.downstream_commit,
        )
        result = try_skip_already_good(
            skip_enabled=True, selection=selection, previous=previous,
            config=_PHYSLIB_CONFIG, upstream_ref="master",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, Outcome.PASSED)
        self.assertEqual(result.search_mode, "skipped-already-good")
        self.assertIn("already verified", selection.decision_reason)
        self.assertIn("Skip all probes", selection.next_action)


class TrySkipKnownBadBisectTests(unittest.TestCase):
    """Scenarios for the known-bad-bisect skip heuristic."""

    _head_probe_run = subprocess.CompletedProcess(["tool"], 1, stdout="failed", stderr="failed")
    _head_probe_state: dict = {"failureStage": "lake build", "firstFailingCommit": "t" * 40}

    def _call(self, *, skip_enabled=True, previous=None, selection=None, ancestor=True):
        selection = selection or _make_selection(
            head_probe_outcome="failed",
            head_probe_failure_stage="build",
            head_probe_summary="failed",
            tested_commit_details=[CommitDetail(sha="t" * 40, title="test")],
        )
        with patch(
            "scripts.probe_downstream_regression_window.is_strict_ancestor",
            return_value=ancestor,
        ):
            return try_skip_known_bad_bisect(
                skip_enabled=skip_enabled,
                selection=selection,
                previous=previous,
                config=_PHYSLIB_CONFIG,
                upstream_ref="master",
                upstream_dir=Path("/dummy"),
                head_probe_run=self._head_probe_run,
                head_probe_state=self._head_probe_state,
                head_probe_summary_text="failed",
            ), selection

    def test_returns_none_when_disabled(self) -> None:
        """Scenario: the heuristic is bypassed entirely when skip_enabled=False, even if conditions would match."""

        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="d" * 40,
        )
        result, _ = self._call(skip_enabled=False, previous=previous)
        self.assertIsNone(result)

    def test_returns_none_when_no_first_known_bad(self) -> None:
        """Scenario: without a prior known-bad commit we have no culprit anchor to reuse."""

        previous = DownstreamStatusRecord(downstream_commit="d" * 40)
        result, _ = self._call(previous=previous)
        self.assertIsNone(result)

    def test_returns_none_when_downstream_changed(self) -> None:
        """Scenario: the downstream has new commits since last run; the old culprit attribution may be wrong."""

        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="other" * 8,
        )
        result, _ = self._call(previous=previous)
        self.assertIsNone(result)

    def test_returns_none_when_not_ancestor(self) -> None:
        """Scenario: the prior bad commit is not an ancestor of the current target, so the episode may have healed."""

        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="d" * 40,
        )
        result, _ = self._call(previous=previous, ancestor=False)
        self.assertIsNone(result)

    def test_returns_failing_result_when_conditions_match(self) -> None:
        """Scenario: known-bad commit is still an ancestor and downstream unchanged — skip re-bisecting."""

        previous = DownstreamStatusRecord(
            first_known_bad_commit="b" * 40,
            downstream_commit="d" * 40,
        )
        result, selection = self._call(previous=previous)
        self.assertIsNotNone(result)
        self.assertEqual(result.outcome, Outcome.FAILED)
        self.assertEqual(result.search_mode, "head-only-known-bad")
        self.assertIn("Re-bisecting", selection.decision_reason)
        self.assertIn("Skip the bisect", selection.next_action)


_PROBE_PARSER_REQUIRED = ["--selection", "/tmp/s.json", "--workdir", "/tmp", "--output-dir", "/tmp"]


class SkipOptimisationFlagsTests(unittest.TestCase):
    """Scenarios for the --skip-already-good / --skip-known-bad-bisect flags."""

    def test_skip_already_good_defaults_to_true(self) -> None:
        """Scenario: skip-already-good is opt-out and engages by default in the select step."""

        args = select_build_parser().parse_args(["--workdir", "/tmp", "--output-dir", "/tmp"])
        self.assertTrue(args.skip_already_good)

    def test_skip_known_bad_bisect_defaults_to_true(self) -> None:
        """Scenario: skip-known-bad-bisect is opt-out and engages by default in the probe step."""

        args = probe_build_parser().parse_args(_PROBE_PARSER_REQUIRED)
        self.assertTrue(args.skip_known_bad_bisect)

    def test_skip_already_good_can_be_disabled(self) -> None:
        """Scenario: --no-skip-already-good forces full validation in the select step."""

        args = select_build_parser().parse_args([
            "--workdir", "/tmp", "--output-dir", "/tmp", "--no-skip-already-good",
        ])
        self.assertFalse(args.skip_already_good)

    def test_skip_known_bad_bisect_can_be_disabled(self) -> None:
        """Scenario: --no-skip-known-bad-bisect forces full bisect in the probe step."""

        args = probe_build_parser().parse_args([*_PROBE_PARSER_REQUIRED, "--no-skip-known-bad-bisect"])
        self.assertFalse(args.skip_known_bad_bisect)

    def test_skip_already_good_can_be_explicitly_enabled(self) -> None:
        """Scenario: --skip-already-good can be passed explicitly to confirm default behaviour."""

        args = select_build_parser().parse_args([
            "--workdir", "/tmp", "--output-dir", "/tmp", "--skip-already-good",
        ])
        self.assertTrue(args.skip_already_good)

    def test_skip_known_bad_bisect_can_be_explicitly_enabled(self) -> None:
        """Scenario: --skip-known-bad-bisect can be passed explicitly to confirm default behaviour."""

        args = probe_build_parser().parse_args([*_PROBE_PARSER_REQUIRED, "--skip-known-bad-bisect"])
        self.assertTrue(args.skip_known_bad_bisect)

    def test_probe_max_commits_defaults_and_overrides(self) -> None:
        """Scenario: --max-commits on the probe parser defaults to 100000 and can be overridden."""

        args = probe_build_parser().parse_args(_PROBE_PARSER_REQUIRED)
        self.assertEqual(args.max_commits, 100000)
        args = probe_build_parser().parse_args([*_PROBE_PARSER_REQUIRED, "--max-commits", "50"])
        self.assertEqual(args.max_commits, 50)

    def test_downstream_config_skip_flags_default_to_true(self) -> None:
        """Scenario: a downstream with no skip overrides inherits both heuristics as enabled."""

        config = DownstreamConfig(name="foo", repo="owner/foo", default_branch="main")
        self.assertTrue(config.skip_already_good)
        self.assertTrue(config.skip_known_bad_bisect)

    def test_downstream_config_skip_flags_can_be_disabled_in_inventory(self) -> None:
        """Scenario: an inventory entry can permanently opt a downstream out of one or both heuristics."""

        import json, tempfile
        inventory = {
            "schema_version": 1,
            "downstreams": [
                {
                    "name": "slow-downstream",
                    "repo": "owner/slow-downstream",
                    "default_branch": "main",
                    "skip_already_good": False,
                    "skip_known_bad_bisect": True,
                    "enabled": True,
                },
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(inventory, f)
            path = Path(f.name)
        loaded = load_inventory(path)
        self.assertFalse(loaded["slow-downstream"].skip_already_good)
        self.assertTrue(loaded["slow-downstream"].skip_known_bad_bisect)


class TryCulpritProbeTests(unittest.TestCase):
    """Scenarios for the culprit-commit re-build that follows a known-bad bisect skip."""

    def test_runs_validation_attempt(self) -> None:
        """Scenario: probe invokes run_validation_attempt with culprit-probe output dir."""
        mock_run = Mock(return_value=(Mock(), {}, None))
        with patch(
            "scripts.probe_downstream_regression_window.run_validation_attempt",
            mock_run,
        ), patch(
            "scripts.probe_downstream_regression_window.parent_commit",
            return_value="p" * 40,
        ):
            run_culprit_probe(
                config=_PHYSLIB_CONFIG,
                culprit_commit="b" * 40,
                upstream_dir=Path("/dummy"),
                project_dir=Path("/dummy/downstream"),
                output_dir=Path("/dummy/output"),
                env={},
                tool_exe=None,
            )
        self.assertTrue(mock_run.called)
        call_kwargs = mock_run.call_args[1]
        self.assertEqual(call_kwargs["output_dir"], Path("/dummy/output/culprit-probe"))

    def test_does_not_propagate_exception(self) -> None:
        """Scenario: if the probe raises (e.g. subprocess error), the exception is swallowed."""
        with patch(
            "scripts.probe_downstream_regression_window.run_validation_attempt",
            side_effect=RuntimeError("tool crashed"),
        ), patch(
            "scripts.probe_downstream_regression_window.parent_commit",
            return_value="p" * 40,
        ):
            # Should not raise
            run_culprit_probe(
                config=_PHYSLIB_CONFIG,
                culprit_commit="b" * 40,
                upstream_dir=Path("/dummy"),
                project_dir=Path("/dummy/downstream"),
                output_dir=Path("/dummy/output"),
                env={},
                tool_exe=None,
            )


if __name__ == "__main__":
    unittest.main()
