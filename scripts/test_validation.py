#!/usr/bin/env python3
"""
Tests for: scripts.validation

Coverage scope:
    - ``invoke_tool`` — chooses between a workflow-supplied prebuilt
      hopscotch binary and a ``lake exe`` fallback build.
    - ``classify_exit_code`` — maps hopscotch's process exit code to a
      semantic ``Outcome``.
    - ``build_skip_result`` — synthesises a ``ValidationResult`` for
      skip-heuristic paths that bypass the actual probe.
    - ``append_commit_plan_artifact`` / ``commit_plan_artifact_path``
      / ``print_commit_plan_summary`` — the runner-side artifact that
      records the full bisect commit list while keeping stdout brief.
    - ``write_selection`` / ``load_selection`` /
      ``selection_artifact_path`` — round-trip serialisation of the
      ``WindowSelection`` artifact that hands off from the select job
      to the probe job.
    - ``render_selection_summary`` — the markdown summary the select
      job posts to the workflow run.

Out of scope:
    - ``run_validation_attempt`` — exercised by the probe / select
      step tests where it is patched out at the module boundary.
    - The hopscotch tool itself; tests assert the command line that
      would be executed without invoking it.

Why this matters
----------------
The selection artifact is the contract between the select step (with
secrets, on ubuntu-latest) and the probe step (no secrets, on the
self-hosted runner).  A dropped field on the round trip would silently
break the probe's ability to honour per-downstream skip flags or
prior-episode state.  The exit-code classification is the contract
between hopscotch and the regression workflow — a wrong mapping would
record a ``failed`` run as ``error`` (silent in the state machine) or
vice versa.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.models import CommitDetail, DownstreamConfig, Outcome, WindowSelection
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


class InvokeToolTests(unittest.TestCase):
    """Choosing how the workflow invokes the validator executable."""

    def test_prebuilt_tool_binary_is_used_when_provided(self) -> None:
        """A workflow-supplied binary is invoked directly without ``lake exe``.

        The regression workflow downloads a prebuilt hopscotch artifact
        in the plan job and forwards its path to the probe job; this
        avoids paying the rebuild check on every probe.  When
        ``tool_exe`` is ``None`` the fallback path uses ``lake exe``,
        but that branch is exercised end-to-end in the workflow itself.
        """
        # Arrange
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

            with patch(
                "scripts.validation.subprocess.Popen", return_value=mock_process
            ) as mock_popen:
                # Act
                invoke_tool(
                    config,
                    "deadbeef00000000000000000000000000000000",
                    "cafebabe00000000000000000000000000000000",
                    project_dir,
                    output_dir,
                    {"LAKE_CACHE_DIR": str(Path(tmp) / "cache")},
                    tool_exe,
                )

            # Assert
            invoked_command = mock_popen.call_args.args[0]
            self.assertEqual(
                str(tool_exe),
                invoked_command[0],
                msg="argv[0] is the prebuilt binary, not `lake`",
            )
            self.assertNotIn(
                "lake",
                invoked_command,
                msg="The prebuilt path bypasses `lake exe` entirely",
            )
            self.assertIn("--from", invoked_command)
            self.assertIn("--to", invoked_command)
            self.assertNotIn("--commits-file", invoked_command)


class ClassifyExitCodeTests(unittest.TestCase):
    """Mapping hopscotch exit codes to ``Outcome``."""

    def test_zero_is_passed(self) -> None:
        """Exit 0 maps to ``PASSED``.  Hopscotch's success contract."""
        # Arrange / Act / Assert
        self.assertEqual(classify_exit_code(0), Outcome.PASSED)

    def test_one_is_failed(self) -> None:
        """Exit 1 maps to ``FAILED``.  This is hopscotch's "build broke" signal."""
        # Arrange / Act / Assert
        self.assertEqual(classify_exit_code(1), Outcome.FAILED)

    def test_other_codes_are_error(self) -> None:
        """Any non-{0,1} exit is ``ERROR`` — including process-killed signals like 137.

        ``ERROR`` is silent in the state machine: it preserves the
        prior episode without opening or closing one.  Mapping crashes
        and OOM kills here keeps the regression record from flapping
        on infrastructure failures.
        """
        # Arrange / Act / Assert
        self.assertEqual(classify_exit_code(2), Outcome.ERROR)
        self.assertEqual(
            classify_exit_code(137),
            Outcome.ERROR,
            msg="Signal-kill codes (128 + signal) map to ERROR, not FAILED",
        )


class BuildSkipResultTests(unittest.TestCase):
    """Synthesising a ``ValidationResult`` for skip-heuristic paths."""

    def test_skip_result_has_correct_outcome_and_search_mode(self) -> None:
        """A skip result carries the caller-supplied outcome and a ``search_mode`` tag.

        The ``search_mode`` string ("skipped-already-good",
        "skipped-cached", "head-only-known-bad") is what
        ``aggregate_results`` reads to render the report's "Previously
        Tested" section.  A wrong tag would silently mis-render the
        report.
        """
        # Arrange
        config = DownstreamConfig(
            name="physlib",
            repo="leanprover-community/physlib",
            default_branch="master",
        )

        # Act
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

        # Assert
        self.assertEqual(result.outcome, Outcome.PASSED)
        self.assertEqual(result.search_mode, "skipped-already-good")
        self.assertEqual(result.downstream_commit, "ds_abc")
        self.assertEqual(result.target_commit, "target_abc")
        self.assertEqual(result.tested_commits, ["target_abc"])
        self.assertIsNone(result.error)
        self.assertIsNone(result.first_failing_commit)
        self.assertEqual(result.last_successful_commit, "target_abc")

    def test_skip_result_with_no_target_has_empty_tested_commits(self) -> None:
        """Without a target commit, ``tested_commits`` is ``[]`` rather than ``[None]``.

        ``aggregate_results.first_bad_position`` looks up SHAs in this
        list; a stray ``None`` entry would produce a misleading
        ``found at position 0`` reading.
        """
        # Arrange
        config = DownstreamConfig(
            name="physlib",
            repo="leanprover-community/physlib",
            default_branch="master",
        )

        # Act
        result = build_skip_result(
            config=config,
            downstream_commit=None,
            upstream_ref="master",
            target_commit=None,
            search_mode="skipped-cached",
            outcome=Outcome.PASSED,
            summary="Cached.",
        )

        # Assert
        self.assertEqual(
            result.tested_commits,
            [],
            msg="No target ⇒ empty list, not [None] — keeps first_bad_position honest",
        )


class CommitPlanArtifactTests(unittest.TestCase):
    """Runner-side commit-plan logging and artifacts."""

    def test_commit_plan_artifact_keeps_full_list_while_stdout_stays_brief(self) -> None:
        """The artifact file holds every commit; stdout shows just a count.

        The full list can be hundreds of commits long for wide bisect
        windows; printing it inline would drown the workflow log.
        Keeping the full list in an artifact while stdout shows
        ``2 commits (full list: tested-commits.txt)`` lets engineers
        drill in only when they need to.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "artifacts"
            commits = [
                CommitDetail(sha="a" * 40, title="first title"),
                CommitDetail(sha="b" * 40, title="second title"),
            ]

            # Act — write artifact
            append_commit_plan_artifact(
                output_dir=output_dir,
                label="bisect window (oldest to newest)",
                commits=commits,
                bisect_window=True,
            )

            # Assert — artifact contains both commits with full SHAs and titles
            artifact_path = commit_plan_artifact_path(output_dir)
            artifact_text = artifact_path.read_text()
            self.assertIn("bisect window (oldest to newest)", artifact_text)
            self.assertIn(f"- {'a' * 40} first title", artifact_text)
            self.assertIn(f"- {'b' * 40} second title", artifact_text)

            # Act — print the brief summary
            with patch("builtins.print") as mock_print:
                print_commit_plan_summary(
                    downstream="PrimeNumberTheoremAnd",
                    label="bisect window (oldest to newest)",
                    commits=commits,
                    artifact_path=artifact_path,
                )

            # Assert — stdout summary names the count and points at the artifact
            mock_print.assert_called_once_with(
                "[PrimeNumberTheoremAnd] bisect window (oldest to newest): 2 commits "
                "(full list: tested-commits.txt)"
            )


class WindowSelectionArtifactTests(unittest.TestCase):
    """Round-trip serialisation of the select-to-probe handoff artifact."""

    def test_selection_artifact_round_trip_preserves_probe_metadata(self) -> None:
        """A bisect-window selection round-trips with all probe-relevant fields intact.

        Every field listed here is consumed by the probe job: a silent
        drop on the round trip would either crash the probe or cause
        it to do the wrong thing (e.g. retry a passed head probe,
        ignore the truncation flag, or build a wrong window).
        """
        # Arrange
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

            # Act
            write_selection(artifact_path, selection)
            loaded = load_selection(artifact_path)

            # Assert
            self.assertTrue(loaded.has_bisect_window)
            self.assertEqual(loaded.search_mode, "bisect")
            self.assertEqual(loaded.tested_commits, ["good" * 10, "bad" * 10])
            self.assertEqual(loaded.tested_commit_details[0].title, "good title")
            self.assertEqual(loaded.head_probe_failure_stage, "build")
            self.assertEqual(loaded.selected_lower_bound_commit, "good" * 10)
            self.assertEqual(loaded.decision_reason, "A usable window exists.")
            self.assertEqual(loaded.next_action, "Run the probe task.")

    def test_selection_round_trip_preserves_previous_episode_state(self) -> None:
        """Prior-episode fields (LKG, FKB, downstream_commit) survive serialisation.

        The probe job reconstructs a ``DownstreamStatusRecord`` from
        these fields without DB access, so the skip heuristics still
        work even though the probe runs on the secret-free runner.  A
        round-trip drop here disables both heuristics silently.
        """
        # Arrange
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

            # Act
            write_selection(artifact_path, selection)
            loaded = load_selection(artifact_path)

            # Assert
            self.assertEqual(loaded.previous_first_known_bad_commit, "f" * 40)
            self.assertEqual(loaded.previous_downstream_commit, "d" * 40)
            self.assertEqual(loaded.previous_last_known_good_commit, "g" * 40)

    def test_selection_round_trip_preserves_skip_known_bad_bisect(self) -> None:
        """A per-downstream ``skip_known_bad_bisect=False`` opt-out survives the trip.

        The select job reads the inventory; the probe job reads the
        artifact.  The opt-out value must propagate so the probe can
        honour it without re-reading the inventory.
        """
        # Arrange
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

            # Act
            write_selection(artifact_path, selection)
            loaded = load_selection(artifact_path)

            # Assert
            self.assertFalse(loaded.skip_known_bad_bisect)


class RenderSelectionSummaryTests(unittest.TestCase):
    """Markdown summary emitted by the select job."""

    def test_selection_summary_explains_skipped_probe(self) -> None:
        """The summary names the head probe outcome, the decision, and the next action.

        The select job posts this to the workflow run summary, so an
        engineer reading the run can understand why the probe was
        skipped without diving into the artifact JSON.  The three
        pieces (outcome / decision / next action) are the contract
        with the workflow log.
        """
        # Arrange
        selection = WindowSelection(
            downstream="PrimeNumberTheoremAnd",
            upstream_ref="master",
            target_commit="bad" * 10,
            head_probe_outcome="passed",
            decision_reason="The upper endpoint passed, so there is no failing window to bisect.",
            next_action="Skip the probe task and report the passing head-only result.",
        )

        # Act
        summary = render_selection_summary(selection)

        # Assert
        self.assertIn("Window Selection Summary", summary)
        self.assertIn("Head probe outcome: `passed`", summary)
        self.assertIn("Decision:", summary)
        self.assertIn("there is no failing window to bisect", summary)
        self.assertIn("Skip the probe task", summary)


if __name__ == "__main__":
    unittest.main()
