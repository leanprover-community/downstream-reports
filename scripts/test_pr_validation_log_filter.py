#!/usr/bin/env python3
"""Tests for scripts/pr_validation/log_filter.py noise filtering.

The filter feeds the failure-tail block in PR comments and the
step-summary block on the workflow run page. Lines that survive the
filter should describe what failed; lines that get dropped are
high-volume progress / scaffolding output that buries the signal.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PR_VALIDATION_DIR = Path(__file__).resolve().parent / "pr_validation"
sys.path.insert(0, str(_PR_VALIDATION_DIR))
_LOG_FILTER_PATH = _PR_VALIDATION_DIR / "log_filter.py"
_spec = importlib.util.spec_from_file_location(
    "pr_validation_log_filter", _LOG_FILTER_PATH
)
log_filter = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(log_filter)


class IsNoiseLineTests(unittest.TestCase):
    """is_noise_line covers each documented prefix."""

    def assert_noise(self, line: str) -> None:
        self.assertTrue(
            log_filter.is_noise_line(line),
            f"expected to drop: {line!r}",
        )

    def assert_kept(self, line: str) -> None:
        self.assertFalse(
            log_filter.is_noise_line(line),
            f"expected to keep: {line!r}",
        )

    def test_drops_check_marks_and_traces(self) -> None:
        """Scenario: success ✔ marks and lake `trace: .>` lines are noise."""
        self.assert_noise("✔ [42/100] Built Mathlib.Foo (1.2s)")
        self.assert_noise("trace: .> elan run --install leanprover/lean4 …")

    def test_drops_lake_info_progress(self) -> None:
        """Scenario: `info:` lines from `lake update` (cloning / toolchain hooks) are noise."""
        self.assert_noise("info: aesop: cloning https://github.com/…")
        self.assert_noise("info: mathlib: running post-update hooks")

    def test_drops_gh_actions_directives(self) -> None:
        """Scenario: ::group::/::endgroup::/::notice/::warning/::error markers leak from validate.sh."""
        self.assert_noise("::group::lake build (downstream)")
        self.assert_noise("::endgroup::")
        self.assert_noise("::notice title=Cherry-pick::Replaying 4 PR commit(s)")
        self.assert_noise("::warning title=FAIL::add-combi failed to build")
        self.assert_noise("::error title=Clone failed::could not clone …")

    def test_drops_cache_progress_ticks(self) -> None:
        """Scenario: `lake exe cache get` download/decompress progress is noise."""
        self.assert_noise(
            "Downloaded: 350 file(s) [attempted 350/8387 = 4%, 297 KB/s], Decompressed: 152"
        )
        self.assert_noise("Decompressed 8385 file(s)")
        self.assert_noise("Already decompressed 8385 file(s)")
        self.assert_noise("Decompressing 359 already-cached file(s)")

    def test_drops_cache_warning_paragraph(self) -> None:
        """Scenario: the predictable 'some files were not found in the cache' header is noise."""
        self.assert_noise("Warning: some files were not found in the cache.")
        self.assert_noise(
            "This usually means that your local checkout of mathlib4 has diverged from upstream."
        )

    def test_keeps_lean_errors_and_failure_marks(self) -> None:
        """Scenario: actual Lean errors and ✖ failure marks are signal."""
        self.assert_kept("✖ [1860/1862] Building AddCombi.BSG (4.4s)")
        self.assert_kept(
            "error: AddCombi/BSG.lean:61:43: Tactic `grewrite` failed: …"
        )
        self.assert_kept("error: Lean exited with code 1")
        self.assert_kept("Some required targets logged failures:")
        self.assert_kept("- AddCombi.BSG")


class FilterLogTextTests(unittest.TestCase):
    """End-to-end filtering of a representative noisy block."""

    def test_collapses_to_signal(self) -> None:
        """Scenario: a 13-line mixed block reduces to just the meaningful failure lines."""
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
        out = log_filter.filter_log_text(sample).splitlines()
        self.assertEqual(
            out,
            [
                "✖ [1860/1862] Building AddCombi.BSG (4.4s)",
                "error: AddCombi/BSG.lean:61:43: Tactic `grewrite` failed",
                "error: Lean exited with code 1",
            ],
        )


class ReadLogTailTests(unittest.TestCase):
    """read_log_tail returns up to max_chars from the END of the filtered log."""

    def test_returns_empty_when_log_missing(self) -> None:
        """Scenario: missing log path returns the empty string (no exception)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "absent.log"
            self.assertEqual(log_filter.read_log_tail(path, 100), "")

    def test_filters_then_truncates(self) -> None:
        """Scenario: filter first, then take the last max_chars characters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "build.log"
            path.write_text(
                "Downloaded: 1 file(s)\n"
                "error: line A\n"
                "Downloaded: 2 file(s)\n"
                "error: line B\n"
            )
            tail = log_filter.read_log_tail(path, 100)
            self.assertNotIn("Downloaded:", tail)
            self.assertIn("error: line A", tail)
            self.assertIn("error: line B", tail)


if __name__ == "__main__":
    unittest.main()
