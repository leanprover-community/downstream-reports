#!/usr/bin/env python3
"""
Tests for: scripts.send_alerts

Coverage scope:
    - ``main`` (CLI) — payload guards (missing/empty file), the
      regression-vs-ondemand dispatch split (which records produce
      alerts and which SHAs get title lookups), and the error-notice
      path for error outcomes.

Out of scope:
    - Alert eligibility and message formatting — ``compute_alert_actions``
      and the formatters are covered by ``test_notifications.py`` (the
      real ``compute_alert_actions`` runs here so action counts stay
      honest, but its per-state behaviour is pinned there).
    - Zulip transport (``ZulipSender``) and the GitHub title/tag fetchers
      (patched; they wrap network calls).

Why this matters
----------------
``send_alerts`` is the last hop between a detected regression and a
human: a dispatch bug here means a NEW_FAILURE lands in the database but
never reaches Zulip.  The regression/on-demand split is easy to break
silently — regression alerts only on transitions while on-demand reports
everything — and the SHA-collection loops decide how many GitHub API
calls each alert tick spends.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.send_alerts import main as alerts_main


def _record(**overrides) -> dict:
    """Build a minimal alert-payload record (serialized RunResultRecord)."""
    record = {
        "downstream": "physlib",
        "repo": "org/physlib",
        "downstream_commit": "d" * 40,
        "episode_state": "new_failure",
        "outcome": "failed",
        "target_commit": "t" * 40,
        "first_known_bad": "b" * 40,
        "last_known_good": "g" * 40,
        "previous_first_known_bad": None,
        "failure_stage": "build",
        "culprit_log_text": None,
    }
    record.update(overrides)
    return record


class _RecordingSender:
    """Stand-in for DryRunSender that records every message."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str, str]] = []

    def send_message(self, stream: str, topic: str, content: str) -> None:
        self.messages.append((stream, topic, content))


def _run_alerts(
    tmp_path: Path,
    payload: dict | str | None,
    *,
    workflow: str = "regression",
) -> tuple[int, _RecordingSender, list, set[str]]:
    """Drive ``main`` with the network fetchers and sender patched.

    ``payload`` of ``None`` means the payload file does not exist; a
    string is written verbatim (empty-file guard); a dict is written as
    JSON.  Returns ``(rc, sender, executed_actions, fetched_shas)``.
    """
    payload_path = tmp_path / "alert-payload.json"
    if isinstance(payload, str):
        payload_path.write_text(payload)
    elif payload is not None:
        payload_path.write_text(json.dumps(payload))

    sender = _RecordingSender()
    executed: list = []
    fetched: set[str] = set()

    def fake_fetch_titles(shas, repo=None, token=None):
        fetched.update(shas)
        return {}

    argv = [
        "send_alerts.py",
        "--alert-payload", str(payload_path),
        "--run-url", "https://example.com/runs/1",
        "--workflow", workflow,
    ]
    with (
        patch("scripts.send_alerts.fetch_commit_titles", side_effect=fake_fetch_titles),
        patch("scripts.send_alerts.fetch_tags", return_value={}),
        patch(
            "scripts.send_alerts.execute_alerts",
            side_effect=lambda actions, sender_: executed.extend(actions),
        ),
        # main() constructs its own sender; the patched class hands back
        # this test's recorder instance instead.
        patch("scripts.send_alerts.DryRunSender", return_value=sender),
        patch.object(sys, "argv", argv),
    ):
        rc = alerts_main()

    return rc, sender, executed, fetched


class TestPayloadGuards:
    """Alert job never fails the workflow over a missing or empty payload."""

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(None, id="missing-payload-file"),
            pytest.param("", id="empty-payload-file"),
        ],
    )
    def test_absent_payload_exits_cleanly(self, tmp_path: Path, payload) -> None:
        """Scenario: the aggregate step produced no payload (or an empty
        one); the alert job exits 0 without fetching or sending anything."""
        rc, sender, executed, fetched = _run_alerts(tmp_path, payload)
        assert rc == 0
        assert executed == []
        assert sender.messages == []
        assert fetched == set()


class TestDispatchSplit:
    """Regression alerts on transitions only; on-demand reports everything."""

    @pytest.mark.parametrize(
        ("workflow", "expect_actions", "expect_target_shas"),
        [
            pytest.param(
                "regression", 1, {"t" * 40, "b" * 40},
                id="regression-alerts-only-on-transitions",
            ),
            pytest.param(
                "ondemand", 2, {"t" * 40, "b" * 40, "u" * 40},
                id="ondemand-reports-every-result",
            ),
        ],
    )
    def test_records_dispatched_per_workflow(
        self,
        tmp_path: Path,
        workflow: str,
        expect_actions: int,
        expect_target_shas: set[str],
    ) -> None:
        """Scenario: one alertable transition plus one steady passing
        result.  Regression alerts (and spends title lookups) on the
        transition only; on-demand covers both."""
        # Arrange
        payload = {
            "results": [
                _record(),
                _record(
                    downstream="alglib", repo="org/alglib",
                    episode_state="passing", outcome="passed",
                    target_commit="u" * 40, first_known_bad=None,
                ),
            ],
            "run_url": "https://example.com/runs/1",
        }

        # Act
        rc, sender, executed, fetched = _run_alerts(tmp_path, payload, workflow=workflow)

        # Assert
        assert rc == 0
        assert len(executed) == expect_actions
        assert expect_target_shas <= fetched
        missed = {"u" * 40} - expect_target_shas
        assert not (missed & fetched), (
            "regression must not spend title lookups on non-alertable records"
        )
        assert sender.messages == [], "no error outcomes, so no error notice"

    def test_error_outcomes_send_one_error_notice(self, tmp_path: Path) -> None:
        """Scenario: error outcomes never alert individually (silent by
        design) but the run sends a single aggregate error notice so a
        permanently-erroring downstream is not invisible."""
        # Arrange
        payload = {
            "results": [
                _record(episode_state="error", outcome="error"),
                _record(
                    downstream="alglib", repo="org/alglib",
                    episode_state="error", outcome="error",
                ),
            ],
        }

        # Act
        rc, sender, executed, _ = _run_alerts(tmp_path, payload)

        # Assert
        assert rc == 0
        assert executed == [], "error outcomes are not alertable transitions"
        assert len(sender.messages) == 1, "exactly one aggregate error notice"
        stream, topic, content = sender.messages[0]
        assert (stream, topic) == ("Hopscotch", "Downstream alerts")
        assert "2" in content, "the notice reports how many builds errored"
