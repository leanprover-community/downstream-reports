#!/usr/bin/env python3
"""Tests for scripts/pr_validation/post_results.py rendering helpers.

We exercise ``render_body`` and ``render_history_line`` directly — the rest
of the module is glue around the GitHub REST API and is covered manually
during smoke-testing rather than with mocks of `gh api`.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same import shape as test_pr_validation_build_matrix.py: pr_validation/ is
# not a Python package, so load the module directly. log_filter is imported by
# post_results via a top-level `from log_filter import …`, so we need the
# pr_validation directory on sys.path before loading.
_PR_VALIDATION_DIR = Path(__file__).resolve().parent / "pr_validation"
sys.path.insert(0, str(_PR_VALIDATION_DIR))
_POST_RESULTS_PATH = _PR_VALIDATION_DIR / "post_results.py"
_spec = importlib.util.spec_from_file_location(
    "pr_validation_post_results", _POST_RESULTS_PATH
)
post_results = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(post_results)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MERGE_SHA = "a" * 40
_HEAD_SHA = "b" * 40
_LKG_SHA = "c" * 40
_DS_SHA = "d" * 40
_RUN_URL = "https://github.com/leanprover-community/downstream-reports/actions/runs/1"


def _make_result(**overrides) -> dict:
    base = {
        "status": "pass",
        "stage": "build",
        "message": "ok",
        "downstream": "FLT",
        "merge_sha": _MERGE_SHA,
        "downstream_sha": _DS_SHA,
        "mode": "merge",
    }
    base.update(overrides)
    return base


def _render(result: dict, *, history: list | None = None) -> str:
    return post_results.render_body(
        name="FLT",
        repo="leanprover-community/FLT",
        default_branch="main",
        result=result,
        head_sha=_HEAD_SHA,
        merge_sha=_MERGE_SHA,
        run_url=_RUN_URL,
        log_tail="some build error",
        history=history
        or [
            post_results.make_history_entry(
                head_sha=_HEAD_SHA,
                merge_sha=_MERGE_SHA,
                status=result.get("status", "infra_failure"),
                run_url=_RUN_URL,
                downstream_sha=_DS_SHA,
                mode=result.get("mode") or "merge",
                lkg_commit=result.get("lkg_commit"),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Merge-mode (existing behaviour)
# ---------------------------------------------------------------------------


class MergeModeRenderingTests(unittest.TestCase):
    """Existing merge-mode comments must continue rendering as before."""

    def test_pass_header(self) -> None:
        """Scenario: merge-mode pass uses the historical headline."""
        body = _render(_make_result(status="pass"))
        self.assertIn("### ✅ FLT — builds against this PR", body)
        self.assertNotIn("rebased onto LKG", body)
        # Master-baseline caveat is still shown for merge-mode runs.
        self.assertIn("did not baseline against master", body)

    def test_fail_inlines_log_tail(self) -> None:
        """Scenario: merge-mode fail inlines the build.log tail in a <details> block."""
        body = _render(_make_result(status="fail"))
        self.assertIn("### ❌ FLT — fails against this PR", body)
        self.assertIn("<details><summary>failure log</summary>", body)
        self.assertIn("some build error", body)


# ---------------------------------------------------------------------------
# LKG-mode comment variants
# ---------------------------------------------------------------------------


class LkgModeRenderingTests(unittest.TestCase):
    """Headers, tested-line, and caveats for the new mode=lkg variants."""

    def test_lkg_pass_header_and_caveat(self) -> None:
        """Scenario: lkg-mode pass calls out the rebase explicitly."""
        body = _render(
            _make_result(status="pass", mode="lkg", lkg_commit=_LKG_SHA)
        )
        self.assertIn(
            "### ✅ FLT — builds against this PR rebased onto LKG", body
        )
        # Master caveat is replaced by the rebase note.
        self.assertNotIn("did not baseline against master", body)
        self.assertIn(
            "rebased the PR's commits onto", body,
        )
        # The LKG SHA is linked, not the merge SHA.
        self.assertIn(_LKG_SHA[:7], body)

    def test_lkg_fail_header(self) -> None:
        """Scenario: lkg-mode fail surfaces the rebase context in the header."""
        body = _render(
            _make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA)
        )
        self.assertIn(
            "### ❌ FLT — fails against this PR rebased onto LKG", body
        )
        self.assertIn("<details><summary>failure log</summary>", body)

    def test_rebase_conflict_header_and_explainer(self) -> None:
        """Scenario: rebase_conflict infra-failure surfaces a dedicated headline."""
        body = _render(
            _make_result(
                status="infra_failure",
                stage="rebase_conflict",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                message=(
                    "PR commits do not apply on top of LKG "
                    f"{_LKG_SHA}; this PR likely depends on post-LKG "
                    "mathlib changes"
                ),
            )
        )
        self.assertIn(
            "### ⚠️ FLT — could not validate (PR conflicts with LKG)",
            body,
        )
        self.assertIn(
            "do not apply cleanly on top of FLT's last-known-good", body
        )
        # Generic "infrastructure failure" boilerplate must NOT appear; this
        # is an actionable signal, not pure infra noise.
        self.assertNotIn(
            "This is an infrastructure failure; it does not imply", body
        )

    def test_mathlib_build_at_lkg_header_and_log(self) -> None:
        """Scenario: mathlib_build_at_lkg surfaces the headline and inlines the build log."""
        body = _render(
            _make_result(
                status="infra_failure",
                stage="mathlib_build_at_lkg",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                message="mathlib failed to build with this PR rebased onto LKG",
            )
        )
        self.assertIn(
            "### ⚠️ FLT — could not validate (mathlib build failed at LKG)",
            body,
        )
        self.assertIn("<details><summary>mathlib build log</summary>", body)
        self.assertIn("some build error", body)


# ---------------------------------------------------------------------------
# History line rendering
# ---------------------------------------------------------------------------


class HistoryLineTests(unittest.TestCase):
    """render_history_line should annotate lkg-mode entries with the LKG SHA."""

    def test_merge_mode_history_line(self) -> None:
        """Scenario: a legacy entry without `mode` renders as merge mode (no annotation)."""
        entry = {
            "head_sha": _HEAD_SHA,
            "status": "pass",
            "run_url": _RUN_URL,
            "downstream_sha": _DS_SHA,
        }
        line = post_results.render_history_line(
            entry, "leanprover-community/FLT", "main"
        )
        self.assertIn("✅", line)
        self.assertIn("passed against `leanprover-community/FLT@main`", line)
        self.assertNotIn("rebased onto LKG", line)

    def test_lkg_mode_history_line_with_commit(self) -> None:
        """Scenario: an lkg-mode entry annotates the LKG short SHA inline."""
        entry = {
            "head_sha": _HEAD_SHA,
            "status": "pass",
            "run_url": _RUN_URL,
            "downstream_sha": _DS_SHA,
            "mode": "lkg",
            "lkg_commit": _LKG_SHA,
        }
        line = post_results.render_history_line(
            entry, "leanprover-community/FLT", "main"
        )
        self.assertIn(f"rebased onto LKG `{_LKG_SHA[:7]}`", line)

    def test_lkg_mode_history_line_without_commit(self) -> None:
        """Scenario: an lkg-mode entry without commit (e.g. lkg_missing) still annotates."""
        entry = {
            "head_sha": _HEAD_SHA,
            "status": "infra_failure",
            "run_url": _RUN_URL,
            "downstream_sha": None,
            "mode": "lkg",
        }
        line = post_results.render_history_line(
            entry, "leanprover-community/FLT", "main"
        )
        self.assertIn("rebased onto LKG", line)
        self.assertIn("⚠️", line)


if __name__ == "__main__":
    unittest.main()
