#!/usr/bin/env python3
"""Tests for the storage module: backend factory, serialization, filesystem backend."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.storage import (
    DownstreamStatusRecord,
    FilesystemBackend,
    RunResultRecord,
)


def _make_run_result(
    downstream: str,
    downstream_commit: str,
    outcome: str,
) -> RunResultRecord:
    """Build a minimal RunResultRecord for testing."""
    return RunResultRecord(
        upstream="leanprover-community/mathlib4",
        downstream=downstream,
        repo="owner/repo",
        downstream_commit=downstream_commit,
        outcome=outcome,
        episode_state="passing" if outcome == "passed" else "error",
        target_commit="target_abc",
        previous_last_known_good=None,
        previous_first_known_bad=None,
        last_known_good="target_abc" if outcome == "passed" else None,
        first_known_bad=None,
        current_last_successful=None,
        current_first_failing=None,
        failure_stage=None,
        search_mode="head-only",
        commit_window_truncated=False,
        error=None,
        head_probe_outcome=outcome,
        head_probe_failure_stage=None,
        culprit_log_text=None,
    )


class FilesystemBackendTests(unittest.TestCase):
    """Test the filesystem storage backend."""

    def test_load_all_statuses_returns_empty_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            result = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertEqual(result, {})

    def test_save_run_and_load_statuses_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            record = RunResultRecord(
                upstream="leanprover-community/mathlib4",
                downstream="TestDownstream",
                repo="owner/repo",
                downstream_commit="ds_head",
                outcome="passed",
                episode_state="passing",
                target_commit="target_abc",
                previous_last_known_good=None,
                previous_first_known_bad=None,
                last_known_good="target_abc",
                first_known_bad=None,
                current_last_successful="target_abc",
                current_first_failing=None,
                failure_stage=None,
                search_mode="head-only",
                commit_window_truncated=False,
                error=None,
                head_probe_outcome="passed",
                head_probe_failure_stage=None,
                culprit_log_text=None,
                pinned_commit="pin_abc",
            )
            statuses = {
                "TestDownstream": DownstreamStatusRecord(
                    last_known_good_commit="target_abc",
                    pinned_commit="pin_abc",
                ),
            }
            backend.save_run(
                run_id="run_123",
                workflow="regression",
                upstream="leanprover-community/mathlib4",
                upstream_ref="master",
                run_url="https://example.com/run/123",
                created_at="2026-04-01T00:00:00Z",
                results=[record],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertIn("TestDownstream", loaded)
            self.assertEqual(loaded["TestDownstream"].last_known_good_commit, "target_abc")
            self.assertEqual(loaded["TestDownstream"].pinned_commit, "pin_abc")

    def test_downstream_commit_round_trip(self) -> None:
        """downstream_commit is persisted in the status JSON and reloaded."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            statuses = {
                "TestDownstream": DownstreamStatusRecord(
                    last_known_good_commit="target_abc",
                    downstream_commit="ds_commit_abc",
                ),
            }
            backend.save_run(
                run_id="run_456",
                workflow="regression",
                upstream="leanprover-community/mathlib4",
                upstream_ref="master",
                run_url="https://example.com/run/456",
                created_at="2026-04-02T00:00:00Z",
                results=[],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertEqual(loaded["TestDownstream"].downstream_commit, "ds_commit_abc")

    def test_last_good_release_round_trip(self) -> None:
        """last_good_release and last_good_release_commit are persisted and reloaded."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            statuses = {
                "TestDownstream": DownstreamStatusRecord(
                    last_known_good_commit="lkg_abc",
                    last_good_release="v4.13.0",
                    last_good_release_commit="sha_v4_13_0",
                ),
            }
            backend.save_run(
                run_id="run_release",
                workflow="regression",
                upstream="leanprover-community/mathlib4",
                upstream_ref="master",
                run_url="https://example.com/run/release",
                created_at="2026-04-10T00:00:00Z",
                results=[],
                updated_statuses=statuses,
            )
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertEqual(loaded["TestDownstream"].last_good_release, "v4.13.0")
            self.assertEqual(loaded["TestDownstream"].last_good_release_commit, "sha_v4_13_0")

    def test_last_good_release_defaults_to_none(self) -> None:
        """Existing status files without last_good_release fields load as None."""
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp) / "status"
            status_dir.mkdir()
            (status_dir / "current.json").write_text(json.dumps({
                "schema_version": 2,
                "reported_at": "2026-04-01T00:00:00Z",
                "downstreams": {
                    "OldDownstream": {
                        "last_known_good_commit": "abc",
                        "first_known_bad_commit": None,
                        "pinned_commit": None,
                        "downstream_commit": None,
                    },
                },
            }))
            backend = FilesystemBackend(Path(tmp))
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertIsNone(loaded["OldDownstream"].last_good_release)
            self.assertIsNone(loaded["OldDownstream"].last_good_release_commit)

    def test_downstream_commit_defaults_to_none(self) -> None:
        """Existing status files without downstream_commit load as None."""
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp) / "status"
            status_dir.mkdir()
            (status_dir / "current.json").write_text(json.dumps({
                "schema_version": 2,
                "reported_at": "2026-04-01T00:00:00Z",
                "downstreams": {
                    "OldDownstream": {
                        "last_known_good_commit": "abc",
                        "first_known_bad_commit": None,
                        "pinned_commit": None,
                    },
                },
            }))
            backend = FilesystemBackend(Path(tmp))
            loaded = backend.load_all_statuses("regression", "leanprover-community/mathlib4")
            self.assertIsNone(loaded["OldDownstream"].downstream_commit)

    def test_load_tested_downstream_commits_empty(self) -> None:
        """Scenario: no results yet returns empty set."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            self.assertEqual(backend.load_tested_downstream_commits("ondemand"), set())

    def test_load_tested_downstream_commits_from_saved_run(self) -> None:
        """Scenario: passed/failed results are discoverable via load_tested_downstream_commits."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            results = [
                _make_run_result("ProjectA", "commit_aaa", "passed"),
                _make_run_result("ProjectB", "commit_bbb", "failed"),
                _make_run_result("ProjectC", "commit_ccc", "error"),
            ]
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream="leanprover-community/mathlib4",
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=results,
                updated_statuses={},
            )
            seen = backend.load_tested_downstream_commits("ondemand")
            self.assertIn(("ProjectA", "commit_aaa"), seen)
            self.assertIn(("ProjectB", "commit_bbb"), seen)
            # error outcomes are excluded from dedup
            self.assertNotIn(("ProjectC", "commit_ccc"), seen)

    def test_load_tested_downstream_commits_scoped_by_workflow(self) -> None:
        """Scenario: results from a different workflow are not returned."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_reg",
                workflow="regression",
                upstream="leanprover-community/mathlib4",
                upstream_ref="master",
                run_url="https://example.com/run/reg",
                created_at="2026-04-01T00:00:00Z",
                results=[_make_run_result("ProjectA", "commit_aaa", "passed")],
                updated_statuses={},
            )
            # ondemand workflow should not see regression results
            self.assertEqual(backend.load_tested_downstream_commits("ondemand"), set())


class LoadPriorResultsTests(unittest.TestCase):
    """Tests for StorageBackend.load_prior_results."""

    def test_empty_pairs_returns_empty(self) -> None:
        """Scenario: passing an empty set returns empty dict."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            self.assertEqual(backend.load_prior_results("ondemand", set()), {})

    def test_returns_matching_results(self) -> None:
        """Scenario: returns rich data for matching (downstream, commit) pairs."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            results = [
                _make_run_result("ProjectA", "commit_aaa", "passed"),
                _make_run_result("ProjectB", "commit_bbb", "failed"),
            ]
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream="leanprover-community/mathlib4",
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=results,
                updated_statuses={},
            )
            pairs = {("ProjectA", "commit_aaa"), ("ProjectB", "commit_bbb")}
            prior = backend.load_prior_results("ondemand", pairs)
            self.assertIn(("ProjectA", "commit_aaa"), prior)
            self.assertIn(("ProjectB", "commit_bbb"), prior)
            self.assertEqual(prior[("ProjectA", "commit_aaa")]["outcome"], "passed")
            self.assertEqual(prior[("ProjectB", "commit_bbb")]["outcome"], "failed")

    def test_excludes_error_outcomes(self) -> None:
        """Scenario: error outcomes are not included in prior results."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream="leanprover-community/mathlib4",
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=[_make_run_result("ProjectC", "commit_ccc", "error")],
                updated_statuses={},
            )
            pairs = {("ProjectC", "commit_ccc")}
            prior = backend.load_prior_results("ondemand", pairs)
            self.assertEqual(prior, {})

    def test_only_returns_requested_pairs(self) -> None:
        """Scenario: results not in the pairs set are excluded."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_1",
                workflow="ondemand",
                upstream="leanprover-community/mathlib4",
                upstream_ref="ondemand",
                run_url="https://example.com/run/1",
                created_at="2026-04-01T00:00:00Z",
                results=[
                    _make_run_result("ProjectA", "commit_aaa", "passed"),
                    _make_run_result("ProjectB", "commit_bbb", "failed"),
                ],
                updated_statuses={},
            )
            pairs = {("ProjectA", "commit_aaa")}
            prior = backend.load_prior_results("ondemand", pairs)
            self.assertIn(("ProjectA", "commit_aaa"), prior)
            self.assertNotIn(("ProjectB", "commit_bbb"), prior)

    def test_returns_newest_result_per_pair(self) -> None:
        """Scenario: when multiple runs match, the newest result is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = FilesystemBackend(Path(tmp))
            backend.save_run(
                run_id="run_old",
                workflow="ondemand",
                upstream="leanprover-community/mathlib4",
                upstream_ref="ondemand",
                run_url="https://example.com/run/old",
                created_at="2026-04-01T00:00:00Z",
                results=[_make_run_result("ProjectA", "commit_aaa", "failed")],
                updated_statuses={},
            )
            backend.save_run(
                run_id="run_new",
                workflow="ondemand",
                upstream="leanprover-community/mathlib4",
                upstream_ref="ondemand",
                run_url="https://example.com/run/new",
                created_at="2026-04-02T00:00:00Z",
                results=[_make_run_result("ProjectA", "commit_aaa", "passed")],
                updated_statuses={},
            )
            pairs = {("ProjectA", "commit_aaa")}
            prior = backend.load_prior_results("ondemand", pairs)
            self.assertEqual(prior[("ProjectA", "commit_aaa")]["outcome"], "passed")


if __name__ == "__main__":
    unittest.main()
