#!/usr/bin/env python3
"""
Tests for: scripts.pr_validation.log_filter

Coverage scope:
    - ``is_noise_line`` — drops each documented high-volume progress /
      scaffolding prefix (lake `trace:`, `info:`, GitHub Actions
      `::group::` directives, cache-progress ticks, the predictable
      "some files were not found in the cache" header + bullets).
    - ``filter_log_text`` — end-to-end pass over a representative noisy
      block; the surviving lines are exactly the meaningful failure
      signal.
    - ``read_log_tail`` — missing-file tolerance + filter-then-truncate
      ordering against ``max_chars``.

Out of scope:
    - Real ``lake build`` output past the prefixes we filter on.  The
      filter is a heuristic, not a parser; tests pin the prefixes we
      promise to handle and the lines we promise to keep.

Why this matters
----------------
The filter feeds the failure-tail block in the dispatch-level PR
comment and the per-job summary on the workflow run page.  A regression
that lets cache-progress ticks back through would bury the actual Lean
error under hundreds of `Downloaded: …` lines and quickly push the
comment past GitHub's 65 536-char limit, where post_results.py's
shrink-to-fit budgeter would start dropping logs entirely.  A
regression that filters out real Lean errors would silently turn an
actionable failure comment into an opaque one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from scripts.pr_validation import log_filter


class TestIsNoiseLine:
    """`is_noise_line` covers each documented prefix."""

    def _assert_noise(self, line: str) -> None:
        assert log_filter.is_noise_line(line), f"expected to drop: {line!r}"

    def _assert_kept(self, line: str) -> None:
        assert not log_filter.is_noise_line(line), f"expected to keep: {line!r}"

    def test_drops_check_marks_and_traces(self) -> None:
        """Success ✔ marks and lake `trace: .>` lines are noise.

        ``lake build`` emits a per-target ``✔ [n/N] Built …`` line on
        each successful target — under 1 % of which is informative if the
        overall build succeeded, and zero if it failed.
        """
        # Arrange / Act / Assert
        self._assert_noise("✔ [42/100] Built Mathlib.Foo (1.2s)")
        self._assert_noise("trace: .> elan run --install leanprover/lean4 …")

    def test_drops_lake_info_progress(self) -> None:
        """`info:` lines from `lake update` (cloning / toolchain hooks) are noise.

        These describe the manifest-resolution and toolchain-setup work
        ``lake update`` does, not the source compilation itself; the
        failure tail is for the actual build output.
        """
        # Arrange / Act / Assert
        self._assert_noise("info: aesop: cloning https://github.com/…")
        self._assert_noise("info: mathlib: running post-update hooks")

    def test_drops_gh_actions_directives(self) -> None:
        """`::group::` / `::endgroup::` / `::notice` / `::warning` / `::error` markers leak from validate.py.

        validate.py wraps each phase in ``::group::`` blocks and emits
        ``::notice::`` / ``::warning::`` annotations so the live workflow
        log reads well.  Those tokens are scaffolding from the runner's
        point of view; the PR comment only wants the underlying log.
        """
        # Arrange / Act / Assert
        self._assert_noise("::group::lake build (downstream)")
        self._assert_noise("::endgroup::")
        self._assert_noise("::notice title=Cherry-pick::Replaying 4 PR commit(s)")
        self._assert_noise("::warning title=FAIL::add-combi failed to build")
        self._assert_noise("::error title=Clone failed::could not clone …")

    def test_drops_cache_progress_ticks(self) -> None:
        """`lake exe cache get` download/decompress progress is noise.

        A cold cache produces thousands of `Downloaded: …` / `Decompressed
        N file(s)` lines; without this filter the failure tail would be
        entirely cache progress and zero signal.
        """
        # Arrange / Act / Assert
        self._assert_noise(
            "Downloaded: 350 file(s) [attempted 350/8387 = 4%, 297 KB/s], Decompressed: 152"
        )
        self._assert_noise("Decompressed 8385 file(s)")
        self._assert_noise("Already decompressed 8385 file(s)")
        self._assert_noise("Decompressing 359 already-cached file(s)")

    def test_drops_cache_warning_paragraph(self) -> None:
        """The predictable 'some files were not found in the cache' header + bullets are noise.

        Lake renders this paragraph any time a single file is missing
        from the upstream olean cache.  It's standard cache-warming
        prose with no PR-specific signal; both the bullet line and the
        indented continuation of each bullet are filtered.
        """
        # Arrange / Act / Assert
        self._assert_noise("Warning: some files were not found in the cache.")
        self._assert_noise(
            "This usually means that your local checkout of mathlib4 has diverged from upstream."
        )
        self._assert_noise("  * If you push your commits to a PR to the mathlib4 repository")
        self._assert_noise("    (use a draft PR if it is not ready for review),")
        self._assert_noise("    then CI will build the oleans and they will be available later.")
        self._assert_noise("  * If you have already opened a PR, this may mean")
        self._assert_noise("    the CI build has failed part-way through building.")

    def test_keeps_lean_errors_and_failure_marks(self) -> None:
        """Actual Lean errors and ✖ failure marks are signal.

        These are what the PR author needs to see in the failure tail;
        the inverse of the previous tests.
        """
        # Arrange / Act / Assert
        self._assert_kept("✖ [1860/1862] Building AddCombi.BSG (4.4s)")
        self._assert_kept(
            "error: AddCombi/BSG.lean:61:43: Tactic `grewrite` failed: …"
        )
        self._assert_kept("error: Lean exited with code 1")
        self._assert_kept("Some required targets logged failures:")
        self._assert_kept("- AddCombi.BSG")


class TestFilterLogText:
    """End-to-end filtering of a representative noisy block."""

    def test_collapses_to_signal(self) -> None:
        """A 13-line mixed block reduces to just the meaningful failure lines.

        Mixes group directives, cache progress, success ticks, the
        cache-warning paragraph, and a real failure.  The expected
        survivors are exactly the ✖ build mark and the two ``error:``
        lines — anything else slipping through would indicate a
        regression in the prefix set.
        """
        # Arrange
        sample = "\n".join(
            [
                "::group::lake build (downstream)",
                "✔ [1845/1862] Built AddCombi.Mathlib.Data.NNRat.Order (1.3s)",
                "Downloaded: 350 file(s) [attempted 350/8387 = 4%, 297 KB/s], Decompressed: 152",
                "Downloaded: 8385 file(s) [attempted 8387/8387 = 100%, 1663 KB/s], Decompressed: 2417",
                "Warning: some files were not found in the cache.",
                "This usually means that your local checkout of mathlib4 has diverged from upstream.",
                "Decompressed 8385 file(s)",
                "Already decompressed 8385 file(s)",
                "✖ [1860/1862] Building AddCombi.BSG (4.4s)",
                "error: AddCombi/BSG.lean:61:43: Tactic `grewrite` failed",
                "error: Lean exited with code 1",
                "::endgroup::",
                "::warning title=FAIL::add-combi failed to build against this PR",
            ]
        )

        # Act
        out = log_filter.filter_log_text(sample).splitlines()

        # Assert
        assert out == [
            "✖ [1860/1862] Building AddCombi.BSG (4.4s)",
            "error: AddCombi/BSG.lean:61:43: Tactic `grewrite` failed",
            "error: Lean exited with code 1",
        ]


class TestReadLogTail:
    """`read_log_tail` returns up to ``max_chars`` from the END of the filtered log."""

    def test_returns_empty_when_log_missing(self) -> None:
        """Missing log path returns the empty string (no exception).

        validate.py emits ``result.json`` even on infra failures that
        never produced a ``build.log``; the renderer must tolerate the
        log being absent rather than crash the report job.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "absent.log"

            # Act / Assert
            assert log_filter.read_log_tail(path, 100) == ""

    def test_filters_then_truncates(self) -> None:
        """Filter first, then take the last ``max_chars`` characters.

        Truncating raw bytes first would clip mid-line and drop the
        error lines we promised to keep; the filter pass has to happen
        first so the byte budget applies to signal, not noise.
        """
        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "build.log"
            path.write_text(
                "Downloaded: 1 file(s)\n"
                "error: line A\n"
                "Downloaded: 2 file(s)\n"
                "error: line B\n"
            )

            # Act
            tail = log_filter.read_log_tail(path, 100)

            # Assert
            assert "Downloaded:" not in tail
            assert "error: line A" in tail
            assert "error: line B" in tail
