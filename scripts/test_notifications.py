#!/usr/bin/env python3
"""Unit tests for the notifications module (alert logic, formatting, senders)."""

from __future__ import annotations

import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

# Ensure the repo root is on sys.path so `scripts.*` imports work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.notifications import (
    ALERTABLE_STATES,
    AlertAction,
    DryRunSender,
    _MATHLIB_COMMIT_URL,
    compute_alert_actions,
    execute_alerts,
    fetch_commit_titles,
    format_error_notice_message,
    format_new_failure_message,
    format_recovered_message,
    format_summary_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_URL = "https://github.com/owner/repo/actions/runs/123"


_STREAM = "Hopscotch"
_TOPIC = "Downstream alerts"


def _make_record(
    downstream: str = "physlib",
    episode_state: str = "new_failure",
    outcome: str = "failed",
    target_commit: str = "abc123def456",
    first_known_bad: str = "bad123bad456",
    last_known_good: str = "good12good34",
    previous_first_known_bad: str | None = None,
    failure_stage: str | None = "build",
    culprit_log_text: str | None = None,
    **kwargs,
) -> dict:
    """Build a minimal serialized RunResultRecord dict."""
    record = {
        "downstream": downstream,
        "episode_state": episode_state,
        "outcome": outcome,
        "target_commit": target_commit,
        "first_known_bad": first_known_bad,
        "last_known_good": last_known_good,
        "previous_first_known_bad": previous_first_known_bad,
        "failure_stage": failure_stage,
        "culprit_log_text": culprit_log_text,
    }
    record.update(kwargs)
    return record


# ---------------------------------------------------------------------------
# Tests: compute_alert_actions
# ---------------------------------------------------------------------------


class ComputeAlertActionsTests(unittest.TestCase):
    """Alert eligibility logic for episode state transitions."""

    def test_new_failure_produces_alert(self) -> None:
        """Scenario: a NEW_FAILURE transition triggers an alert with the downstream name."""
        records = [_make_record(episode_state="new_failure")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].downstream, "physlib")

    def test_recovered_produces_alert(self) -> None:
        """Scenario: a RECOVERED transition triggers an alert."""
        records = [_make_record(episode_state="recovered", outcome="passed")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC)
        self.assertEqual(len(actions), 1)

    def test_passing_does_not_trigger(self) -> None:
        """Scenario: a stable PASSING state does not trigger an alert."""
        records = [_make_record(episode_state="passing", outcome="passed")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC)
        self.assertEqual(len(actions), 0)

    def test_failing_does_not_trigger(self) -> None:
        """Scenario: an ongoing FAILING state does not trigger an alert."""
        records = [_make_record(episode_state="failing")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC)
        self.assertEqual(len(actions), 0)

    def test_error_does_not_trigger(self) -> None:
        """Scenario: an ERROR state does not trigger an alert."""
        records = [_make_record(episode_state="error", outcome="error")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC)
        self.assertEqual(len(actions), 0)

    def test_multiple_downstreams(self) -> None:
        """Scenario: only alertable transitions produce actions; stable states are skipped."""
        records = [
            _make_record(downstream="physlib", episode_state="new_failure"),
            _make_record(downstream="other", episode_state="recovered", outcome="passed"),
            _make_record(downstream="stable", episode_state="passing", outcome="passed"),
        ]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC)
        self.assertEqual(len(actions), 2)
        self.assertEqual({a.downstream for a in actions}, {"physlib", "other"})

    def test_action_uses_provided_stream_and_topic(self) -> None:
        """Scenario: all actions use the stream/topic passed as arguments."""
        records = [_make_record(episode_state="new_failure")]
        actions = compute_alert_actions(records, _RUN_URL, "my-stream", "my-topic")
        self.assertEqual(actions[0].stream, "my-stream")
        self.assertEqual(actions[0].topic, "my-topic")


# ---------------------------------------------------------------------------
# Tests: format_new_failure_message
# ---------------------------------------------------------------------------


class FormatNewFailureMessageTests(unittest.TestCase):
    """Message rendering for new regression alerts."""

    def test_includes_downstream_name(self) -> None:
        """Scenario: the message mentions which downstream regressed."""
        msg = format_new_failure_message(_make_record(), _RUN_URL)
        self.assertIn("physlib", msg)

    def test_includes_first_known_bad(self) -> None:
        """Scenario: the culprit commit SHA is included."""
        msg = format_new_failure_message(
            _make_record(first_known_bad="deadbeef1234"), _RUN_URL
        )
        self.assertIn("deadbeef1234", msg)

    def test_includes_failure_stage(self) -> None:
        """Scenario: the failure stage (build/test) is mentioned."""
        msg = format_new_failure_message(
            _make_record(failure_stage="test"), _RUN_URL
        )
        self.assertIn("test", msg)

    def test_includes_run_url(self) -> None:
        """Scenario: a link to the CI run is included."""
        msg = format_new_failure_message(_make_record(), _RUN_URL)
        self.assertIn(_RUN_URL, msg)

    def test_includes_culprit_log_when_present(self) -> None:
        """Scenario: the culprit log excerpt is embedded in the message."""
        msg = format_new_failure_message(
            _make_record(culprit_log_text="error: type mismatch"), _RUN_URL
        )
        self.assertIn("error: type mismatch", msg)

    def test_truncates_long_culprit_log(self) -> None:
        """Scenario: a very long culprit log is truncated."""
        long_log = "x" * 3000
        msg = format_new_failure_message(
            _make_record(culprit_log_text=long_log), _RUN_URL
        )
        self.assertIn("… (truncated)", msg)
        # The full 3000-char log should not appear.
        self.assertNotIn(long_log, msg)

    def test_missing_fields_use_placeholders(self) -> None:
        """Scenario: missing commit fields display as '(unknown)'."""
        msg = format_new_failure_message(
            _make_record(first_known_bad=None, target_commit=None), _RUN_URL
        )
        self.assertIn("(unknown)", msg)


# ---------------------------------------------------------------------------
# Tests: format_recovered_message
# ---------------------------------------------------------------------------


class FormatRecoveredMessageTests(unittest.TestCase):
    """Message rendering for recovery alerts."""

    def test_includes_downstream_name(self) -> None:
        """Scenario: the message mentions which downstream recovered."""
        msg = format_recovered_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertIn("physlib", msg)

    def test_includes_previous_first_known_bad(self) -> None:
        """Scenario: the previously-bad commit is referenced."""
        msg = format_recovered_message(
            _make_record(previous_first_known_bad="oldbadbad123"), _RUN_URL
        )
        self.assertIn("oldbadbad123", msg)

    def test_includes_run_url(self) -> None:
        """Scenario: a link to the CI run is included."""
        msg = format_recovered_message(_make_record(), _RUN_URL)
        self.assertIn(_RUN_URL, msg)


# ---------------------------------------------------------------------------
# Tests: DryRunSender
# ---------------------------------------------------------------------------


class DryRunSenderTests(unittest.TestCase):
    """DryRunSender logs messages without sending them."""

    def test_logs_message_to_stdout(self) -> None:
        """Scenario: DryRunSender prints the message content to stdout."""
        sender = DryRunSender()
        captured = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured
            sender.send_message("stream", "topic", "hello world")
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        self.assertIn("stream", output)
        self.assertIn("topic", output)
        self.assertIn("hello world", output)

    def test_does_not_raise(self) -> None:
        """Scenario: DryRunSender never raises."""
        sender = DryRunSender()
        sender.send_message("s", "t", "c")


# ---------------------------------------------------------------------------
# Tests: execute_alerts
# ---------------------------------------------------------------------------


class ExecuteAlertsTests(unittest.TestCase):
    """Alert execution with mock senders."""

    def test_sends_all_actions(self) -> None:
        """Scenario: each action triggers exactly one send_message call."""
        sender = MagicMock()
        actions = [
            AlertAction("a", "s1", "t1", "msg1"),
            AlertAction("b", "s2", "t2", "msg2"),
        ]
        execute_alerts(actions, sender)
        self.assertEqual(sender.send_message.call_count, 2)

    def test_sender_error_does_not_halt(self) -> None:
        """Scenario: a send failure for one downstream does not prevent subsequent alerts."""
        sender = MagicMock()
        sender.send_message.side_effect = [RuntimeError("fail"), None]
        actions = [
            AlertAction("a", "s1", "t1", "msg1"),
            AlertAction("b", "s2", "t2", "msg2"),
        ]
        execute_alerts(actions, sender)
        # Both were attempted despite the first failing.
        self.assertEqual(sender.send_message.call_count, 2)

    def test_empty_actions_is_noop(self) -> None:
        """Scenario: no actions means no calls to the sender."""
        sender = MagicMock()
        execute_alerts([], sender)
        sender.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: ALERTABLE_STATES constant
# ---------------------------------------------------------------------------


class AlertableStatesTests(unittest.TestCase):
    """Verify the alertable states constant."""

    def test_contains_new_failure_and_recovered(self) -> None:
        """Scenario: only new_failure and recovered are alertable."""
        self.assertEqual(ALERTABLE_STATES, {"new_failure", "recovered"})


# ---------------------------------------------------------------------------
# Helpers: summary tests
# ---------------------------------------------------------------------------

_SUMMARY_RUN_META = {
    "run_id": "12345",
    "run_url": "https://github.com/owner/repo/actions/runs/12345",
    "upstream_ref": "master",
    "reported_at": "2026-04-08 10:00 UTC",
}


def _make_summary_row(
    downstream: str = "physlib",
    repo: str = "owner/physlib",
    outcome: str = "passed",
    episode_state: str = "passing",
    target_commit: str = "aabbccddee11",
    first_known_bad: str | None = None,
    last_known_good: str | None = "aabbccddee11",
    bump_commits: int | None = None,
    **kwargs,
) -> dict:
    row = {
        "downstream": downstream,
        "repo": repo,
        "outcome": outcome,
        "episode_state": episode_state,
        "target_commit": target_commit,
        "first_known_bad": first_known_bad,
        "last_known_good": last_known_good,
        "bump_commits": bump_commits,
    }
    row.update(kwargs)
    return row


# ---------------------------------------------------------------------------
# Tests: format_summary_message
# ---------------------------------------------------------------------------


class FormatSummaryMessageTests(unittest.TestCase):
    """Summary table rendering for Zulip digest messages."""

    def test_renders_table_inside_spoiler(self) -> None:
        """Scenario: the table is wrapped in a Zulip spoiler block so it is collapsible, with the correct columns."""
        msg = format_summary_message(_SUMMARY_RUN_META, [_make_summary_row()])
        self.assertIn("```spoiler", msg)
        self.assertIn("| Downstream | Status | First Bad | Safe commits |", msg)
        self.assertNotIn("Target", msg)
        self.assertNotIn("Last Good", msg)

    def test_includes_all_downstreams_as_short_name_links(self) -> None:
        """Scenario: every downstream appears as a GitHub link using the short name as label, sorted case-insensitively by short name."""
        rows = [
            _make_summary_row(downstream="Zeta", repo="owner/Zeta"),
            _make_summary_row(downstream="alpha", repo="owner/alpha"),
            _make_summary_row(downstream="Beta", repo="owner/Beta"),
        ]
        msg = format_summary_message(_SUMMARY_RUN_META, rows)
        self.assertIn("[Zeta](https://github.com/owner/Zeta)", msg)
        self.assertIn("[alpha](https://github.com/owner/alpha)", msg)
        self.assertIn("[Beta](https://github.com/owner/Beta)", msg)
        # case-insensitive: alpha < Beta < Zeta
        self.assertLess(msg.index("alpha"), msg.index("Beta"))
        self.assertLess(msg.index("Beta"), msg.index("Zeta"))

    def test_passing_status_shows_emoji_only(self) -> None:
        """Scenario: a passing downstream shows a check emoji with no state label text."""
        msg = format_summary_message(
            _SUMMARY_RUN_META, [_make_summary_row(episode_state="passing")]
        )
        self.assertIn(":check:", msg)
        self.assertNotIn("passing", msg)

    def test_failing_status_shows_emoji_only(self) -> None:
        """Scenario: a failing downstream shows a cross emoji with no state label text."""
        msg = format_summary_message(
            _SUMMARY_RUN_META,
            [_make_summary_row(episode_state="failing", outcome="failed", first_known_bad="aabbccddee11")],
        )
        self.assertIn(":cross_mark:", msg)
        self.assertNotIn("failing", msg)

    def test_new_failure_status_has_cross_emoji(self) -> None:
        """Scenario: a new_failure downstream shows a cross emoji in the status column."""
        msg = format_summary_message(
            _SUMMARY_RUN_META, [_make_summary_row(episode_state="new_failure")]
        )
        self.assertIn(":cross_mark:", msg)

    def test_error_status_has_warning_emoji(self) -> None:
        """Scenario: an error downstream shows a warning emoji in the status column."""
        msg = format_summary_message(
            _SUMMARY_RUN_META, [_make_summary_row(episode_state="error", outcome="error")]
        )
        self.assertIn(":warning:", msg)

    def test_missing_first_bad_shows_dash(self) -> None:
        """Scenario: when first_known_bad is None, the column displays a dash instead of a link."""
        msg = format_summary_message(
            _SUMMARY_RUN_META, [_make_summary_row(first_known_bad=None)]
        )
        self.assertIn("| — |", msg)

    def test_present_first_bad_is_linkified(self) -> None:
        """Scenario: when first_known_bad is set, its short SHA appears as a GitHub commit link."""
        msg = format_summary_message(
            _SUMMARY_RUN_META, [_make_summary_row(first_known_bad="deadbeef1234")]
        )
        self.assertIn("deadbeef1234", msg)
        self.assertIn(f"{_MATHLIB_COMMIT_URL}/deadbeef1234", msg)

    def test_commit_title_appended_when_provided(self) -> None:
        """Scenario: when commit_titles contains the first_known_bad SHA, the title is shown after the link."""
        sha = "deadbeef1234"
        msg = format_summary_message(
            _SUMMARY_RUN_META,
            [_make_summary_row(first_known_bad=sha)],
            commit_titles={sha: "feat: add some feature"},
        )
        self.assertIn("feat: add some feature", msg)

    def test_commit_title_truncated_at_60_chars(self) -> None:
        """Scenario: commit titles longer than 60 characters are truncated with ellipsis."""
        sha = "deadbeef1234"
        long_title = "x" * 70
        msg = format_summary_message(
            _SUMMARY_RUN_META,
            [_make_summary_row(first_known_bad=sha)],
            commit_titles={sha: long_title},
        )
        self.assertIn("...", msg)
        self.assertNotIn("x" * 70, msg)

    def test_bump_commits_shown_when_set(self) -> None:
        """Scenario: when bump_commits is an integer, it appears in the Bump column."""
        msg = format_summary_message(
            _SUMMARY_RUN_META,
            [_make_summary_row(bump_commits=42)],
        )
        self.assertIn("| 42 |", msg)

    def test_bump_commits_dash_when_none(self) -> None:
        """Scenario: when bump_commits is None, the Bump column shows a dash."""
        msg = format_summary_message(
            _SUMMARY_RUN_META,
            [_make_summary_row(bump_commits=None)],
        )
        # The last column should be a dash; check the row ends with "| — |"
        self.assertIn("| — |", msg)

    def test_includes_run_metadata(self) -> None:
        """Scenario: the header includes the upstream ref, run ID, and a link to the run."""
        msg = format_summary_message(_SUMMARY_RUN_META, [_make_summary_row()])
        self.assertIn("master", msg)
        self.assertIn("12345", msg)
        self.assertIn(_SUMMARY_RUN_META["run_url"], msg)

    def test_counts_appear_before_spoiler(self) -> None:
        """Scenario: the compatible/incompatible/error counts appear in the header, before the spoiler block."""
        rows = [
            _make_summary_row(outcome="passed"),
            _make_summary_row(downstream="a", repo="owner/a", outcome="passed"),
            _make_summary_row(downstream="b", repo="owner/b", outcome="failed"),
            _make_summary_row(downstream="c", repo="owner/c", outcome="error"),
        ]
        msg = format_summary_message(_SUMMARY_RUN_META, rows)
        self.assertIn("2 compatible", msg)
        self.assertIn("1 incompatible", msg)
        self.assertIn("1 errors", msg)
        # Counts should appear before the spoiler block
        self.assertLess(msg.index("compatible"), msg.index("```spoiler"))

    def test_empty_rows_produces_spoiler_with_no_data_rows(self) -> None:
        """Scenario: when no downstreams are present, the spoiler still renders with the table header and zero counts."""
        msg = format_summary_message(_SUMMARY_RUN_META, [])
        self.assertIn("```spoiler", msg)
        self.assertIn("| Downstream |", msg)
        self.assertIn("0 compatible", msg)


# ---------------------------------------------------------------------------
# Tests: format_error_notice_message
# ---------------------------------------------------------------------------


class FormatErrorNoticeMessageTests(unittest.TestCase):
    """Error notice message rendering for unexpected build failures."""

    def test_includes_count(self) -> None:
        """Scenario: the number of failed builds appears in the message."""
        msg = format_error_notice_message(3, _RUN_URL)
        self.assertIn("3", msg)

    def test_includes_run_url(self) -> None:
        """Scenario: a link to the CI run is included."""
        msg = format_error_notice_message(1, _RUN_URL)
        self.assertIn(_RUN_URL, msg)

    def test_singular_noun(self) -> None:
        """Scenario: exactly one failure uses the singular 'build'."""
        msg = format_error_notice_message(1, _RUN_URL)
        self.assertIn("1 build ", msg)
        self.assertNotIn("builds", msg)

    def test_plural_noun(self) -> None:
        """Scenario: two or more failures use the plural 'builds'."""
        msg = format_error_notice_message(2, _RUN_URL)
        self.assertIn("builds", msg)


if __name__ == "__main__":
    unittest.main()
