"""Messaging abstractions and alert logic for downstream status changes.

The module is split into three layers:

1. **Sender protocol** — ``MessageSender`` plus concrete implementations
   (``ZulipSender``, ``DryRunSender``).  Reusable by future periodic digest
   jobs that need to post messages to the same endpoints.

2. **Alert decision** — ``compute_alert_actions`` filters aggregated run
   results to status-change events (``new_failure``, ``recovered``) and
   pairs each with the Zulip destination from the inventory.

3. **Alert execution** — ``execute_alerts`` sends the computed actions via
   the chosen sender, handling per-message errors gracefully.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

try:
    import zulip
except ImportError:  # pragma: no cover — optional at import time
    zulip = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Sender abstraction
# ---------------------------------------------------------------------------

ZULIP_SITE = "https://mathlib-initiative.zulipchat.com"

# Episode states that warrant an alert.  Kept as a module-level constant so
# that callers (and tests) can reference it without duplicating the set.
ALERTABLE_STATES: frozenset[str] = frozenset({"new_failure", "recovered"})


@runtime_checkable
class MessageSender(Protocol):
    """Protocol for posting messages to a stream/topic endpoint.

    Implementations must be usable as a context-free callable — no
    session state is required between calls.  This keeps the protocol
    reusable for both one-shot alert jobs and future periodic digests.
    """

    def send_message(self, stream: str, topic: str, content: str) -> None:
        """Post *content* to *stream* / *topic*."""
        ...


class ZulipSender:
    """Send messages via the ``zulip`` Python package."""

    def __init__(self, *, email: str, api_key: str, site: str = ZULIP_SITE) -> None:
        if zulip is None:
            raise RuntimeError(
                "The 'zulip' package is required for ZulipSender.  "
                "Install it with: pip install zulip"
            )
        self._client = zulip.Client(email=email, api_key=api_key, site=site)

    def send_message(self, stream: str, topic: str, content: str) -> None:
        result = self._client.send_message(
            {"type": "stream", "to": stream, "topic": topic, "content": content}
        )
        if result.get("result") != "success":
            raise RuntimeError(f"Zulip API error: {result}")


class DryRunSender:
    """Log what would be sent without calling any external API."""

    def send_message(self, stream: str, topic: str, content: str) -> None:
        print(f"[dry-run] Would send to #{stream} > {topic}:")
        for line in content.splitlines():
            print(f"  {line}")


# ---------------------------------------------------------------------------
# Alert data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlertAction:
    """One alert to send: the downstream, destination, and rendered content."""

    downstream: str
    stream: str
    topic: str
    content: str


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _short_sha(sha: str | None) -> str:
    """Return the first 12 characters of a SHA, or '(unknown)'."""
    return sha[:12] if sha else "(unknown)"


def format_new_failure_message(record: dict[str, Any], run_url: str) -> str:
    """Render a Zulip message for a newly opened regression episode.

    Includes the culprit commit, failure stage, and a link to the CI run.
    """
    downstream = record["downstream"]
    target = _short_sha(record.get("target_commit"))
    first_bad = _short_sha(record.get("first_known_bad"))
    failure_stage = record.get("failure_stage") or "unknown"

    lines = [
        f"**New regression detected in {downstream}**",
        "",
        f"- Target commit: `{target}`",
        f"- First known bad: `{first_bad}`",
        f"- Failure stage: {failure_stage}",
        f"- [CI run]({run_url})",
    ]

    culprit_log = record.get("culprit_log_text")
    if culprit_log:
        # Truncate long logs to keep the message readable.
        truncated = culprit_log[:2000]
        if len(culprit_log) > 2000:
            truncated += "\n… (truncated)"
        lines.extend(["", "```", truncated, "```"])

    return "\n".join(lines)


def format_recovered_message(record: dict[str, Any], run_url: str) -> str:
    """Render a Zulip message for a downstream that has recovered."""
    downstream = record["downstream"]
    target = _short_sha(record.get("target_commit"))
    prev_bad = _short_sha(record.get("previous_first_known_bad"))

    lines = [
        f"**{downstream} has recovered**",
        "",
        f"- Target commit: `{target}`",
        f"- Previous first known bad: `{prev_bad}`",
        f"- [CI run]({run_url})",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alert computation
# ---------------------------------------------------------------------------


def compute_alert_actions(
    records: list[dict[str, Any]],
    run_url: str,
    stream: str,
    topic: str,
) -> list[AlertAction]:
    """Determine which alerts to send from aggregated run results.

    Only ``new_failure`` and ``recovered`` transitions produce alerts.
    All alerts are sent to the same *stream* / *topic*.
    """
    actions: list[AlertAction] = []
    for record in records:
        episode_state = record.get("episode_state", "")
        if episode_state not in ALERTABLE_STATES:
            continue

        downstream_name = record.get("downstream", "")

        if episode_state == "new_failure":
            content = format_new_failure_message(record, run_url)
        else:
            content = format_recovered_message(record, run_url)

        actions.append(
            AlertAction(
                downstream=downstream_name,
                stream=stream,
                topic=topic,
                content=content,
            )
        )
    return actions


# ---------------------------------------------------------------------------
# Alert execution
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Summary formatting
# ---------------------------------------------------------------------------

# Zulip emoji for each episode state / outcome.
_STATUS_EMOJI: dict[str, str] = {
    "passing": ":check:",
    "new_failure": ":cross_mark:",
    "failing": ":cross_mark:",
    "recovered": ":check:",
    "error": ":warning:",
}


def format_summary_message(
    run_meta: dict[str, Any],
    rows: list[dict[str, Any]],
) -> str:
    """Render a compact Zulip markdown table summarising all downstream states.

    *run_meta* and *rows* are the values returned by
    ``storage.load_run_for_site()``.  The table is designed to be readable
    inline in a Zulip stream without expanding a collapsible section.
    """
    run_url = run_meta.get("run_url", "")
    upstream_ref = run_meta.get("upstream_ref", "master")
    reported_at = run_meta.get("reported_at", "")

    header_lines = [
        f"**Mathlib Hopscotch — latest state** (upstream ref: `{upstream_ref}`)",
        f"Run: [{run_meta.get('run_id', '?')}]({run_url}) — reported {reported_at}",
        "",
    ]

    table_lines = [
        "| Downstream | Status | Target | First Bad | Last Good |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        downstream = row.get("downstream", "?")
        episode = row.get("episode_state", "")
        emoji = _STATUS_EMOJI.get(episode, "")
        status = f"{emoji} {episode}" if emoji else episode
        target = _short_sha(row.get("target_commit"))
        first_bad = _short_sha(row.get("first_known_bad")) if row.get("first_known_bad") else "—"
        last_good = _short_sha(row.get("last_known_good")) if row.get("last_known_good") else "—"
        table_lines.append(
            f"| {downstream} | {status} | `{target}` | {first_bad} | {last_good} |"
        )

    n_passed = sum(1 for r in rows if r.get("outcome") == "passed")
    n_failed = sum(1 for r in rows if r.get("outcome") == "failed")
    n_error = sum(1 for r in rows if r.get("outcome") == "error")
    footer = f"\n{n_passed} compatible · {n_failed} incompatible · {n_error} errors"

    return "\n".join(header_lines + table_lines) + footer


def execute_alerts(actions: list[AlertAction], sender: MessageSender) -> None:
    """Send all computed alerts via *sender*.

    Each alert is sent independently; a failure for one downstream does
    not prevent alerts for others.  Errors are printed to stderr.
    """
    for action in actions:
        try:
            sender.send_message(action.stream, action.topic, action.content)
            print(f"Sent {action.downstream} alert to #{action.stream} > {action.topic}")
        except Exception as exc:
            print(
                f"Failed to send alert for {action.downstream}: {exc}",
                file=sys.stderr,
            )
