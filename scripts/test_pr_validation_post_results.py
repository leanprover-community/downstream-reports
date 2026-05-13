#!/usr/bin/env python3
"""Tests for scripts/pr_validation/post_results.py rendering helpers.

Two layers are covered:

* :func:`render_entry_section` — one downstream's verdict block; the unit
  used inside the dispatch body.
* :func:`render_dispatch_body` — the full single-comment-per-dispatch
  assembly with optional @-mention, title, summary table, and stacked
  sections.

The GitHub REST plumbing around these is exercised manually during smoke
runs rather than with `gh api` mocks.
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
    """Render a single entry section in isolation (no dispatch wrapper)."""
    return post_results.render_entry_section(
        name=result.get("downstream", "FLT"),
        repo="leanprover-community/FLT",
        default_branch="main",
        result=result,
        merge_sha=_MERGE_SHA,
        run_url=_RUN_URL,
        log_tail="some build error",
    )


def _make_entry(result: dict, log_tail: str = "") -> dict:
    """Wrap a result.json into the shape ``render_dispatch_body`` expects."""
    return {
        "name": result.get("downstream", "FLT"),
        "repo": "leanprover-community/FLT",
        "default_branch": "main",
        "result": result,
        "log_tail": log_tail,
    }


# ---------------------------------------------------------------------------
# Entry-label helper
# ---------------------------------------------------------------------------


class EntryLabelTests(unittest.TestCase):
    """`entry_label` round-trips the `!downstream-check` grammar."""

    def test_lkg_bare(self) -> None:
        """Scenario: LKG mode with no rev is the bare downstream name."""
        self.assertEqual(post_results.entry_label("FLT", "lkg"), "FLT")

    def test_lkg_with_rev(self) -> None:
        """Scenario: a rev attaches as `name@rev`."""
        self.assertEqual(
            post_results.entry_label("FLT", "lkg", rev="v1.2.3"),
            "FLT@v1.2.3",
        )

    def test_merge_bare(self) -> None:
        """Scenario: merge mode appends the explicit `--merge-branch` flag."""
        self.assertEqual(
            post_results.entry_label("FLT", "merge"),
            "FLT --merge-branch",
        )

    def test_merge_with_rev(self) -> None:
        """Scenario: rev + merge yields `name@rev --merge-branch`."""
        self.assertEqual(
            post_results.entry_label("FLT", "merge", rev="v1.2.3"),
            "FLT@v1.2.3 --merge-branch",
        )


# ---------------------------------------------------------------------------
# Merge-mode (existing behaviour)
# ---------------------------------------------------------------------------


class MergeModeRenderingTests(unittest.TestCase):
    """Merge-mode entry sections carry the `--merge-branch` token in the heading."""

    def test_pass_header(self) -> None:
        """Scenario: a merge-mode pass renders the `## ✅` header with `--merge-branch`; no master caveat."""
        body = _render(_make_result(status="pass"))
        self.assertIn(
            "## ✅ FLT --merge-branch builds against this PR", body
        )
        # A clean merge-mode pass is unambiguous — no subtitle disclaimer.
        self.assertNotIn("did not baseline against master", body)
        self.assertNotIn("mathlib master is currently", body)

    def test_fail_inlines_log_tail(self) -> None:
        """Scenario: merge-mode fail inlines the build.log tail in a <details> block."""
        body = _render(_make_result(status="fail"))
        self.assertIn(
            "## ❌ FLT --merge-branch fails against this PR", body
        )
        self.assertIn("<details><summary>failure log</summary>", body)
        self.assertIn("some build error", body)


# ---------------------------------------------------------------------------
# LKG-mode comment variants
# ---------------------------------------------------------------------------


class LkgModeRenderingTests(unittest.TestCase):
    """Headers, subtitle, and caveats for the mode=lkg variants."""

    def test_lkg_pass_header_and_no_subtitle_without_fkb(self) -> None:
        """Scenario: a clean lkg-mode pass with no FKB skips the framing subtitle."""
        body = _render(
            _make_result(status="pass", mode="lkg", lkg_commit=_LKG_SHA)
        )
        self.assertIn(
            "## ✅ FLT builds against this PR rebased onto LKG", body
        )
        # The LKG SHA is still linked in the Tested: paragraph.
        self.assertIn(_LKG_SHA[:7], body)
        # No subtitle — clean pass + healthy master is unambiguous.
        self.assertNotIn("independent of current mathlib master health", body)

    def test_lkg_fail_header(self) -> None:
        """Scenario: lkg-mode fail header carries the rebased-onto-LKG suffix."""
        body = _render(
            _make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA)
        )
        self.assertIn(
            "## ❌ FLT fails against this PR rebased onto LKG", body
        )
        self.assertIn("<details><summary>failure log</summary>", body)

    def test_lkg_fail_keeps_framing(self) -> None:
        """Scenario: lkg fail still emits the rebased-on-LKG subtitle so the verdict is interpretable."""
        body = _render(
            _make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA)
        )
        self.assertIn(
            "replayed the PR's changes on top of a mathlib revision"
            " compatible with FLT",
            body,
        )

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
            "## ⚠️ FLT: could not validate (PR conflicts with LKG)",
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
            "## ⚠️ FLT: could not validate (mathlib build failed at LKG)",
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
        """Scenario: a pre-cherry-pick infra failure surfaces the LKG anchor under an `Attempted:` label."""
        body = _render(
            _make_result(
                status="infra_failure",
                stage="fetch",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                # No pr_base_sha / pr_head_sha — fetch failed before resolution.
            )
        )
        # The run stopped before any build happened; the recipe describes
        # the intent rather than claiming success.
        self.assertIn("**Attempted:**", body)
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

    def test_lkg_infra_failure_uses_attempted_label_and_gerunds(self) -> None:
        """Scenario: lkg-mode infra failure renders the recipe in gerund form under Attempted:."""
        body = _render(
            _make_result(
                status="infra_failure",
                stage="rebase_conflict",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=2,
            )
        )
        self.assertIn("**Attempted:**", body)
        self.assertIn("cherry-picking 2 PR commit(s)", body)
        self.assertIn("and building against", body)
        # The past-tense forms describe a completed build and must not
        # appear when the build did not complete.
        self.assertNotIn("**Tested:**", body)
        self.assertNotIn("cherry-picked onto", body)

    def test_merge_infra_failure_uses_attempted_label(self) -> None:
        """Scenario: merge-mode infra failure renders the recipe in gerund form."""
        body = _render(
            _make_result(
                status="infra_failure",
                stage="clone_downstream",
                mode="merge",
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=1,
                # No downstream_sha — the clone step never produced one.
                downstream_sha=None,
            )
        )
        self.assertIn("**Attempted:**", body)
        # Subject-verb order in merge mode flips: we lead with `building <ds>`
        # because the downstream is the target of the action.
        self.assertRegex(body, r"\bbuilding\b.*\bagainst the PR's merge tree\b")
        self.assertNotIn("**Tested:**", body)
        self.assertNotIn("built against", body)

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


_FKB_SHA = "f" * 40


class FkbAwareFramingTests(unittest.TestCase):
    """When the snapshot records a first_known_bad_commit, the subtitle states master health definitively."""

    def test_merge_fail_with_fkb_names_the_regression(self) -> None:
        """Scenario: merge fail + FKB set links to the regression commit and recommends LKG mode."""
        body = _render(
            _make_result(
                status="fail",
                mode="merge",
                pr_base_sha="1" * 40,
                pr_head_sha="2" * 40,
                commits_replayed=1,
                fkb_commit=_FKB_SHA,
            )
        )
        self.assertIn("mathlib master is currently incompatible with FLT", body)
        self.assertIn(f"/commit/{_FKB_SHA}", body)
        self.assertIn("Drop `--merge-branch`", body)

    def test_merge_fail_without_fkb_attributes_failure_to_pr(self) -> None:
        """Scenario: merge fail with no FKB recorded says master is healthy and points at the PR."""
        body = _render(
            _make_result(
                status="fail",
                mode="merge",
                pr_base_sha="1" * 40,
                pr_head_sha="2" * 40,
                commits_replayed=1,
            )
        )
        self.assertIn(
            "mathlib master is currently known to build with FLT", body
        )
        self.assertIn("attributable to the PR", body)

    def test_merge_pass_has_no_master_caveat(self) -> None:
        """Scenario: a successful merge-mode build needs no subtitle disclaimer."""
        body = _render(
            _make_result(
                status="pass",
                mode="merge",
                pr_base_sha="1" * 40,
                pr_head_sha="2" * 40,
                commits_replayed=1,
            )
        )
        self.assertNotIn("mathlib master is currently", body)
        self.assertNotIn("did not baseline against master", body)

    def test_lkg_pass_with_fkb_explains_why_lkg_matters(self) -> None:
        """Scenario: lkg pass + FKB set names the master regression to sharpen the verdict."""
        body = _render(
            _make_result(
                status="pass",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                fkb_commit=_FKB_SHA,
            )
        )
        self.assertIn(
            "Current mathlib master is incompatible with FLT", body
        )
        self.assertIn(f"/commit/{_FKB_SHA}", body)
        self.assertIn("purely be about the PR's effect on FLT", body)

    def test_lkg_pass_without_fkb_skips_subtitle(self) -> None:
        """Scenario: lkg pass with no FKB skips the subtitle entirely (clean verdict)."""
        body = _render(
            _make_result(
                status="pass",
                mode="lkg",
                lkg_commit=_LKG_SHA,
            )
        )
        # No subtitle at all — the recipe + section header speak for themselves.
        self.assertNotIn("independent of current mathlib master health", body)
        self.assertNotIn("Current mathlib master is incompatible", body)


# ---------------------------------------------------------------------------
# Dispatch-level body (single comment for all entries)
# ---------------------------------------------------------------------------


class DispatchBodyTests(unittest.TestCase):
    """`render_dispatch_body` assembles all entries into one comment."""

    def _render_dispatch(self, entries: list[dict], **kwargs) -> str:
        return post_results.render_dispatch_body(
            entries=entries,
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
            **kwargs,
        )

    def test_title_links_merge_sha_and_run(self) -> None:
        """Scenario: the dispatch title carries the merge SHA + run link."""
        body = self._render_dispatch([_make_entry(_make_result(status="pass"))])
        self.assertIn("# Downstream validation against PR merge", body)
        self.assertIn(f"/commit/{_MERGE_SHA}", body)
        self.assertIn(_RUN_URL, body)

    def test_single_entry_skips_summary_table(self) -> None:
        """Scenario: a one-entry dispatch needs no table — the section is the whole story."""
        body = self._render_dispatch([_make_entry(_make_result(status="pass"))])
        self.assertNotIn("| Entry | Verdict |", body)

    def test_multiple_entries_render_summary_table(self) -> None:
        """Scenario: 2+ entries render a leading table with one row each, in stable order."""
        entries = [
            _make_entry(
                _make_result(
                    downstream="Toric", status="pass", mode="lkg",
                    lkg_commit=_LKG_SHA,
                )
            ),
            _make_entry(
                _make_result(
                    downstream="Toric", status="pass", mode="merge",
                )
            ),
            _make_entry(
                _make_result(
                    downstream="carleson", status="fail", mode="merge",
                    fkb_commit=_FKB_SHA,
                ),
                log_tail="some build error",
            ),
        ]
        body = self._render_dispatch(entries)
        self.assertIn("| Entry | Verdict |", body)
        self.assertIn("|---|---|", body)
        # Entry label (backticked) shows up for each row.
        self.assertIn("`Toric`", body)
        self.assertIn("`Toric --merge-branch`", body)
        self.assertIn("`carleson --merge-branch`", body)
        # The table precedes the per-entry sections.
        table_idx = body.find("| Entry | Verdict |")
        first_section_idx = body.find("## ")
        self.assertGreater(first_section_idx, table_idx)

    def test_each_entry_renders_as_a_section(self) -> None:
        """Scenario: every entry produces its own `## ` section in the body."""
        entries = [
            _make_entry(
                _make_result(downstream="A", status="pass", mode="lkg", lkg_commit=_LKG_SHA)
            ),
            _make_entry(
                _make_result(downstream="B", status="pass", mode="merge")
            ),
        ]
        body = self._render_dispatch(entries)
        self.assertIn("## ✅ A builds against this PR rebased onto LKG", body)
        self.assertIn("## ✅ B --merge-branch builds against this PR", body)

    def test_mention_renders_once_at_top(self) -> None:
        """Scenario: `triggered_by` produces a single `_Requested by @<user>._` line above the title."""
        body = self._render_dispatch(
            [
                _make_entry(_make_result(status="pass", downstream="A")),
                _make_entry(_make_result(status="pass", downstream="B")),
            ],
            triggered_by="marcelolynch",
        )
        # Exactly one mention line.
        self.assertEqual(body.count("_Requested by @marcelolynch._"), 1)
        mention_idx = body.find("_Requested by @marcelolynch._")
        title_idx = body.find("# Downstream validation")
        self.assertGreaterEqual(mention_idx, 0)
        self.assertLess(mention_idx, title_idx)

    def test_no_mention_when_triggered_by_empty(self) -> None:
        """Scenario: empty `triggered_by` omits the mention line entirely."""
        body = self._render_dispatch(
            [_make_entry(_make_result(status="pass"))],
            triggered_by="",
        )
        self.assertNotIn("_Requested by", body)


# ---------------------------------------------------------------------------
# Verdict gloss for the summary table
# ---------------------------------------------------------------------------


class VerdictSummaryTests(unittest.TestCase):
    """`verdict_summary` produces the one-line gloss for each table row."""

    def test_lkg_pass(self) -> None:
        """Scenario: an lkg-mode pass advertises the LKG rebase."""
        self.assertEqual(
            post_results.verdict_summary(
                _make_result(status="pass", mode="lkg", lkg_commit=_LKG_SHA)
            ),
            "✅ builds (rebased onto LKG)",
        )

    def test_merge_pass(self) -> None:
        """Scenario: a merge-mode pass is the plain verdict."""
        self.assertEqual(
            post_results.verdict_summary(_make_result(status="pass", mode="merge")),
            "✅ builds",
        )

    def test_lkg_fail_attributes_to_pr(self) -> None:
        """Scenario: an lkg-mode fail is unambiguously the PR's fault."""
        self.assertEqual(
            post_results.verdict_summary(
                _make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA)
            ),
            "❌ fails (attributable to the PR)",
        )

    def test_merge_fail_with_fkb_names_regression(self) -> None:
        """Scenario: a merge fail with FKB recorded calls out the master regression."""
        summary = post_results.verdict_summary(
            _make_result(status="fail", mode="merge", fkb_commit=_FKB_SHA)
        )
        self.assertIn("master incompatibility at", summary)
        self.assertIn(f"/commit/{_FKB_SHA}", summary)

    def test_merge_fail_without_fkb_attributes_to_pr(self) -> None:
        """Scenario: a merge fail with no FKB attributes to the PR."""
        self.assertEqual(
            post_results.verdict_summary(
                _make_result(status="fail", mode="merge")
            ),
            "❌ fails (attributable to the PR)",
        )

    def test_rebase_conflict_gloss(self) -> None:
        """Scenario: rebase_conflict gets a dedicated warning gloss."""
        self.assertEqual(
            post_results.verdict_summary(
                _make_result(
                    status="infra_failure",
                    stage="rebase_conflict",
                    mode="lkg",
                )
            ),
            "⚠️ could not validate (PR conflicts with LKG)",
        )

    def test_mathlib_build_at_lkg_gloss(self) -> None:
        """Scenario: mathlib_build_at_lkg has its own gloss."""
        self.assertEqual(
            post_results.verdict_summary(
                _make_result(
                    status="infra_failure",
                    stage="mathlib_build_at_lkg",
                    mode="lkg",
                )
            ),
            "⚠️ could not validate (mathlib build failed at LKG)",
        )

    def test_generic_infra_failure_names_stage(self) -> None:
        """Scenario: a generic infra failure names the offending stage."""
        self.assertEqual(
            post_results.verdict_summary(
                _make_result(
                    status="infra_failure",
                    stage="clone_downstream",
                    mode="merge",
                )
            ),
            "⚠️ could not validate (clone_downstream)",
        )


# ---------------------------------------------------------------------------
# Size budgeting
# ---------------------------------------------------------------------------


class SizeBudgetTests(unittest.TestCase):
    """The dispatch body fits under the GitHub PR-comment limit."""

    def test_body_within_limit_for_typical_dispatch(self) -> None:
        """Scenario: a 4-entry dispatch with two failures stays under the comment limit."""
        # 8k of repeated junk per failure log is realistic.
        log = ("oops oops " * 1000)[:8_000]
        entries = [
            _make_entry(_make_result(downstream="A", status="pass", mode="lkg", lkg_commit=_LKG_SHA)),
            _make_entry(
                _make_result(downstream="A", status="fail", mode="merge", fkb_commit=_FKB_SHA),
                log_tail=log,
            ),
            _make_entry(_make_result(downstream="B", status="pass", mode="lkg", lkg_commit=_LKG_SHA)),
            _make_entry(
                _make_result(downstream="B", status="fail", mode="merge"),
                log_tail=log,
            ),
        ]
        body = post_results._shrink_to_fit(
            entries=entries,
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
            triggered_by="marcelolynch",
        )
        self.assertLessEqual(len(body), post_results.COMMENT_MAX_CHARS)

    def test_oversized_logs_shrink_until_body_fits(self) -> None:
        """Scenario: pathologically large logs are halved until the body fits the limit."""
        huge_log = "x" * 200_000
        entries = [
            _make_entry(
                _make_result(downstream="A", status="fail", mode="merge"),
                log_tail=huge_log,
            ),
            _make_entry(
                _make_result(downstream="B", status="fail", mode="merge"),
                log_tail=huge_log,
            ),
        ]
        body = post_results._shrink_to_fit(
            entries=entries,
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
            triggered_by="",
        )
        self.assertLessEqual(len(body), post_results.COMMENT_MAX_CHARS)
        # Both sections still render with their headers.
        self.assertIn("## ❌ A --merge-branch fails", body)
        self.assertIn("## ❌ B --merge-branch fails", body)


# ---------------------------------------------------------------------------
# Self-contained body invariants
# ---------------------------------------------------------------------------


class SelfContainedBodyTests(unittest.TestCase):
    """No hidden markers or cross-dispatch scaffolding leak into a rendered section."""

    def test_section_contains_no_hidden_html_marker(self) -> None:
        """Scenario: render_entry_section emits no `<!-- pr-check-downstream:* -->` blocks."""
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

    def test_section_renders_a_single_run_without_a_history_block(self) -> None:
        """Scenario: the section contains the current verdict only — no `Previous runs` list."""
        body = _render(_make_result(status="fail"))
        self.assertNotIn("**Previous runs**", body)


if __name__ == "__main__":
    unittest.main()
