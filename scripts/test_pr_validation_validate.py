#!/usr/bin/env python3
"""
Tests for: scripts.pr_validation.validate

Coverage scope:
    - ``Config.from_env`` — the env-var contract (required vars, mode
      validation, optional fallbacks).
    - ``build_result_record`` — the JSON shape on disk for each (status,
      mode, optional-field) combo.
    - ``derive_pr_endpoints`` — the (PR_BASE, PR_HEAD) derivation from a
      merge commit's parents (two-parent and fast-forward cases).
    - ``Log`` — the dual-write contract: stdout + log file, both for
      ``Log.print`` and ``Log.run`` (subprocess streaming).
    - ``fail_infra`` — emits ``infra_failure`` result + annotation +
      exits 0.

Out of scope:
    - The individual stage functions (``clone_mathlib``,
      ``warm_cache``, …).  Those are subprocess orchestrators against
      real git/lake; they're exercised by the smoke-dispatch runs
      against PR 38783, not in this unit suite.
    - The lake / lakedit tools themselves.

Why this matters
----------------
``validate.py`` is the only code path that produces the per-entry
``result.json`` artifacts that ``post_results.py`` then renders into
PR comments.  Drift in the JSON shape (a missed field, a wrong
default) silently breaks the rendered comment without a workflow
error.  The pre-flight ``Config.from_env`` is what stops a misconfigured
workflow from blasting through every stage before failing on a missing
env var, and the stage→infra-failure mapping is what makes the comment
say "PR conflicts with LKG" instead of "infra_failure (rebase_conflict)".
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.conftest import SHA_A, SHA_C, SHA_D, SHA_F
from scripts.pr_validation import validate
from scripts.pr_validation.validate import (
    MODE_LKG,
    MODE_MERGE,
    Config,
    Log,
    State,
    build_result_record,
    derive_pr_endpoints,
)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _minimal_env(**overrides) -> dict[str, str]:
    """A complete env that satisfies Config.from_env's required vars.

    Mode defaults to LKG (so ``LKG_COMMIT`` is required); tests for the
    merge path pass ``MODE="merge"`` via ``**overrides``.
    """
    env = {
        "PR_NUMBER": "38783",
        "MERGE_SHA": SHA_A,
        "DOWNSTREAM": "FLT",
        "DOWNSTREAM_REPO": "leanprover-community/FLT",
        "DEFAULT_BRANCH": "main",
        "DEPENDENCY_NAME": "mathlib",
        "WORKDIR": "/tmp/wd",
        "OUTPUT_DIR": "/tmp/out",
        "TOOL_BIN": "/tmp/bin",
        "MODE": MODE_LKG,
        "LKG_COMMIT": SHA_C,
    }
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Config.from_env
# ---------------------------------------------------------------------------


class TestConfigFromEnv:
    """``Config.from_env`` reads + validates the env-var contract."""

    def test_required_fields_round_trip(self) -> None:
        """Every required var lands on the right Config attribute.

        These map 1:1 with the bash script's ``: "${VAR:?}"`` guards;
        the test pins that mapping so a typo in the env var name
        surfaces as a unit-test failure rather than a CI-only one.
        """
        # Arrange / Act
        cfg = Config.from_env(_minimal_env())

        # Assert
        assert cfg.pr_number == "38783"
        assert cfg.merge_sha == SHA_A
        assert cfg.downstream == "FLT"
        assert cfg.downstream_repo == "leanprover-community/FLT"
        assert cfg.default_branch == "main"
        assert cfg.dependency_name == "mathlib"
        assert cfg.workdir == Path("/tmp/wd")
        assert cfg.output_dir == Path("/tmp/out")
        assert cfg.tool_bin == Path("/tmp/bin")
        assert cfg.mode == MODE_LKG
        assert cfg.lkg_commit == SHA_C

    def test_mode_defaults_to_lkg(self) -> None:
        """An empty MODE defaults to LKG (the user-facing default).

        Matches the bash script's ``MODE="${MODE:-lkg}"`` so the
        downstream-reports workflow can omit MODE entirely for the
        common case.
        """
        # Arrange
        env = _minimal_env()
        del env["MODE"]

        # Act
        cfg = Config.from_env(env)

        # Assert
        assert cfg.mode == MODE_LKG

    def test_unknown_mode_rejected(self) -> None:
        """A MODE that isn't merge|lkg raises before any work happens."""
        # Arrange
        env = _minimal_env(MODE="weird")

        # Act / Assert
        with pytest.raises(ValueError, match="unknown MODE"):
            Config.from_env(env)

    def test_lkg_requires_lkg_commit(self) -> None:
        """``MODE=lkg`` without ``LKG_COMMIT`` fails fast with a clear message.

        Catches the most common misconfiguration: a manual
        ``workflow_dispatch`` that forgets to pass LKG_COMMIT.
        Without this check the bash script would clone mathlib4 first
        and then crash several stages in.
        """
        # Arrange
        env = _minimal_env()
        del env["LKG_COMMIT"]

        # Act / Assert
        with pytest.raises(ValueError, match="LKG_COMMIT"):
            Config.from_env(env)

    def test_merge_mode_lkg_commit_optional(self) -> None:
        """``MODE=merge`` doesn't require LKG_COMMIT.

        Merge mode builds against the would-be-merged tree directly;
        no rebase anchor needed.  This is what lets the freshly-
        onboarded ``newcomer`` downstream still validate via
        ``--merge-branch``.
        """
        # Arrange
        env = _minimal_env(MODE=MODE_MERGE)
        del env["LKG_COMMIT"]

        # Act
        cfg = Config.from_env(env)

        # Assert
        assert cfg.mode == MODE_MERGE
        assert cfg.lkg_commit == ""

    def test_downstream_rev_defaults_to_default_branch(self) -> None:
        """When DOWNSTREAM_REV is unset, the rev falls back to DEFAULT_BRANCH.

        The bash script does ``DOWNSTREAM_REV="${DOWNSTREAM_REV:-$DEFAULT_BRANCH}"``
        so the validate flow can always rely on a non-empty rev.
        """
        # Arrange / Act
        cfg = Config.from_env(_minimal_env())

        # Assert
        assert cfg.downstream_rev == "main"

    def test_requested_name_defaults_to_downstream(self) -> None:
        """When REQUESTED_NAME is unset, it mirrors the canonical downstream name.

        The matrix builder always sets ``requested_name`` so the env
        var is normally present; for direct dispatches that skip it,
        the fallback keeps the result.json's optional-field
        contract intact.
        """
        # Arrange / Act
        cfg = Config.from_env(_minimal_env())

        # Assert
        assert cfg.requested_name == "FLT"

    def test_requested_name_preserved_when_different(self) -> None:
        """A slug-style REQUESTED_NAME survives onto Config for result.json."""
        # Arrange
        env = _minimal_env(REQUESTED_NAME="leanprover-community/FLT")

        # Act
        cfg = Config.from_env(env)

        # Assert
        assert cfg.requested_name == "leanprover-community/FLT"

    def test_upstream_repo_defaults_to_canonical_mathlib(self) -> None:
        """When UPSTREAM_REPO is unset, we clone leanprover-community/mathlib4.

        That's the only mathlib4 fork we expect to fetch
        ``refs/pull/N/merge`` from; an explicit override is supported
        for testing fixtures.
        """
        # Arrange / Act
        cfg = Config.from_env(_minimal_env())

        # Assert
        assert cfg.upstream_repo == "leanprover-community/mathlib4"

    def test_missing_required_field_reports_which(self) -> None:
        """A missing required var names which one in the error."""
        # Arrange
        env = _minimal_env()
        del env["MERGE_SHA"]

        # Act / Assert
        with pytest.raises(ValueError, match="MERGE_SHA"):
            Config.from_env(env)


# ---------------------------------------------------------------------------
# Result record shape
# ---------------------------------------------------------------------------


class TestBuildResultRecord:
    """``build_result_record`` produces the JSON written to result.json."""

    def _cfg(self, **overrides) -> Config:
        return Config.from_env(_minimal_env(**overrides))

    def test_minimal_pass_record_in_lkg_mode(self) -> None:
        """A clean lkg-mode pass produces the expected baseline shape.

        Required fields always present; optional fields (FKB,
        downstream_rev, requested_name, PR endpoints) only when they
        carry information beyond their default.
        """
        # Arrange
        cfg = self._cfg()
        state = State(downstream_sha=SHA_D)

        # Act
        record = build_result_record(
            cfg, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert record == {
            "status": "pass",
            "stage": "build",
            "message": "ok",
            "downstream": "FLT",
            "merge_sha": SHA_A,
            "downstream_sha": SHA_D,
            "mode": MODE_LKG,
            "lkg_commit": SHA_C,
        }

    def test_merge_mode_pass_omits_lkg_commit(self) -> None:
        """Merge-mode records have no ``lkg_commit`` field."""
        # Arrange
        cfg = self._cfg(MODE=MODE_MERGE, LKG_COMMIT="")
        state = State(downstream_sha=SHA_D)

        # Act
        record = build_result_record(
            cfg, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert "lkg_commit" not in record
        assert record["mode"] == MODE_MERGE

    def test_fkb_attaches_when_set(self) -> None:
        """A non-empty FKB_COMMIT lands on result.json for the comment renderer."""
        # Arrange
        cfg = self._cfg(FKB_COMMIT=SHA_F)
        state = State(downstream_sha=SHA_D)

        # Act
        record = build_result_record(
            cfg, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert record["fkb_commit"] == SHA_F

    def test_fkb_absent_when_empty(self) -> None:
        """An empty FKB_COMMIT omits the field rather than recording ""."""
        # Arrange
        cfg = self._cfg(FKB_COMMIT="")
        state = State(downstream_sha=SHA_D)

        # Act
        record = build_result_record(
            cfg, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert "fkb_commit" not in record

    def test_requested_name_only_when_different_from_downstream(self) -> None:
        """``requested_name`` is recorded only when the slug form differs from the short name.

        Keeps result.json compact for the common case (user typed the
        short name) and makes ``post_results.get("requested_name")``
        falsy → fall back to ``downstream``.
        """
        # Arrange — short-name request
        cfg_short = self._cfg(REQUESTED_NAME="FLT")
        state = State(downstream_sha=SHA_D)

        # Act
        record_short = build_result_record(
            cfg_short, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert "requested_name" not in record_short

        # Arrange — slug request
        cfg_slug = self._cfg(REQUESTED_NAME="leanprover-community/FLT")

        # Act
        record_slug = build_result_record(
            cfg_slug, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert record_slug["requested_name"] == "leanprover-community/FLT"

    def test_downstream_rev_only_when_different_from_default_branch(self) -> None:
        """``downstream_rev`` is recorded only when it changes the checkout target.

        The comment renderer uses field presence to decide whether to
        surface the ``@<rev>`` annotation in the recipe link.
        """
        # Arrange — default rev
        cfg_default = self._cfg()
        state = State(downstream_sha=SHA_D)

        # Act / Assert
        record = build_result_record(
            cfg_default, state, status="pass", stage="build", message="ok"
        )
        assert "downstream_rev" not in record

        # Arrange — explicit rev
        cfg_pinned = self._cfg(DOWNSTREAM_REV="v1.2.3")

        # Act
        record = build_result_record(
            cfg_pinned, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert record["downstream_rev"] == "v1.2.3"

    def test_pr_endpoints_and_commit_count_when_resolved(self) -> None:
        """PR_BASE/PR_HEAD/commits_replayed flow into the record once resolved.

        These are populated by ``resolve_mathlib_tree`` after the
        merge SHA is fetched.  The recipe paragraph in the rendered
        comment relies on them for the compare URL.
        """
        # Arrange
        cfg = self._cfg()
        state = State(
            downstream_sha=SHA_D,
            pr_base="1" * 40,
            pr_head="2" * 40,
            n_commits=3,
        )

        # Act
        record = build_result_record(
            cfg, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert record["pr_base_sha"] == "1" * 40
        assert record["pr_head_sha"] == "2" * 40
        assert record["commits_replayed"] == 3

    def test_replayed_tree_sha_recorded_in_lkg_mode_only(self) -> None:
        """The synthetic post-cherry-pick tree SHA is recorded for LKG passes only.

        Merge mode doesn't synthesise a new commit; even if State
        happened to carry a value, the record stays clean.
        """
        # Arrange — LKG
        cfg_lkg = self._cfg()
        state = State(downstream_sha=SHA_D, replayed_tree_sha="3" * 40)

        # Act
        record = build_result_record(
            cfg_lkg, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert record["replayed_tree_sha"] == "3" * 40

        # Arrange — merge
        cfg_merge = self._cfg(MODE=MODE_MERGE, LKG_COMMIT="")

        # Act
        record = build_result_record(
            cfg_merge, state, status="pass", stage="build", message="ok"
        )

        # Assert
        assert "replayed_tree_sha" not in record

    def test_infra_failure_record_keeps_stage_and_message(self) -> None:
        """Infra failures preserve the stage name + message for the renderer.

        The stage is what dispatches the comment renderer onto the
        targeted ``rebase_conflict`` / ``mathlib_build_at_lkg``
        explainers; the message gives the renderer the fallback text
        when no targeted explainer fires.
        """
        # Arrange
        cfg = self._cfg()
        state = State(downstream_sha="")  # the build failed before we cloned the downstream

        # Act
        record = build_result_record(
            cfg, state,
            status="infra_failure",
            stage="rebase_conflict",
            message="PR commits do not apply on top of LKG …",
        )

        # Assert
        assert record["status"] == "infra_failure"
        assert record["stage"] == "rebase_conflict"
        assert record["message"].startswith("PR commits do not apply")
        # downstream_sha is None (not the literal empty string) so the
        # renderer's ``if ds_sha`` check fires correctly.
        assert record["downstream_sha"] is None


# ---------------------------------------------------------------------------
# PR endpoint derivation
# ---------------------------------------------------------------------------


class TestDerivePrEndpoints:
    """``derive_pr_endpoints`` extracts (PR_BASE, PR_HEAD) from MERGE_SHA's parents."""

    def test_two_parent_merge_returns_both(self) -> None:
        """A normal merge commit (^1 = base, ^2 = head) yields both endpoints.

        That's the common case: GitHub computes
        ``refs/pull/N/merge = merge(base, head)`` and we extract both
        parents directly.
        """
        # Arrange
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc_result(stdout="aaaa\n"),  # ^1
                _proc_result(stdout="bbbb\n"),  # ^2
            ]

            # Act
            pr_base, pr_head = derive_pr_endpoints(
                log=_noop_log(), mathlib_dir=Path("/repo"), merge_sha=SHA_A
            )

        # Assert
        assert pr_base == "aaaa"
        assert pr_head == "bbbb"

    def test_fast_forward_merge_returns_empty_head(self) -> None:
        """A single-parent merge commit reports PR_HEAD == "".

        Fast-forward case: the PR's tree IS the merge SHA, so there's
        nothing to cherry-pick on top of LKG.  The caller treats
        empty PR_HEAD as the signal to skip the cherry-pick step.
        """
        # Arrange
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                _proc_result(stdout="aaaa\n"),  # ^1
                _proc_result(returncode=128, stderr="bad revision"),  # ^2
            ]

            # Act
            pr_base, pr_head = derive_pr_endpoints(
                log=_noop_log(), mathlib_dir=Path("/repo"), merge_sha=SHA_A
            )

        # Assert
        assert pr_base == "aaaa"
        assert pr_head == ""

    def test_unresolvable_base_returns_both_empty(self) -> None:
        """When ^1 itself fails to resolve, both endpoints stay empty.

        The caller treats this as a fatal error and emits an
        ``infra_failure`` stage=rev_parse — better than silently
        building against an undefined tree.
        """
        # Arrange
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _proc_result(returncode=128)

            # Act
            pr_base, pr_head = derive_pr_endpoints(
                log=_noop_log(), mathlib_dir=Path("/repo"), merge_sha=SHA_A
            )

        # Assert
        assert pr_base == ""
        assert pr_head == ""


def _proc_result(
    *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _noop_log() -> Log:
    """A Log that writes its log-file output to an in-memory buffer."""
    log = Log(Path("/dev/null"), sink=io.StringIO())
    log.__enter__()
    return log


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


class TestLog:
    """``Log`` mirrors every line to both stdout and the log file.

    The contract that bash's ``exec > >(tee -a "$LOG") 2>&1`` provided:
    nothing the script emits is invisible to the live workflow log,
    and the final build.log artifact is the complete transcript.
    """

    def test_print_writes_to_both_streams(self, capsys: pytest.CaptureFixture) -> None:
        """``log.print`` shows up in stdout AND the log file."""
        # Arrange
        sink = io.StringIO()
        with Log(Path("/dev/null"), sink=sink) as log:
            # Act
            log.print("hello")

        # Assert
        assert capsys.readouterr().out == "hello\n"
        assert sink.getvalue() == "hello\n"

    def test_run_streams_subprocess_output_to_both(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """``log.run`` mirrors a subprocess's stdout+stderr to both sinks.

        The subprocess writes two lines to stdout and one to stderr;
        both end up in stdout (Actions log) and the log file
        (artifact) in order.
        """
        # Arrange
        sink = io.StringIO()
        with Log(Path("/dev/null"), sink=sink) as log:
            # Act
            rc = log.run(
                [
                    sys.executable, "-c",
                    "import sys; print('A'); print('B', file=sys.stderr); print('C')",
                ]
            )

        # Assert
        assert rc == 0
        captured = capsys.readouterr().out
        assert "A" in captured
        assert "B" in captured
        assert "C" in captured
        # Same content in the log file.
        assert "A" in sink.getvalue()
        assert "B" in sink.getvalue()
        assert "C" in sink.getvalue()

    def test_run_returns_subprocess_exit_code(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """``log.run`` returns the subprocess's exit code unchanged."""
        # Arrange
        sink = io.StringIO()
        with Log(Path("/dev/null"), sink=sink) as log:
            # Act
            rc = log.run([sys.executable, "-c", "import sys; sys.exit(7)"])

        # Assert
        assert rc == 7

    def test_print_after_exit_raises(self) -> None:
        """Using a Log outside its ``with`` block is a programming error.

        Catches a class of refactor bugs where stage functions
        accidentally hold onto a stale Log reference.
        """
        # Arrange
        log = Log(Path("/dev/null"), sink=io.StringIO())
        with log:
            pass

        # Act / Assert
        with pytest.raises(AssertionError):
            log.print("nope")


# ---------------------------------------------------------------------------
# fail_infra
# ---------------------------------------------------------------------------


class TestFailInfra:
    """``fail_infra`` writes result.json, emits an annotation, exits 0."""

    def test_emits_infra_failure_record_and_exits_zero(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """The on-disk record has status=infra_failure with the supplied stage and message."""
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            env = _minimal_env(OUTPUT_DIR=tmp)
            cfg = Config.from_env(env)
            state = State()
            sink = io.StringIO()
            with Log(Path("/dev/null"), sink=sink) as log:
                # Act
                with pytest.raises(SystemExit) as exc_info:
                    validate.fail_infra(
                        cfg, state, log,
                        stage="clone",
                        title="Clone failed",
                        message="could not clone leanprover-community/mathlib4",
                    )

            # Assert
            assert exc_info.value.code == 0
            record = json.loads((Path(tmp) / "result.json").read_text())
            assert record["status"] == "infra_failure"
            assert record["stage"] == "clone"
            assert (
                record["message"]
                == "could not clone leanprover-community/mathlib4"
            )

    def test_default_annotation_is_error_level(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """The default annotation severity is ``::error::``.

        Stage failures like clone / fetch / lakedit should look
        alarming in the workflow log; the warning-level variants are
        reserved for ``rebase_conflict`` and ``mathlib_build_at_lkg``
        where the "failure" is really "PR can't be tested against
        older mathlib".
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_env(_minimal_env(OUTPUT_DIR=tmp))
            state = State()
            sink = io.StringIO()

            # Act
            with Log(Path("/dev/null"), sink=sink) as log:
                with pytest.raises(SystemExit):
                    validate.fail_infra(
                        cfg, state, log,
                        stage="clone",
                        title="Clone failed",
                        message="oops",
                    )

        # Assert
        assert "::error title=Clone failed::" in sink.getvalue()

    def test_warning_annotation_for_soft_failures(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        """``annotation="warning"`` emits ``::warning::`` instead.

        Used by rebase_conflict / mathlib_build_at_lkg where the
        failure reflects an inherent limitation of LKG-mode
        validation, not an infrastructure problem.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_env(_minimal_env(OUTPUT_DIR=tmp))
            state = State()
            sink = io.StringIO()

            # Act
            with Log(Path("/dev/null"), sink=sink) as log:
                with pytest.raises(SystemExit):
                    validate.fail_infra(
                        cfg, state, log,
                        stage="rebase_conflict",
                        title="Rebase conflict",
                        message="PR commits do not apply on top of LKG …",
                        annotation="warning",
                    )

        # Assert
        assert "::warning title=Rebase conflict::" in sink.getvalue()
        assert "::error" not in sink.getvalue()
