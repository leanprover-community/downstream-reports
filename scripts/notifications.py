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

import json
import sys
import urllib.request
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


def format_error_notice_message(n_error: int, run_url: str) -> str:
    """Render a notice for builds that failed with unexpected errors."""
    noun = "build" if n_error == 1 else "builds"
    return (
        f":warning: {n_error} {noun} failed unexpectedly. "
        f"Please see the [CI run]({run_url}) for details."
    )


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

_MATHLIB_REPO = "leanprover-community/mathlib4"
_MATHLIB_COMMIT_URL = "https://github.com/leanprover-community/mathlib4/commit"
_GITHUB_API = "https://api.github.com"
_COMMIT_TITLE_MAX = 60  # truncate first-bad commit titles to this many characters

# Zulip emoji for each episode state / outcome.
_STATUS_EMOJI: dict[str, str] = {
    "passing": ":check:",
    "new_failure": ":cross_mark:",
    "failing": ":cross_mark:",
    "recovered": ":check:",
    "error": ":warning:",
}


def fetch_commit_titles(
    shas: list[str],
    repo: str = _MATHLIB_REPO,
    token: str | None = None,
) -> dict[str, str]:
    """Return ``{sha: first_line_of_commit_message}`` for each SHA.

    Uses the GitHub Commits API.  Missing or failed lookups are omitted from
    the result dict — callers should treat a missing key as an unknown title.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "downstream-reports/notifications",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result: dict[str, str] = {}
    for sha in shas:
        url = f"{_GITHUB_API}/repos/{repo}/commits/{sha}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            message = data.get("commit", {}).get("message", "")
            first_line = message.splitlines()[0] if message else ""
            if first_line:
                result[sha] = first_line
        except Exception as exc:
            print(f"  warning: could not fetch commit title for {sha[:12]}: {exc}", file=sys.stderr)
    return result


def format_summary_message(
    run_meta: dict[str, Any],
    rows: list[dict[str, Any]],
    commit_titles: dict[str, str] | None = None,
) -> str:
    """Render a compact Zulip markdown table summarising all downstream states.

    *run_meta* and *rows* are the values returned by
    ``storage.load_run_for_site()``.  The table is designed to be readable
    inline in a Zulip stream without expanding a collapsible section.

    *commit_titles* is an optional ``{sha: title}`` mapping used to annotate
    the first-known-bad column; pass the result of ``fetch_commit_titles()``
    to populate it.
    """
    run_url = run_meta.get("run_url", "")
    upstream_ref = run_meta.get("upstream_ref", "master")
    reported_at = run_meta.get("reported_at", "")

    header_lines = [
        f"**Mathlib Downstreams — latest state** (upstream ref: `{upstream_ref}`) | [Full report](https://leanprover-community.github.io/downstream-reports/)",
        f"Run: [{run_meta.get('run_id', '?')}]({run_url}) — reported {reported_at}",
        "",
    ]

    table_lines = [
        "| Downstream | Status | First Bad | Safe commits |",
        "|---|---|---|---|",
    ]
    titles = commit_titles or {}
    for row in sorted(rows, key=lambda r: (r.get("downstream") or "").lower()):
        name = row.get("downstream") or "?"
        repo = row.get("repo") or name
        downstream_cell = f"[{name}](https://github.com/{repo})"
        episode = row.get("episode_state", "")
        status = _STATUS_EMOJI.get(episode, episode)

        first_bad_sha = row.get("first_known_bad")
        if first_bad_sha:
            short = _short_sha(first_bad_sha)
            link = f"[{short}]({_MATHLIB_COMMIT_URL}/{first_bad_sha})"
            title = titles.get(first_bad_sha, "")
            if title:
                if len(title) > _COMMIT_TITLE_MAX:
                    title = title[:_COMMIT_TITLE_MAX - 3] + "..."
                first_bad_cell = f"{link} {title}"
            else:
                first_bad_cell = link
        else:
            first_bad_cell = "—"

        bump = row.get("bump_commits")
        bump_cell = str(bump) if bump is not None else "—"

        table_lines.append(f"| {downstream_cell} | {status} | {first_bad_cell} | {bump_cell} |")

    n_passed = sum(1 for r in rows if r.get("outcome") == "passed")
    n_failed = sum(1 for r in rows if r.get("outcome") == "failed")
    n_error = sum(1 for r in rows if r.get("outcome") == "error")
    counts = f"{n_passed} compatible · {n_failed} incompatible · {n_error} errors"

    header_lines.insert(1, counts)

    spoiler = "```spoiler Details\n" + "\n".join(table_lines) + "\n```"

    return "\n".join(header_lines) + "\n" + spoiler


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
