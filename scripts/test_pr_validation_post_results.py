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
        """Scenario: merge-mode pass uses the green-check headline and the master caveat."""
        body = _render(_make_result(status="pass"))
        self.assertIn("### ✅ FLT builds against this PR", body)
        self.assertIn("did not baseline against master", body)

    def test_fail_inlines_log_tail(self) -> None:
        """Scenario: merge-mode fail inlines the build.log tail in a <details> block."""
        body = _render(_make_result(status="fail"))
        self.assertIn("### ❌ FLT fails against this PR", body)
        self.assertIn("<details><summary>failure log</summary>", body)
        self.assertIn("some build error", body)


# ---------------------------------------------------------------------------
# LKG-mode comment variants
# ---------------------------------------------------------------------------


class LkgModeRenderingTests(unittest.TestCase):
    """Headers, subtitle, and caveats for the mode=lkg variants."""

    def test_lkg_pass_header_and_subtitle(self) -> None:
        """Scenario: lkg-mode pass header reads as a sentence and the subtitle explains the verdict."""
        body = _render(
            _make_result(status="pass", mode="lkg", lkg_commit=_LKG_SHA)
        )
        self.assertIn(
            "### ✅ FLT builds against this PR rebased onto LKG", body
        )
        self.assertIn(
            "replayed the PR's changes on top of a mathlib revision"
            " compatible with FLT",
            body,
        )
        # The LKG SHA is linked in the Tested: paragraph.
        self.assertIn(_LKG_SHA[:7], body)

    def test_lkg_fail_header(self) -> None:
        """Scenario: lkg-mode fail header carries the rebased-onto-LKG suffix."""
        body = _render(
            _make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA)
        )
        self.assertIn(
            "### ❌ FLT fails against this PR rebased onto LKG", body
        )
        self.assertIn("<details><summary>failure log</summary>", body)

    def test_rebase_conflict_header_and_explainer(self) -> None:
        """Scenario: rebase_conflict infra-failure renders a dedicated headline."""
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
            "### ⚠️ FLT: could not validate (PR conflicts with LKG)",
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
            "### ⚠️ FLT: could not validate (mathlib build failed at LKG)",
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
    """The 'Tested:' paragraph spells out the rebase recipe."""

    def test_lkg_recipe_includes_count_compare_link_and_lkg(self) -> None:
        """Scenario: lkg pass renders commit count, compare URL, and LKG commit link."""
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
        # The Tested anchor users skim for.
        self.assertIn("**Tested:**", body)
        # Commit count is named explicitly.
        self.assertIn("4 PR commit(s)", body)
        # Compare URL with both endpoints' SHAs in the path.
        self.assertIn(f"/compare/{_PR_BASE}..{_PR_HEAD}", body)
        # LKG commit link.
        self.assertIn(f"/commit/{_LKG_SHA}", body)
        # The post-cherry-pick synthetic tree SHA lives only on the runner
        # disk, so the recipe omits it; readers see the LKG anchor and the
        # PR compare URL, both of which point at refs they can browse.
        self.assertNotIn(_REPLAYED[:7], body)

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
        self.assertIn("**Tested:**", body)
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
        self.assertIn("**Tested:**", body)
        self.assertIn("the PR's merge tree", body)
        self.assertIn(f"/commit/{_PR_HEAD}", body)
        self.assertIn(f"/commit/{_PR_BASE}", body)
        self.assertIn("4 commit(s) over base", body)

    def test_subtitle_appears_above_tested_line(self) -> None:
        """Scenario: the 'replayed the PR's changes' subtitle reads before the Tested: line."""
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
        subtitle_idx = body.find("replayed the PR's changes on top of")
        tested_idx = body.find("**Tested:**")
        log_idx = body.find("<details><summary>failure log")
        self.assertGreater(subtitle_idx, 0)
        self.assertGreater(tested_idx, 0)
        self.assertGreater(log_idx, 0)
        self.assertLess(subtitle_idx, tested_idx, "Subtitle should precede Tested:")
        self.assertLess(tested_idx, log_idx, "Tested: should precede the failure log")

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
# Self-contained body invariants
# ---------------------------------------------------------------------------


class SelfContainedBodyTests(unittest.TestCase):
    """Each comment body is a complete, standalone Markdown render.

    A dispatch POSTs one comment per matrix entry; the body carries every
    field a reader needs (header + subtitle + Tested recipe + optional
    failure log). These tests pin the invariant that no hidden marker or
    cross-comment scaffolding leaks into the rendered body.
    """

    def test_body_contains_no_hidden_html_marker(self) -> None:
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

    def test_body_renders_a_single_run_without_a_history_block(self) -> None:
        """Scenario: the body contains the current verdict only — no `Previous runs` list."""
        body = _render(_make_result(status="fail"))
        self.assertNotIn("**Previous runs**", body)


if __name__ == "__main__":
    unittest.main()
