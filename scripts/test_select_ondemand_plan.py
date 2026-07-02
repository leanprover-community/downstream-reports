#!/usr/bin/env python3
"""
Tests for: scripts.select_ondemand_plan

Coverage scope:
    - ``main`` (CLI) — the full candidate → include/skipped decision
      table: enabled/bumping-branch filtering, the seen-commit dedup
      against prior on-demand runs, ``--force``, ``--downstream`` /
      ``--branch`` targeting, and unreachable branch refs.
    - The two-pass dedup contract: prior details are batch-loaded only
      for the pairs that were actually skipped.

Out of scope:
    - ``_gh_api`` network behaviour (patched throughout; HTTP error
      handling is a thin wrapper around urllib).
    - Backend implementations (``create_backend`` is patched; the SQL
      queries behind ``load_tested_downstream_commits`` /
      ``load_prior_results`` are covered by ``test_storage.py``).

Why this matters
----------------
The plan job decides which downstreams consume self-hosted probe
capacity on every on-demand tick.  A filtering bug either burns runner
time re-validating commits already tested (dedup broken) or silently
drops a downstream from the matrix (filtering too aggressive) — and the
skipped-details payload feeds the Zulip report, so its shape is a
contract with ``send_alerts``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.select_ondemand_plan import main as plan_main

_SHA = "c" * 40


def _downstream(name: str = "physlib", **overrides) -> dict:
    entry = {
        "name": name,
        "repo": f"org/{name}",
        "default_branch": "main",
        "bumping_branch": "bump",
    }
    entry.update(overrides)
    return entry


def _run_plan(
    tmp_path: Path,
    downstreams: list[dict],
    *,
    argv_extra: tuple[str, ...] = (),
    seen: frozenset[tuple[str, str]] = frozenset(),
    prior: dict[tuple[str, str], dict] | None = None,
    branch_heads: dict[tuple[str, str], str] | None = None,
) -> tuple[int, list[dict], list[dict], MagicMock]:
    """Drive ``main`` with a fake backend and GitHub API.

    ``branch_heads`` maps ``(repo, branch)`` to the head SHA the fake
    API reports; a missing key behaves like an API error (ref fetch
    returns None).  Returns ``(rc, include, skipped, backend)``.
    """
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"downstreams": downstreams}))
    output = tmp_path / "matrix.json"
    skipped_output = tmp_path / "skipped.json"

    backend = MagicMock()
    backend.load_tested_downstream_commits.return_value = set(seen)
    backend.load_prior_results.return_value = prior or {}

    heads = branch_heads if branch_heads is not None else {
        (d["repo"], d.get("bumping_branch") or "bump"): _SHA for d in downstreams
    }

    def fake_gh_api(path: str, token: str) -> dict | None:
        _, repo_owner, repo_name, _, _, _, branch = path.split("/", 6)
        sha = heads.get((f"{repo_owner}/{repo_name}", branch))
        return None if sha is None else {"object": {"sha": sha}}

    argv = [
        "select_ondemand_plan.py",
        "--inventory", str(inventory),
        "--output", str(output),
        "--skipped-output", str(skipped_output),
        "--backend", "dry-run",
        *argv_extra,
    ]
    with (
        patch("scripts.select_ondemand_plan.create_backend", return_value=backend),
        patch("scripts.select_ondemand_plan._gh_api", side_effect=fake_gh_api),
        patch.dict(os.environ, {"GITHUB_TOKEN": "gh_fake"}),
        patch.object(sys, "argv", argv),
    ):
        rc = plan_main()

    include = json.loads(output.read_text())["include"]
    skipped = json.loads(skipped_output.read_text())
    return rc, include, skipped, backend


class TestPlanDecisionTable:
    """Which downstreams land in the matrix vs the skipped payload."""

    @pytest.mark.parametrize(
        ("downstreams", "argv_extra", "seen", "expect_include", "expect_skipped"),
        [
            pytest.param(
                [_downstream("physlib")], (), frozenset(),
                ["physlib"], [],
                id="new-commit-on-bumping-branch-is-included",
            ),
            pytest.param(
                [_downstream("physlib")], (), frozenset({("physlib", _SHA)}),
                [], ["physlib"],
                id="already-tested-head-is-skipped",
            ),
            pytest.param(
                [_downstream("physlib")], ("--force",), frozenset({("physlib", _SHA)}),
                ["physlib"], [],
                id="force-includes-already-tested-head",
            ),
            pytest.param(
                [_downstream("physlib", enabled=False)], (), frozenset(),
                [], [],
                id="disabled-downstream-is-not-a-candidate",
            ),
            pytest.param(
                [_downstream("physlib", bumping_branch=None)], (), frozenset(),
                [], [],
                id="no-bumping-branch-is-not-a-candidate",
            ),
            pytest.param(
                [_downstream("physlib"), _downstream("alglib")],
                ("--downstream", "alglib"), frozenset(),
                ["alglib"], [],
                id="downstream-filter-limits-candidates",
            ),
            pytest.param(
                [_downstream("physlib", bumping_branch=None)],
                ("--downstream", "physlib", "--branch", "feature/x"), frozenset(),
                ["physlib"], [],
                id="explicit-branch-bypasses-bumping-branch-filter",
            ),
        ],
    )
    def test_candidate_filtering(
        self,
        tmp_path: Path,
        downstreams: list[dict],
        argv_extra: tuple[str, ...],
        seen: frozenset[tuple[str, str]],
        expect_include: list[str],
        expect_skipped: list[str],
    ) -> None:
        """Scenario: each row of the include/skip decision table."""
        # Arrange — explicit-branch scenarios need the override branch head.
        branch_heads = {
            (d["repo"], "feature/x" if "--branch" in argv_extra else d.get("bumping_branch") or "bump"): _SHA
            for d in downstreams
        }

        # Act
        rc, include, skipped, _ = _run_plan(
            tmp_path, downstreams,
            argv_extra=argv_extra, seen=seen, branch_heads=branch_heads,
        )

        # Assert
        assert rc == 0
        assert [item["name"] for item in include] == expect_include
        assert [item["downstream"] for item in skipped] == expect_skipped
        for item in include:
            assert item["runs_on"], "matrix entries must always carry a runs-on label"

    def test_unfetchable_branch_ref_drops_downstream_entirely(self, tmp_path: Path) -> None:
        """Scenario: a downstream whose branch ref cannot be fetched is
        neither included nor reported as skipped — the API error is logged
        and the downstream simply drops out of this tick."""
        rc, include, skipped, _ = _run_plan(
            tmp_path, [_downstream("physlib")], branch_heads={},
        )
        assert rc == 0
        assert include == []
        assert skipped == []

    def test_branch_without_downstream_is_a_usage_error(self, tmp_path: Path) -> None:
        """Scenario: --branch targets a specific downstream; without
        --downstream the dispatch is ambiguous and must fail loudly."""
        with pytest.raises(SystemExit):
            _run_plan(tmp_path, [_downstream()], argv_extra=("--branch", "feature/x"))


class TestSkippedDetailsBatch:
    """The second-pass batch load of prior details for skipped downstreams."""

    def test_prior_details_loaded_only_for_skipped_pairs(self, tmp_path: Path) -> None:
        """Scenario: one downstream skips (already-tested head) and one is
        included; prior details are batch-loaded for exactly the skipped
        pair and flow verbatim into the skipped payload."""
        # Arrange
        prior = {
            ("physlib", _SHA): {
                "outcome": "failed",
                "episode_state": "failing",
                "first_known_bad": "b" * 40,
                "target_commit": "t" * 40,
                "failure_stage": "lake build",
                "run_url": "https://example.com/runs/7",
                "job_url": "https://example.com/jobs/7",
            }
        }

        # Act
        rc, include, skipped, backend = _run_plan(
            tmp_path,
            [_downstream("physlib"), _downstream("alglib")],
            seen=frozenset({("physlib", _SHA)}),
            prior=prior,
        )

        # Assert
        assert rc == 0
        assert [item["name"] for item in include] == ["alglib"]
        backend.load_prior_results.assert_called_once_with(
            "ondemand", {("physlib", _SHA)},
        )
        (entry,) = skipped
        assert entry == {
            "downstream": "physlib",
            "repo": "org/physlib",
            "downstream_commit": _SHA,
            "outcome": "failed",
            "episode_state": "failing",
            "first_known_bad": "b" * 40,
            "target_commit": "t" * 40,
            "failure_stage": "lake build",
            "previous_run_url": "https://example.com/runs/7",
            "previous_job_url": "https://example.com/jobs/7",
        }

    def test_no_skips_means_no_prior_details_query(self, tmp_path: Path) -> None:
        """Scenario: when nothing skips, the batch query never runs — the
        plan job's only DB reads are the seen-commit load and (at most)
        one batch of prior details."""
        rc, _, skipped, backend = _run_plan(tmp_path, [_downstream("physlib")])
        assert rc == 0
        assert skipped == []
        backend.load_prior_results.assert_not_called()
