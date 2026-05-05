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
    AlertAction,
    DryRunSender,
    _MATHLIB_COMMIT_URL,
    _MATHLIB_RELEASE_URL,
    compute_alert_actions,
    execute_alerts,
    fetch_commit_titles,
    format_error_notice_message,
    format_new_failure_message,
    format_ondemand_compatible_message,
    format_ondemand_failure_message,
    format_ondemand_skipped_message,
    format_recovered_message,
    format_summary_message,
    _commit_link_with_title,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RUN_URL = "https://github.com/owner/repo/actions/runs/123"


_STREAM = "Hopscotch"
_TOPIC = "Downstream alerts"


def _make_record(
    downstream: str = "physlib",
    repo: str = "owner/physlib",
    downstream_commit: str | None = "ds1234ds5678",
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
        "repo": repo,
        "downstream_commit": downstream_commit,
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
# Tests: _commit_link_with_title (tag display)
# ---------------------------------------------------------------------------


class CommitLinkWithTagTests(unittest.TestCase):
    """Tag-name display logic in _commit_link_with_title."""

    def test_tag_used_as_display_label(self) -> None:
        """Scenario: when sha_to_tag maps the SHA, the tag name is shown instead of the short SHA."""
        sha = "abc123def456" + "0" * 16
        link = _commit_link_with_title(sha, sha_to_tag={sha: "v4.19.0"})
        self.assertIn("[`v4.19.0`]", link)
        self.assertNotIn(f"[`{sha[:12]}`]", link)

    def test_tag_link_points_to_commit_url(self) -> None:
        """Scenario: a tagged commit still links to the GitHub commit URL."""
        sha = "abc123def456" + "0" * 16
        link = _commit_link_with_title(sha, sha_to_tag={sha: "v4.19.0"})
        self.assertIn(f"{_MATHLIB_COMMIT_URL}/{sha}", link)

    def test_tag_suppresses_commit_title(self) -> None:
        """Scenario: when a tag is present, the commit title is not appended."""
        sha = "abc123def456" + "0" * 16
        link = _commit_link_with_title(
            sha,
            commit_titles={sha: "feat: some feature"},
            sha_to_tag={sha: "v4.19.0"},
        )
        self.assertNotIn("feat: some feature", link)

    def test_no_tag_falls_back_to_short_sha_and_title(self) -> None:
        """Scenario: when sha_to_tag has no entry, the short SHA and title are used as normal."""
        sha = "abc123def456" + "0" * 16
        link = _commit_link_with_title(
            sha,
            commit_titles={sha: "feat: some feature"},
            sha_to_tag={},
        )
        self.assertIn(f"[`{sha[:12]}`]", link)
        self.assertIn("feat: some feature", link)


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
        """Scenario: the culprit log excerpt is embedded in a spoiler block."""
        msg = format_new_failure_message(
            _make_record(culprit_log_text="error: type mismatch"), _RUN_URL
        )
        self.assertIn("error: type mismatch", msg)
        self.assertIn("```spoiler", msg)

    def test_culprit_log_absent_means_no_spoiler(self) -> None:
        """Scenario: when culprit_log_text is None, no spoiler block is added."""
        msg = format_new_failure_message(_make_record(culprit_log_text=None), _RUN_URL)
        self.assertNotIn("```spoiler", msg)

    def test_missing_fields_use_placeholders(self) -> None:
        """Scenario: missing commit fields display as '(unknown)'."""
        msg = format_new_failure_message(
            _make_record(first_known_bad=None, target_commit=None), _RUN_URL
        )
        self.assertIn("(unknown)", msg)

    def test_target_commit_is_linked_sha(self) -> None:
        """Scenario: the target commit SHA is rendered as a clickable GitHub link."""
        sha = "deadbeefcafe1234abcd5678"
        msg = format_new_failure_message(_make_record(target_commit=sha), _RUN_URL)
        self.assertIn(f"{_MATHLIB_COMMIT_URL}/{sha}", msg)

    def test_first_known_bad_is_linked_sha(self) -> None:
        """Scenario: the first known bad commit SHA is rendered as a clickable GitHub link."""
        sha = "cafebabe123456abcdef7890"
        msg = format_new_failure_message(_make_record(first_known_bad=sha), _RUN_URL)
        self.assertIn(f"{_MATHLIB_COMMIT_URL}/{sha}", msg)

    def test_includes_commit_title_when_provided(self) -> None:
        """Scenario: when commit_titles supplies a title for first_known_bad, it appears after the link."""
        sha = "cafebabe123456abcdef7890"
        msg = format_new_failure_message(
            _make_record(first_known_bad=sha),
            _RUN_URL,
            commit_titles={sha: "feat: add some feature"},
        )
        self.assertIn("feat: add some feature", msg)

    def test_run_link_uses_downstream_validation_run_label(self) -> None:
        """Scenario: the CI run link reads 'Downstream validation run', not 'CI run'."""
        msg = format_new_failure_message(_make_record(), _RUN_URL)
        self.assertIn("Downstream validation run", msg)
        self.assertNotIn("CI run", msg)

    def test_title_includes_downstream_commit_link(self) -> None:
        """Scenario: the downstream commit SHA is linked in the message title."""
        sha = "ds1234ds5678abcdef90"
        msg = format_new_failure_message(
            _make_record(downstream_commit=sha, repo="owner/physlib"), _RUN_URL
        )
        self.assertIn(f"https://github.com/owner/physlib/commit/{sha}", msg)

    def test_uses_target_mathlib_commit_label(self) -> None:
        """Scenario: the target commit bullet is labelled 'Target Mathlib commit'."""
        msg = format_new_failure_message(_make_record(), _RUN_URL)
        self.assertIn("Target Mathlib commit", msg)

    def test_downstream_commit_title_shown_when_provided(self) -> None:
        """Scenario: when commit_titles has a title for the downstream commit, it appears after a dash."""
        sha = "ds1234ds5678abcdef90"
        msg = format_new_failure_message(
            _make_record(downstream_commit=sha, repo="owner/physlib"),
            _RUN_URL,
            commit_titles={sha: "chore: bump mathlib"},
        )
        self.assertIn("chore: bump mathlib", msg)

    def test_tagged_commit_shows_tag_name(self) -> None:
        """Scenario: when sha_to_tag maps the first_known_bad SHA, the tag name appears instead of the short SHA."""
        sha = "bad123bad456" + "0" * 16
        msg = format_new_failure_message(
            _make_record(first_known_bad=sha),
            _RUN_URL,
            sha_to_tag={sha: "v4.19.0"},
        )
        self.assertIn("[`v4.19.0`]", msg)
        self.assertNotIn(f"[`{sha[:12]}`]", msg)


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

    def test_target_commit_is_linked_sha(self) -> None:
        """Scenario: the target commit SHA is rendered as a clickable GitHub link."""
        sha = "deadbeefcafe1234abcd5678"
        msg = format_recovered_message(_make_record(target_commit=sha), _RUN_URL)
        self.assertIn(f"{_MATHLIB_COMMIT_URL}/{sha}", msg)

    def test_previous_first_known_bad_is_linked_sha(self) -> None:
        """Scenario: the previous first known bad commit SHA is rendered as a clickable GitHub link."""
        sha = "cafebabe123456abcdef7890"
        msg = format_recovered_message(
            _make_record(previous_first_known_bad=sha), _RUN_URL
        )
        self.assertIn(f"{_MATHLIB_COMMIT_URL}/{sha}", msg)

    def test_run_link_uses_downstream_validation_run_label(self) -> None:
        """Scenario: the CI run link reads 'Downstream validation run', not 'CI run'."""
        msg = format_recovered_message(_make_record(), _RUN_URL)
        self.assertIn("Downstream validation run", msg)
        self.assertNotIn("CI run", msg)

    def test_title_includes_downstream_commit_link(self) -> None:
        """Scenario: the downstream commit SHA is linked in the message title."""
        sha = "ds1234ds5678abcdef90"
        msg = format_recovered_message(
            _make_record(downstream_commit=sha, repo="owner/physlib", episode_state="recovered", outcome="passed"),
            _RUN_URL,
        )
        self.assertIn(f"https://github.com/owner/physlib/commit/{sha}", msg)

    def test_uses_target_mathlib_commit_label(self) -> None:
        """Scenario: the target commit bullet is labelled 'Target Mathlib commit'."""
        msg = format_recovered_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertIn("Target Mathlib commit", msg)

    def test_uses_previous_known_bad_label(self) -> None:
        """Scenario: the previous failure commit bullet is labelled 'Previous known-bad'."""
        msg = format_recovered_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertIn("Previous known-bad", msg)

    def test_tagged_commit_shows_tag_name(self) -> None:
        """Scenario: when sha_to_tag maps the previous_first_known_bad SHA, the tag name appears instead of the short SHA."""
        sha = "oldbadbad123" + "0" * 16
        msg = format_recovered_message(
            _make_record(previous_first_known_bad=sha, episode_state="recovered", outcome="passed"),
            _RUN_URL,
            sha_to_tag={sha: "v4.18.0"},
        )
        self.assertIn("[`v4.18.0`]", msg)
        self.assertNotIn(f"[`{sha[:12]}`]", msg)

    def test_release_tag_shown_when_present(self) -> None:
        """Scenario: last_good_release in the record yields a linked release line."""
        msg = format_new_failure_message(
            _make_record(last_good_release="v4.13.0"), _RUN_URL
        )
        self.assertIn("v4.13.0", msg)
        self.assertIn(f"{_MATHLIB_RELEASE_URL}/v4.13.0", msg)
        self.assertIn("Last compatible release", msg)

    def test_release_tag_absent_when_none(self) -> None:
        """Scenario: no last_good_release → release line is omitted."""
        msg = format_new_failure_message(_make_record(), _RUN_URL)
        self.assertNotIn("Last compatible release", msg)
        self.assertNotIn(_MATHLIB_RELEASE_URL, msg)

    def test_recovered_release_tag_shown_when_present(self) -> None:
        """Scenario: last_good_release in a recovered record yields a compatible-release line."""
        msg = format_recovered_message(
            _make_record(episode_state="recovered", outcome="passed", last_good_release="v4.13.0"),
            _RUN_URL,
        )
        self.assertIn("v4.13.0", msg)
        self.assertIn(f"{_MATHLIB_RELEASE_URL}/v4.13.0", msg)
        self.assertIn("Compatible with release", msg)

    def test_recovered_release_tag_absent_when_none(self) -> None:
        """Scenario: no last_good_release in recovered record → release line omitted."""
        msg = format_recovered_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertNotIn("Compatible with release", msg)
        self.assertNotIn(_MATHLIB_RELEASE_URL, msg)


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
    last_good_release: str | None = None,
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
        "last_good_release": last_good_release,
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
        self.assertIn("| Downstream | Status | Good release | First Bad | Safe commits |", msg)
        self.assertNotIn("Target", msg)
        self.assertNotIn("Last Good", msg)

    def test_last_release_shown_when_present(self) -> None:
        """Scenario: when last_good_release is set, a linked tag name appears in the Last release column."""
        msg = format_summary_message(
            _SUMMARY_RUN_META,
            [_make_summary_row(last_good_release="v4.13.0")],
        )
        self.assertIn("v4.13.0", msg)
        self.assertIn(f"{_MATHLIB_RELEASE_URL}/v4.13.0", msg)

    def test_last_release_dash_when_absent(self) -> None:
        """Scenario: when last_good_release is None, the Last release column shows a dash."""
        msg = format_summary_message(
            _SUMMARY_RUN_META,
            [_make_summary_row(last_good_release=None)],
        )
        # "— |" appears as the release column dash (release column precedes first_bad)
        self.assertIn("| — |", msg)

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


# ---------------------------------------------------------------------------
# Tests: format_ondemand_failure_message
# ---------------------------------------------------------------------------


class FormatOndemandFailureMessageTests(unittest.TestCase):
    """Message rendering for on-demand incompatibility alerts."""

    def test_includes_downstream_name(self) -> None:
        """Scenario: the message mentions which downstream is incompatible."""
        msg = format_ondemand_failure_message(_make_record(), _RUN_URL)
        self.assertIn("physlib", msg)

    def test_incompatibility_headline(self) -> None:
        """Scenario: the headline uses incompatibility language, not regression language."""
        msg = format_ondemand_failure_message(_make_record(), _RUN_URL)
        self.assertIn("incompatible with the targeted Mathlib revision", msg)
        self.assertNotIn("regression", msg)
        self.assertNotIn("New regression", msg)

    def test_includes_first_known_bad(self) -> None:
        """Scenario: the culprit commit SHA is included."""
        msg = format_ondemand_failure_message(
            _make_record(first_known_bad="deadbeef1234"), _RUN_URL
        )
        self.assertIn("deadbeef1234", msg)

    def test_includes_failure_stage(self) -> None:
        """Scenario: the failure stage is mentioned."""
        msg = format_ondemand_failure_message(
            _make_record(failure_stage="test"), _RUN_URL
        )
        self.assertIn("test", msg)

    def test_includes_run_url(self) -> None:
        """Scenario: a link to the CI run is included."""
        msg = format_ondemand_failure_message(_make_record(), _RUN_URL)
        self.assertIn(_RUN_URL, msg)

    def test_includes_culprit_log_when_present(self) -> None:
        """Scenario: the culprit log excerpt is embedded in a spoiler block."""
        msg = format_ondemand_failure_message(
            _make_record(culprit_log_text="error: type mismatch"), _RUN_URL
        )
        self.assertIn("error: type mismatch", msg)
        self.assertIn("```spoiler", msg)

    def test_culprit_log_absent_means_no_spoiler(self) -> None:
        """Scenario: when culprit_log_text is None, no spoiler block is added."""
        msg = format_ondemand_failure_message(_make_record(culprit_log_text=None), _RUN_URL)
        self.assertNotIn("```spoiler", msg)

    def test_includes_downstream_commit_link(self) -> None:
        """Scenario: the downstream commit SHA is linked in the message headline."""
        sha = "ds1234ds5678abcdef90"
        msg = format_ondemand_failure_message(
            _make_record(downstream_commit=sha, repo="owner/physlib"), _RUN_URL
        )
        self.assertIn(f"https://github.com/owner/physlib/commit/{sha}", msg)

    def test_uses_target_mathlib_commit_label(self) -> None:
        """Scenario: the target commit bullet is labelled 'Target Mathlib commit'."""
        msg = format_ondemand_failure_message(_make_record(), _RUN_URL)
        self.assertIn("Target Mathlib commit", msg)

    def test_release_tag_shown_when_present(self) -> None:
        """Scenario: last_good_release yields a last-compatible-release line."""
        msg = format_ondemand_failure_message(
            _make_record(last_good_release="v4.13.0"), _RUN_URL
        )
        self.assertIn("v4.13.0", msg)
        self.assertIn(f"{_MATHLIB_RELEASE_URL}/v4.13.0", msg)
        self.assertIn("Last compatible release", msg)

    def test_release_tag_absent_when_none(self) -> None:
        """Scenario: no last_good_release → release line is omitted."""
        msg = format_ondemand_failure_message(_make_record(), _RUN_URL)
        self.assertNotIn("Last compatible release", msg)
        self.assertNotIn(_MATHLIB_RELEASE_URL, msg)


# ---------------------------------------------------------------------------
# Tests: format_ondemand_compatible_message
# ---------------------------------------------------------------------------


class FormatOndemandCompatibleMessageTests(unittest.TestCase):
    """Message rendering for on-demand compatibility (recovered) alerts."""

    def test_includes_downstream_name(self) -> None:
        """Scenario: the message mentions which downstream is compatible."""
        msg = format_ondemand_compatible_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertIn("physlib", msg)

    def test_compatibility_headline(self) -> None:
        """Scenario: the headline uses compatibility language, not recovery language."""
        msg = format_ondemand_compatible_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertIn("compatible with the targeted Mathlib revision", msg)
        self.assertNotIn("recovered", msg)
        self.assertNotIn("regression", msg)

    def test_includes_run_url(self) -> None:
        """Scenario: a link to the CI run is included."""
        msg = format_ondemand_compatible_message(_make_record(), _RUN_URL)
        self.assertIn(_RUN_URL, msg)

    def test_includes_previous_first_known_bad(self) -> None:
        """Scenario: the previously-bad commit is referenced."""
        msg = format_ondemand_compatible_message(
            _make_record(previous_first_known_bad="oldbadbad123"), _RUN_URL
        )
        self.assertIn("oldbadbad123", msg)

    def test_uses_previous_known_bad_label(self) -> None:
        """Scenario: the previous failure commit bullet is labelled 'Previous known-bad'."""
        msg = format_ondemand_compatible_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertIn("Previous known-bad", msg)

    def test_includes_downstream_commit_link(self) -> None:
        """Scenario: the downstream commit SHA is linked in the message headline."""
        sha = "ds1234ds5678abcdef90"
        msg = format_ondemand_compatible_message(
            _make_record(downstream_commit=sha, repo="owner/physlib", episode_state="recovered", outcome="passed"),
            _RUN_URL,
        )
        self.assertIn(f"https://github.com/owner/physlib/commit/{sha}", msg)

    def test_release_tag_shown_when_present(self) -> None:
        """Scenario: last_good_release yields a compatible-release line."""
        msg = format_ondemand_compatible_message(
            _make_record(episode_state="recovered", outcome="passed", last_good_release="v4.13.0"),
            _RUN_URL,
        )
        self.assertIn("v4.13.0", msg)
        self.assertIn(f"{_MATHLIB_RELEASE_URL}/v4.13.0", msg)
        self.assertIn("Compatible with release", msg)

    def test_release_tag_absent_when_none(self) -> None:
        """Scenario: no last_good_release → release line is omitted."""
        msg = format_ondemand_compatible_message(
            _make_record(episode_state="recovered", outcome="passed"), _RUN_URL
        )
        self.assertNotIn("Compatible with release", msg)
        self.assertNotIn(_MATHLIB_RELEASE_URL, msg)


# ---------------------------------------------------------------------------
# Tests: compute_alert_actions with workflow parameter
# ---------------------------------------------------------------------------


class ComputeAlertActionsWorkflowTests(unittest.TestCase):
    """Verify that workflow='ondemand' switches to on-demand formatters."""

    def test_ondemand_failure_uses_incompatibility_language(self) -> None:
        """Scenario: workflow='ondemand' new_failure produces incompatibility headline."""
        records = [_make_record(episode_state="new_failure")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC, workflow="ondemand")
        self.assertEqual(len(actions), 1)
        self.assertIn("incompatible with the targeted Mathlib revision", actions[0].content)
        self.assertNotIn("New regression", actions[0].content)

    def test_ondemand_recovered_uses_compatibility_language(self) -> None:
        """Scenario: workflow='ondemand' recovered produces compatibility headline."""
        records = [_make_record(episode_state="recovered", outcome="passed")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC, workflow="ondemand")
        self.assertEqual(len(actions), 1)
        self.assertIn("compatible with the targeted Mathlib revision", actions[0].content)
        self.assertNotIn("recovered", actions[0].content)

    def test_regression_failure_uses_regression_language(self) -> None:
        """Scenario: workflow='regression' (default) new_failure produces regression headline."""
        records = [_make_record(episode_state="new_failure")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC, workflow="regression")
        self.assertEqual(len(actions), 1)
        self.assertIn("New regression detected", actions[0].content)

    def test_regression_is_default(self) -> None:
        """Scenario: omitting workflow defaults to regression formatters."""
        records = [_make_record(episode_state="new_failure")]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC)
        self.assertEqual(len(actions), 1)
        self.assertIn("New regression detected", actions[0].content)

    def test_ondemand_reports_all_non_error_states(self) -> None:
        """Scenario: workflow='ondemand' reports all non-error states."""
        records = [
            _make_record(episode_state="passing"),
            _make_record(downstream="b", episode_state="failing"),
            _make_record(downstream="c", episode_state="new_failure"),
        ]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC, workflow="ondemand")
        self.assertEqual(len(actions), 3)
        self.assertEqual(
            [a.downstream for a in actions],
            ["physlib", "b", "c"],
        )

    def test_ondemand_errors_still_filtered(self) -> None:
        """Scenario: workflow='ondemand' still filters out error states."""
        records = [
            _make_record(episode_state="passing"),
            _make_record(downstream="b", episode_state="error"),
        ]
        actions = compute_alert_actions(records, _RUN_URL, _STREAM, _TOPIC, workflow="ondemand")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].downstream, "physlib")

    def test_ondemand_includes_skipped_downstreams(self) -> None:
        """Scenario: workflow='ondemand' includes skipped downstreams in alerts."""
        records = [_make_record(episode_state="passing")]
        skipped = [
            {
                "downstream": "skipped-ds",
                "repo": "owner/skipped-ds",
                "downstream_commit": "abc123",
                "outcome": "failed",
                "first_known_bad": "bad456",
                "target_commit": "target789",
            }
        ]
        actions = compute_alert_actions(
            records, _RUN_URL, _STREAM, _TOPIC, workflow="ondemand", skipped=skipped,
        )
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[1].downstream, "skipped-ds")
        self.assertIn("was not retested", actions[1].content)


class FormatOndemandSkippedMessageTests(unittest.TestCase):
    """Tests for format_ondemand_skipped_message."""

    def test_includes_downstream_name(self) -> None:
        """Scenario: message contains the downstream name."""
        record = {
            "downstream": "MyProject",
            "repo": "owner/MyProject",
            "downstream_commit": "abc123def456",
            "outcome": "passed",
            "target_commit": "target789",
        }
        msg = format_ondemand_skipped_message(record, _RUN_URL)
        self.assertIn("MyProject", msg)

    def test_compatible_outcome_label(self) -> None:
        """Scenario: passed outcome shows 'compatible'."""
        record = {
            "downstream": "ds",
            "repo": "owner/ds",
            "downstream_commit": "abc",
            "outcome": "passed",
            "target_commit": "target",
        }
        msg = format_ondemand_skipped_message(record, _RUN_URL)
        self.assertIn("compatible", msg)
        self.assertNotIn("incompatible", msg)

    def test_incompatible_outcome_label(self) -> None:
        """Scenario: failed outcome shows 'incompatible'."""
        record = {
            "downstream": "ds",
            "repo": "owner/ds",
            "downstream_commit": "abc",
            "outcome": "failed",
            "first_known_bad": "bad123",
            "target_commit": "target",
        }
        msg = format_ondemand_skipped_message(record, _RUN_URL)
        self.assertIn("incompatible", msg)

    def test_includes_first_known_bad_when_failed(self) -> None:
        """Scenario: first known bad is shown when outcome is failed."""
        record = {
            "downstream": "ds",
            "repo": "owner/ds",
            "downstream_commit": "abc",
            "outcome": "failed",
            "first_known_bad": "bad123456789",
            "target_commit": "target",
        }
        msg = format_ondemand_skipped_message(record, _RUN_URL)
        self.assertIn("bad123456789", msg)

    def test_includes_previous_job_url(self) -> None:
        """Scenario: previous validation link is shown when available."""
        record = {
            "downstream": "ds",
            "repo": "owner/ds",
            "downstream_commit": "abc",
            "outcome": "passed",
            "target_commit": "target",
            "previous_job_url": "https://example.com/job/42",
        }
        msg = format_ondemand_skipped_message(record, _RUN_URL)
        self.assertIn("https://example.com/job/42", msg)

    def test_skipped_headline(self) -> None:
        """Scenario: message headline indicates downstream was not retested."""
        record = {
            "downstream": "ds",
            "repo": "owner/ds",
            "downstream_commit": "abc",
            "outcome": "passed",
            "target_commit": "target",
        }
        msg = format_ondemand_skipped_message(record, _RUN_URL)
        self.assertIn("was not retested", msg)


if __name__ == "__main__":
    unittest.main()
