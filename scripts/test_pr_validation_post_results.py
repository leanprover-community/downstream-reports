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


def _render(result: dict) -> str:
    return post_results.render_body(
        name="FLT",
        repo="leanprover-community/FLT",
        default_branch="main",
        result=result,
        merge_sha=_MERGE_SHA,
        run_url=_RUN_URL,
        log_tail="some build error",
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
# Test-tree paragraph (the explicit recipe)
# ---------------------------------------------------------------------------


_PR_BASE = "1" * 40
_PR_HEAD = "2" * 40
_REPLAYED = "3" * 40


class TestTreeRecipeTests(unittest.TestCase):
    """The 'What this run tested' paragraph spells out the rebase recipe."""

    def test_lkg_recipe_includes_count_compare_link_and_replayed_tree(self) -> None:
        """Scenario: lkg pass renders commit count, compare URL, LKG link, replayed-tree link."""
        body = _render(
            _make_result(
                status="pass",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=4,
                replayed_tree_sha=_REPLAYED,
            )
        )
        # The literal "What this run tested" header is the anchor users skim for.
        self.assertIn("**What this run tested:**", body)
        # Commit count is named explicitly.
        self.assertIn("4 PR commit(s)", body)
        # Compare URL with both endpoints' SHAs in the path.
        self.assertIn(f"/compare/{_PR_BASE}..{_PR_HEAD}", body)
        # LKG commit link.
        self.assertIn(f"/commit/{_LKG_SHA}", body)
        # Resulting tree SHA appears explicitly so the run is reproducible.
        self.assertIn("resulting tree", body)
        self.assertIn(_REPLAYED[:7], body)

    def test_lkg_recipe_omits_compare_link_when_endpoints_unknown(self) -> None:
        """Scenario: a pre-cherry-pick infra failure still surfaces the LKG anchor without compare URL."""
        body = _render(
            _make_result(
                status="infra_failure",
                stage="fetch",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                # No pr_base_sha / pr_head_sha — fetch failed before resolution.
            )
        )
        self.assertIn("**What this run tested:**", body)
        self.assertIn(f"/commit/{_LKG_SHA}", body)
        self.assertNotIn("/compare/", body)

    def test_merge_recipe_includes_head_base_and_count(self) -> None:
        """Scenario: merge mode shows the merge tree's head + base + commit count."""
        body = _render(
            _make_result(
                status="pass",
                mode="merge",
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=4,
            )
        )
        self.assertIn("**What this run tested:**", body)
        self.assertIn("the PR's merge tree", body)
        self.assertIn(f"/commit/{_PR_HEAD}", body)
        self.assertIn(f"/commit/{_PR_BASE}", body)
        self.assertIn("4 commit(s) over base", body)

    def test_explainer_appears_above_failure_log(self) -> None:
        """Scenario: the 'rebased onto LKG' explainer reads before the failure log, not after it."""
        body = _render(
            _make_result(
                status="fail",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=2,
            )
        )
        explainer_idx = body.find("rebased the PR's commits onto")
        log_idx = body.find("<details><summary>failure log")
        self.assertGreater(explainer_idx, 0)
        self.assertGreater(log_idx, 0)
        self.assertLess(
            explainer_idx,
            log_idx,
            "Explainer should appear before the failure log so it's not missed.",
        )

    def test_recipe_surfaces_requested_rev(self) -> None:
        """Scenario: when the result records `downstream_rev`, the recipe link uses it as label."""
        body = _render(
            _make_result(
                status="pass",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=1,
                downstream_rev="v1.2.3",
            )
        )
        # Link label includes the rev, not just the short SHA.
        self.assertIn(
            "[`leanprover-community/FLT@v1.2.3`]"
            f"(https://github.com/leanprover-community/FLT/commit/{_DS_SHA})",
            body,
        )


# ---------------------------------------------------------------------------
# No-marker behaviour
# ---------------------------------------------------------------------------


class NoMarkerTests(unittest.TestCase):
    """Comments are self-contained — no hidden marker or history block.

    We dropped the edit-in-place machinery: each dispatch POSTs fresh
    comments, so a body that still leaked the old `<!-- … -->` markers
    would be visible to readers as dead text. These tests pin the
    invariant.
    """

    def test_body_does_not_contain_pr_check_downstream_marker(self) -> None:
        """Scenario: render_body emits no `<!-- pr-check-downstream:* -->` blocks."""
        body = _render(
            _make_result(
                status="pass",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                downstream_rev="v1.2.3",
            )
        )
        self.assertNotIn("<!-- pr-check-downstream:result:", body)
        self.assertNotIn("<!-- pr-check-downstream:history-data", body)

    def test_body_does_not_contain_previous_runs_section(self) -> None:
        """Scenario: there is no `Previous runs` block — that was history-only chrome."""
        body = _render(_make_result(status="fail"))
        self.assertNotIn("**Previous runs**", body)


if __name__ == "__main__":
    unittest.main()
