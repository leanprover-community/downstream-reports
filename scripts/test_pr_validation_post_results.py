#!/usr/bin/env python3
"""
Tests for: scripts.pr_validation.post_results

Coverage scope:
    - ``entry_label`` — round-trips the `!downstream-check` grammar
      (bare name / `@<rev>` / ` --merge-branch`) so the displayed
      entry token matches the user's request.
    - ``verdict_summary`` — one-line gloss for each (mode, status,
      stage, FKB?) tuple that the summary table cell can carry.
    - ``render_entry_section`` — one downstream's ``##`` section:
      header + optional framing subtitle + ``Tested:`` / ``Attempted:``
      recipe + optional inline log.
    - ``render_dispatch_body`` — assembly of the single dispatch-level
      comment (mention line, title, optional summary table, stacked
      sections).
    - ``_shrink_to_fit`` — the budgeter that progressively trims log
      tails until the body fits under GitHub's PR-comment limit.

Out of scope:
    - ``post_comment`` — the gh-api invocation is exercised manually
      during smoke runs rather than mocked here.
    - The actual `lake build` log content past the snippets we use as
      log_tail fixtures; ``log_filter`` covers the filtering side.

Why this matters
----------------
post_results.py is the only place that turns a pile of result.json
artifacts into a human-readable verdict on the PR.  A regression in
the framing subtitle would silently mislead the PR author about
whether a failure was master's or the PR's fault; a regression in the
size budget would overflow GitHub's 65 536-char comment limit and
either crash the report job or chop the failure log mid-line.  The
entry-label round-trip is what makes a slug request render *as* a slug
in the table — otherwise the comment quietly switches to the
canonical short name and surprises the user.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.conftest import SHA_A, SHA_C, SHA_D, SHA_F
from scripts.pr_validation import post_results


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Semantic aliases for the SHA constants we share with the rest of the
# suite.  These names read naturally inside assertions about what the
# comment links to.
_MERGE_SHA = SHA_A
_LKG_SHA = SHA_C
_DS_SHA = SHA_D
_FKB_SHA = SHA_F

# PR commit-range endpoints (base..head).  These are local to the
# post_results suite — they're not generic merge SHAs but always
# represent the two parents of a PR's merge commit.
_PR_BASE = "1" * 40
_PR_HEAD = "2" * 40

_RUN_URL = "https://github.com/leanprover-community/downstream-reports/actions/runs/1"


def _make_result(**overrides) -> dict:
    """Build a result.json-shaped dict with the merge-mode pass defaults."""
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


class TestEntryLabel:
    """``entry_label`` round-trips the `!downstream-check` grammar."""

    def test_lkg_bare(self) -> None:
        """LKG mode with no rev is the bare downstream name.

        LKG is the implicit default, so the entry label collapses to
        just the name to match what the user typed.
        """
        # Arrange / Act / Assert
        assert post_results.entry_label("FLT", "lkg") == "FLT"

    def test_lkg_with_rev(self) -> None:
        """A rev attaches as ``name@rev``."""
        # Arrange / Act / Assert
        assert post_results.entry_label("FLT", "lkg", rev="v1.2.3") == "FLT@v1.2.3"

    def test_merge_bare(self) -> None:
        """Merge mode appends the explicit `--merge-branch` flag.

        The flag is preserved in the label because the user explicitly
        opted into merge mode; eliding it would make the displayed
        entry indistinguishable from the default LKG variant.
        """
        # Arrange / Act / Assert
        assert post_results.entry_label("FLT", "merge") == "FLT --merge-branch"

    def test_merge_with_rev(self) -> None:
        """Rev + merge yields ``name@rev --merge-branch``."""
        # Arrange / Act / Assert
        assert (
            post_results.entry_label("FLT", "merge", rev="v1.2.3")
            == "FLT@v1.2.3 --merge-branch"
        )


# ---------------------------------------------------------------------------
# Merge-mode (existing behaviour)
# ---------------------------------------------------------------------------


class TestMergeModeRendering:
    """Merge-mode entry sections carry the `--merge-branch` token in the heading."""

    def test_pass_header(self) -> None:
        """A merge-mode pass renders the ``## ✅`` header with `--merge-branch`; no master caveat.

        A clean merge-mode pass is unambiguous: the PR builds against the
        merged tree.  No framing subtitle is needed since there's no
        ambiguity about whose fault any non-existent failure would be —
        the master-health framings (which lead with ``mathlib master is
        currently …``) only fire on fails.
        """
        # Arrange / Act
        body = _render(_make_result(status="pass"))

        # Assert
        assert "## ✅ FLT --merge-branch builds against this PR" in body
        assert "mathlib master is currently" not in body

    def test_fail_inlines_log_tail(self) -> None:
        """Merge-mode fail inlines the build.log tail in a ``<details>`` block.

        The log tail is what makes a failure verdict actionable; we
        always inline it for failed builds so the PR author doesn't
        have to click through to the artifact.
        """
        # Arrange / Act
        body = _render(_make_result(status="fail"))

        # Assert
        assert "## ❌ FLT --merge-branch fails against this PR" in body
        assert "<details><summary>failure log</summary>" in body
        assert "some build error" in body


# ---------------------------------------------------------------------------
# LKG-mode comment variants
# ---------------------------------------------------------------------------


class TestLkgModeRendering:
    """Headers, subtitle, and caveats for the mode=lkg variants."""

    def test_lkg_pass_header_and_no_subtitle_without_fkb(self) -> None:
        """A clean lkg-mode pass with no FKB skips the framing subtitle.

        Pass + healthy master = unambiguous verdict; the recipe + section
        header already convey "rebased onto LKG" without needing extra
        prose.  The skip is what makes the dispatch comment readable on
        a happy-path multi-entry dispatch.
        """
        # Arrange / Act
        body = _render(_make_result(status="pass", mode="lkg", lkg_commit=_LKG_SHA))

        # Assert
        assert "## ✅ FLT builds against this PR rebased onto LKG" in body
        assert _LKG_SHA[:7] in body
        assert "independent of current mathlib master health" not in body

    def test_lkg_fail_header(self) -> None:
        """LKG-mode fail header carries the rebased-onto-LKG suffix.

        The suffix makes the verdict's framing immediately legible:
        "this fail is what your PR did to LKG", not "this fail is
        master's fault leaking through".
        """
        # Arrange / Act
        body = _render(_make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA))

        # Assert
        assert "## ❌ FLT fails against this PR rebased onto LKG" in body
        assert "<details><summary>failure log</summary>" in body

    def test_lkg_fail_keeps_framing(self) -> None:
        """LKG fail still emits the rebased-on-LKG subtitle so the verdict is interpretable.

        Unlike the pass case, fails always keep the framing — a reader
        needs to know "this fail is PR-attributable" rather than try to
        infer that from the rest of the comment.
        """
        # Arrange / Act
        body = _render(_make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA))

        # Assert
        assert (
            "replayed the PR's changes on top of a mathlib revision compatible with FLT"
            in body
        )

    def test_rebase_conflict_header_and_explainer(self) -> None:
        """`rebase_conflict` infra-failure renders a dedicated headline + actionable explainer.

        This stage means the PR's commits don't apply cleanly on top of
        LKG, which the PR author can usually fix by rebasing onto a
        more recent base.  The targeted explainer points at that
        diagnosis specifically.
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="infra_failure",
                stage="rebase_conflict",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                message=(
                    f"PR commits do not apply on top of LKG {_LKG_SHA}; "
                    "this PR likely depends on post-LKG mathlib changes"
                ),
            )
        )

        # Assert
        assert "## ⚠️ FLT: could not validate (PR conflicts with LKG)" in body
        assert "do not apply cleanly on top of FLT's last-known-good" in body

    def test_mathlib_build_at_lkg_header_and_log(self) -> None:
        """`mathlib_build_at_lkg` surfaces the headline and inlines the build log.

        Inlining the log here (unlike most infra failures) lets the PR
        author distinguish "my changes broke mathlib's library build"
        from a transient infra flake.
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="infra_failure",
                stage="mathlib_build_at_lkg",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                message="mathlib failed to build with this PR rebased onto LKG",
            )
        )

        # Assert
        assert (
            "## ⚠️ FLT: could not validate (mathlib build failed at LKG)" in body
        )
        assert "<details><summary>mathlib build log</summary>" in body
        assert "some build error" in body


# ---------------------------------------------------------------------------
# Test-tree paragraph (the explicit recipe)
# ---------------------------------------------------------------------------


class TestTestTreeRecipe:
    """The 'Tested:' paragraph spells out the rebase recipe."""

    def test_lkg_recipe_includes_count_compare_link_and_lkg(self) -> None:
        """LKG pass renders commit count, compare URL, and LKG commit link.

        The recipe is what makes the verdict reproducible by hand: a
        reader follows the compare link to see exactly which commits
        were replayed and the LKG link to see the anchor commit.
        Together with the downstream commit link below them, those are
        the three refs that fully describe what was built.
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="pass",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=4,
            )
        )

        # Assert
        assert "**Tested:**" in body
        assert "4 PR commit(s)" in body
        assert f"/compare/{_PR_BASE}..{_PR_HEAD}" in body
        assert f"/commit/{_LKG_SHA}" in body

    def test_lkg_recipe_omits_compare_link_when_endpoints_unknown(self) -> None:
        """A pre-cherry-pick infra failure surfaces the LKG anchor under an `Attempted:` label.

        When the validate step failed before resolving PR endpoints
        (e.g. a fetch failure), we have an LKG to anchor on but no
        commit range to compare.  The recipe describes the intent in
        gerund form to make clear the build did not complete.
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="infra_failure",
                stage="fetch",
                mode="lkg",
                lkg_commit=_LKG_SHA,
            )
        )

        # Assert
        assert "**Attempted:**" in body
        assert f"/commit/{_LKG_SHA}" in body
        assert "/compare/" not in body

    def test_merge_recipe_includes_head_base_and_count(self) -> None:
        """Merge mode shows the merge tree's head + base + commit count.

        The merge SHA alone is opaque — surfacing the head + base
        commits lets the PR author audit which tree GitHub actually
        merged.
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="pass",
                mode="merge",
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=4,
            )
        )

        # Assert
        assert "**Tested:**" in body
        assert "the PR's merge tree" in body
        assert f"/commit/{_PR_HEAD}" in body
        assert f"/commit/{_PR_BASE}" in body
        assert "4 commit(s) over base" in body

    def test_lkg_infra_failure_uses_attempted_label_and_gerunds(self) -> None:
        """LKG-mode infra failure renders the recipe in gerund form under `Attempted:`.

        Past-tense forms like "cherry-picked onto X, built against Y"
        would lie about what happened on an infra failure where no
        build completed.  The gerund switch keeps the recipe honest.
        """
        # Arrange / Act
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

        # Assert
        assert "**Attempted:**" in body
        assert "cherry-picking 2 PR commit(s)" in body
        assert "and building against" in body
        assert "**Tested:**" not in body
        assert "cherry-picked onto" not in body

    def test_merge_infra_failure_uses_attempted_label(self) -> None:
        """Merge-mode infra failure renders the recipe in gerund form.

        Subject-verb order in merge mode flips: we lead with
        "building <ds>" because the downstream is the target of the
        action (vs LKG mode where the commits are the subject).
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="infra_failure",
                stage="clone_downstream",
                mode="merge",
                pr_base_sha=_PR_BASE,
                pr_head_sha=_PR_HEAD,
                commits_replayed=1,
                downstream_sha=None,
            )
        )

        # Assert
        import re

        assert "**Attempted:**" in body
        assert re.search(r"\bbuilding\b.*\bagainst the PR's merge tree\b", body)
        assert "**Tested:**" not in body
        assert "built against" not in body

    def test_subtitle_appears_above_tested_line(self) -> None:
        """The 'replayed the PR's changes' subtitle reads before the ``Tested:`` line.

        Section flow is: header → subtitle (framing) → recipe → log.
        Reversing the subtitle and recipe would make the recipe read
        ahead of the framing that gives it meaning.
        """
        # Arrange / Act
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

        # Assert
        subtitle_idx = body.find("replayed the PR's changes on top of")
        tested_idx = body.find("**Tested:**")
        log_idx = body.find("<details><summary>failure log")
        assert subtitle_idx > 0
        assert tested_idx > 0
        assert log_idx > 0
        assert subtitle_idx < tested_idx, "Subtitle should precede Tested:"
        assert tested_idx < log_idx, "Tested: should precede the failure log"

    def test_recipe_surfaces_requested_rev(self) -> None:
        """When the result records ``downstream_rev``, the recipe link uses it as label.

        The recipe link's URL always points at the resolved SHA so the
        reader gets the exact tested tree; the link's label uses the
        user's requested rev so they see what they asked for.
        """
        # Arrange / Act
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

        # Assert
        assert (
            "[`leanprover-community/FLT@v1.2.3`]"
            f"(https://github.com/leanprover-community/FLT/commit/{_DS_SHA})"
            in body
        )


# ---------------------------------------------------------------------------
# FKB-aware framing
# ---------------------------------------------------------------------------


class TestFkbAwareFraming:
    """When the snapshot records a first_known_bad_commit, the subtitle states master health definitively."""

    def test_merge_fail_with_fkb_names_the_regression(self) -> None:
        """Merge fail + FKB set links to the regression commit and recommends LKG mode.

        FKB is positive knowledge that master is already broken for this
        downstream.  Surfacing that turns a "your PR broke X" message
        into "master broke X first, and the PR can't help that" — saving
        the PR author from chasing a phantom.
        """
        # Arrange / Act
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

        # Assert
        assert "mathlib master is currently incompatible with FLT" in body
        assert f"/commit/{_FKB_SHA}" in body
        assert "Drop `--merge-branch`" in body

    def test_merge_fail_without_fkb_attributes_failure_to_pr(self) -> None:
        """Merge fail with no FKB recorded says master is healthy and points at the PR.

        No FKB = the snapshot positively records master as building
        with X.  The failure is therefore PR-attributable and we say so
        unambiguously.
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="fail",
                mode="merge",
                pr_base_sha="1" * 40,
                pr_head_sha="2" * 40,
                commits_replayed=1,
            )
        )

        # Assert
        assert "mathlib master is currently known to build with FLT" in body
        assert "attributable to the PR" in body

    def test_merge_pass_has_no_master_caveat(self) -> None:
        """A successful merge-mode build needs no subtitle disclaimer.

        A pass is a pass — master health is irrelevant when the
        downstream actually built.  Neither the FKB-set framing
        (``mathlib master is currently incompatible …``) nor the
        FKB-null framing (``mathlib master is currently known to
        build …``) should fire on a pass.
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="pass",
                mode="merge",
                pr_base_sha="1" * 40,
                pr_head_sha="2" * 40,
                commits_replayed=1,
            )
        )

        # Assert
        assert "mathlib master is currently" not in body

    def test_lkg_pass_with_fkb_explains_why_lkg_matters(self) -> None:
        """LKG pass + FKB set names the master regression to sharpen the verdict.

        Without the framing, an LKG pass reads as "build worked", which
        a reader might assume is good news about master.  The FKB
        caveat clarifies: the verdict is about the PR's effect on X,
        not master's compatibility with X (which the FKB shows is
        currently broken).
        """
        # Arrange / Act
        body = _render(
            _make_result(
                status="pass",
                mode="lkg",
                lkg_commit=_LKG_SHA,
                fkb_commit=_FKB_SHA,
            )
        )

        # Assert
        assert "Current mathlib master is incompatible with FLT" in body
        assert f"/commit/{_FKB_SHA}" in body
        assert "purely be about the PR's effect on FLT" in body

    def test_lkg_pass_without_fkb_skips_subtitle(self) -> None:
        """LKG pass with no FKB skips the subtitle entirely (clean verdict).

        The cleanest case — recipe + section header speak for
        themselves; an extra "this was independent of master health"
        subtitle would just be noise.
        """
        # Arrange / Act
        body = _render(_make_result(status="pass", mode="lkg", lkg_commit=_LKG_SHA))

        # Assert
        assert "independent of current mathlib master health" not in body
        assert "Current mathlib master is incompatible" not in body


# ---------------------------------------------------------------------------
# Dispatch-level body (single comment for all entries)
# ---------------------------------------------------------------------------


class TestDispatchBody:
    """``render_dispatch_body`` assembles all entries into one comment."""

    def _render_dispatch(self, entries: list[dict], **kwargs) -> str:
        return post_results.render_dispatch_body(
            entries=entries,
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
            **kwargs,
        )

    def test_title_links_merge_sha_and_run(self) -> None:
        """The dispatch title carries the merge SHA + run link.

        Both anchors are what a reader needs to navigate from the
        comment back to the source-of-truth GitHub views (the merge
        tree and the workflow run that produced the verdicts).
        """
        # Arrange / Act
        body = self._render_dispatch([_make_entry(_make_result(status="pass"))])

        # Assert
        assert "# Downstream validation against PR merge" in body
        assert f"/commit/{_MERGE_SHA}" in body
        assert _RUN_URL in body

    def test_single_entry_skips_summary_table(self) -> None:
        """A one-entry dispatch needs no table — the section is the whole story.

        A two-row table for a single entry is visual noise; the
        section header already carries the verdict at a glance.
        """
        # Arrange / Act
        body = self._render_dispatch([_make_entry(_make_result(status="pass"))])

        # Assert
        assert "| Entry | Verdict |" not in body

    def test_multiple_entries_render_summary_table(self) -> None:
        """2+ entries render a leading table with one row each, in stable order.

        The table is the first thing a reader sees; it lets them
        triage at a glance ("3 of 4 passed, the one fail is
        master-attributable") before diving into per-entry sections.
        """
        # Arrange
        entries = [
            _make_entry(
                _make_result(
                    downstream="Toric",
                    status="pass",
                    mode="lkg",
                    lkg_commit=_LKG_SHA,
                )
            ),
            _make_entry(
                _make_result(downstream="Toric", status="pass", mode="merge")
            ),
            _make_entry(
                _make_result(
                    downstream="carleson",
                    status="fail",
                    mode="merge",
                    fkb_commit=_FKB_SHA,
                ),
                log_tail="some build error",
            ),
        ]

        # Act
        body = self._render_dispatch(entries)

        # Assert
        assert "| Entry | Verdict |" in body
        assert "|---|---|" in body
        assert "`Toric`" in body
        assert "`Toric --merge-branch`" in body
        assert "`carleson --merge-branch`" in body
        # The table precedes the per-entry sections.
        table_idx = body.find("| Entry | Verdict |")
        first_section_idx = body.find("## ")
        assert first_section_idx > table_idx

    def test_each_entry_renders_as_a_section(self) -> None:
        """Every entry produces its own ``##`` section in the body."""
        # Arrange
        entries = [
            _make_entry(
                _make_result(downstream="A", status="pass", mode="lkg", lkg_commit=_LKG_SHA)
            ),
            _make_entry(_make_result(downstream="B", status="pass", mode="merge")),
        ]

        # Act
        body = self._render_dispatch(entries)

        # Assert
        assert "## ✅ A builds against this PR rebased onto LKG" in body
        assert "## ✅ B --merge-branch builds against this PR" in body

    def test_mention_renders_once_at_top(self) -> None:
        """``triggered_by`` produces a single ``_Requested by @<user>._`` line above the title.

        One mention per dispatch — readers get one notification when
        their dispatch finishes, not one per matrix entry.
        """
        # Arrange
        # Act
        body = self._render_dispatch(
            [
                _make_entry(_make_result(status="pass", downstream="A")),
                _make_entry(_make_result(status="pass", downstream="B")),
            ],
            triggered_by="marcelolynch",
        )

        # Assert
        assert body.count("_Requested by @marcelolynch._") == 1
        mention_idx = body.find("_Requested by @marcelolynch._")
        title_idx = body.find("# Downstream validation")
        assert mention_idx >= 0
        assert mention_idx < title_idx

    def test_no_mention_when_triggered_by_empty(self) -> None:
        """Empty ``triggered_by`` omits the mention line entirely."""
        # Arrange / Act
        body = self._render_dispatch(
            [_make_entry(_make_result(status="pass"))], triggered_by=""
        )

        # Assert
        assert "_Requested by" not in body


# ---------------------------------------------------------------------------
# Verdict gloss for the summary table
# ---------------------------------------------------------------------------


class TestVerdictSummary:
    """``verdict_summary`` produces the one-line gloss for each table row."""

    def test_lkg_pass(self) -> None:
        """An lkg-mode pass advertises the LKG rebase.

        The "(rebased onto LKG)" suffix tells the reader at a glance
        that the verdict is master-health-independent.
        """
        # Arrange / Act / Assert
        assert (
            post_results.verdict_summary(
                _make_result(status="pass", mode="lkg", lkg_commit=_LKG_SHA)
            )
            == "✅ builds (rebased onto LKG)"
        )

    def test_merge_pass(self) -> None:
        """A merge-mode pass is the plain verdict."""
        # Arrange / Act / Assert
        assert (
            post_results.verdict_summary(_make_result(status="pass", mode="merge"))
            == "✅ builds"
        )

    def test_lkg_fail_attributes_to_pr(self) -> None:
        """An lkg-mode fail is unambiguously the PR's fault.

        LKG mode rebased onto known-good mathlib, so any failure must
        be the PR's effect; the gloss states that directly.
        """
        # Arrange / Act / Assert
        assert (
            post_results.verdict_summary(
                _make_result(status="fail", mode="lkg", lkg_commit=_LKG_SHA)
            )
            == "❌ fails (attributable to the PR)"
        )

    def test_merge_fail_with_fkb_names_regression(self) -> None:
        """A merge fail with FKB recorded calls out the master regression."""
        # Arrange / Act
        summary = post_results.verdict_summary(
            _make_result(status="fail", mode="merge", fkb_commit=_FKB_SHA)
        )

        # Assert
        assert "master incompatibility at" in summary
        assert f"/commit/{_FKB_SHA}" in summary

    def test_merge_fail_without_fkb_attributes_to_pr(self) -> None:
        """A merge fail with no FKB attributes to the PR."""
        # Arrange / Act / Assert
        assert (
            post_results.verdict_summary(_make_result(status="fail", mode="merge"))
            == "❌ fails (attributable to the PR)"
        )

    def test_rebase_conflict_gloss(self) -> None:
        """`rebase_conflict` gets a dedicated warning gloss."""
        # Arrange / Act / Assert
        assert (
            post_results.verdict_summary(
                _make_result(
                    status="infra_failure", stage="rebase_conflict", mode="lkg"
                )
            )
            == "⚠️ could not validate (PR conflicts with LKG)"
        )

    def test_mathlib_build_at_lkg_gloss(self) -> None:
        """`mathlib_build_at_lkg` has its own gloss."""
        # Arrange / Act / Assert
        assert (
            post_results.verdict_summary(
                _make_result(
                    status="infra_failure",
                    stage="mathlib_build_at_lkg",
                    mode="lkg",
                )
            )
            == "⚠️ could not validate (mathlib build failed at LKG)"
        )

    def test_generic_infra_failure_names_stage(self) -> None:
        """A generic infra failure names the offending stage.

        The stage is the actionable signal — a `clone_downstream`
        failure tells the maintainer to check the downstream repo's
        availability, while `lake_update` points at a manifest issue.
        """
        # Arrange / Act / Assert
        assert (
            post_results.verdict_summary(
                _make_result(
                    status="infra_failure",
                    stage="clone_downstream",
                    mode="merge",
                )
            )
            == "⚠️ could not validate (clone_downstream)"
        )


# ---------------------------------------------------------------------------
# Size budgeting
# ---------------------------------------------------------------------------


class TestSizeBudget:
    """The dispatch body fits under the GitHub PR-comment limit."""

    def test_body_within_limit_for_typical_dispatch(self) -> None:
        """A 4-entry dispatch with two failures stays under the comment limit.

        Two ~8K logs + two passes is a representative dispatch.  The
        body must fit under 60K (the budgeter's cap; 65 536 is
        GitHub's hard limit, the budgeter leaves headroom for the
        request envelope).
        """
        # Arrange
        log = ("oops oops " * 1000)[:8_000]
        entries = [
            _make_entry(
                _make_result(downstream="A", status="pass", mode="lkg", lkg_commit=_LKG_SHA)
            ),
            _make_entry(
                _make_result(
                    downstream="A", status="fail", mode="merge", fkb_commit=_FKB_SHA
                ),
                log_tail=log,
            ),
            _make_entry(
                _make_result(downstream="B", status="pass", mode="lkg", lkg_commit=_LKG_SHA)
            ),
            _make_entry(
                _make_result(downstream="B", status="fail", mode="merge"),
                log_tail=log,
            ),
        ]

        # Act
        body = post_results._shrink_to_fit(
            entries=entries,
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
            triggered_by="marcelolynch",
        )

        # Assert
        assert len(body) <= post_results.COMMENT_MAX_CHARS

    def test_oversized_logs_shrink_until_body_fits(self) -> None:
        """Pathologically large logs are halved until the body fits the limit.

        Two 200K logs is several times the comment budget.  The
        shrink-to-fit loop must halve the longest log repeatedly until
        the assembled body fits — and the per-entry section headers
        must survive so the reader still sees which entries failed.
        """
        # Arrange
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

        # Act
        body = post_results._shrink_to_fit(
            entries=entries,
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
            triggered_by="",
        )

        # Assert
        assert len(body) <= post_results.COMMENT_MAX_CHARS
        assert "## ❌ A --merge-branch fails" in body
        assert "## ❌ B --merge-branch fails" in body


# ---------------------------------------------------------------------------
# Requested-name flow (slug / short-name dichotomy)
# ---------------------------------------------------------------------------


class TestRequestedName:
    """When ``requested_name`` is set, it surfaces as the displayed entry token.

    The literal token the user typed (short name or ``owner/repo``
    slug) flows through validate.sh into result.json.  post_results
    uses it for the displayed entry label only; prose stays on the
    canonical downstream name.
    """

    def _render_section(self, **result_overrides) -> str:
        return post_results.render_entry_section(
            name="FLT",
            repo="leanprover-community/FLT",
            default_branch="main",
            result=_make_result(**result_overrides),
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
            log_tail="",
        )

    def test_slug_request_shows_in_section_header(self) -> None:
        """A section rendered for a slug request displays the slug in the header.

        Header mirrors the user's request; prose (the recipe sentence
        below) continues to use the canonical name so prose stays
        readable even when the slug form is awkward in running text.
        """
        # Arrange / Act
        body = self._render_section(
            status="pass",
            mode="lkg",
            lkg_commit=_LKG_SHA,
            requested_name="leanprover-community/FLT",
        )

        # Assert
        assert (
            "## ✅ leanprover-community/FLT builds against this PR rebased onto LKG"
            in body
        )
        assert "FLT's last-known-good" in body

    def test_slug_request_shows_in_summary_table(self) -> None:
        """The summary table cell shows the user's literal slug, not the canonical name."""
        # Arrange / Act
        body = post_results.render_dispatch_body(
            entries=[
                _make_entry(
                    _make_result(
                        downstream="FLT",
                        status="pass",
                        mode="lkg",
                        lkg_commit=_LKG_SHA,
                        requested_name="leanprover-community/FLT",
                    )
                ),
                _make_entry(
                    _make_result(
                        downstream="Toric",
                        status="pass",
                        mode="lkg",
                        lkg_commit=_LKG_SHA,
                    )
                ),
            ],
            merge_sha=_MERGE_SHA,
            run_url=_RUN_URL,
        )

        # Assert
        assert "`leanprover-community/FLT`" in body

    def test_no_requested_name_falls_back_to_downstream(self) -> None:
        """result.json without ``requested_name`` (the common case) shows the canonical name."""
        # Arrange / Act
        body = self._render_section(status="pass", mode="lkg", lkg_commit=_LKG_SHA)

        # Assert
        assert "## ✅ FLT builds against this PR rebased onto LKG" in body


