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

_MATHLIB_COMMIT_URL = "https://github.com/leanprover-community/mathlib4/commit"
_COMMIT_TITLE_MAX = 60  # truncate commit titles to this many characters


def _short_sha(sha: str | None) -> str:
    """Return the first 12 characters of a SHA, or '(unknown)'."""
    return sha[:12] if sha else "(unknown)"


def _commit_link_with_title(
    sha: str | None,
    commit_titles: dict[str, str] | None = None,
    sha_to_tag: dict[str, str] | None = None,
) -> str:
    """Return a Zulip markdown link for a mathlib commit SHA.

    If *sha* is None, returns the placeholder ``(unknown)``.  When *sha_to_tag*
    maps the SHA to a tag name (e.g. ``v4.19.0``), the tag is used as the
    display label and no commit title is appended (the tag is self-descriptive).
    Otherwise, the short SHA is shown and *commit_titles* is consulted for an
    optional title suffix, truncated to ``_COMMIT_TITLE_MAX`` characters.
    """
    if sha is None:
        return "(unknown)"
    url = f"{_MATHLIB_COMMIT_URL}/{sha}"
    tags = sha_to_tag or {}
    tag = tags.get(sha)
    if tag:
        return f"[`{tag}`]({url})"
    short = _short_sha(sha)
    link = f"[`{short}`]({url})"
    titles = commit_titles or {}
    title = titles.get(sha, "")
    if title:
        if len(title) > _COMMIT_TITLE_MAX:
            title = title[:_COMMIT_TITLE_MAX - 3] + "..."
        return f"{link} {title}"
    return link


def _downstream_commit_link(
    sha: str | None,
    repo: str | None,
    commit_titles: dict[str, str] | None = None,
) -> str:
    """Return a Zulip markdown link for a downstream commit SHA with optional title.

    Links to ``https://github.com/{repo}/commit/{sha}``.  Title (if found in
    *commit_titles*) is appended after a `` - `` separator.
    """
    if not sha:
        return "(unknown)"
    short = _short_sha(sha)
    if not repo:
        return f"`{short}`"
    url = f"https://github.com/{repo}/commit/{sha}"
    link = f"[`{short}`]({url})"
    titles = commit_titles or {}
    title = titles.get(sha, "")
    if title:
        if len(title) > _COMMIT_TITLE_MAX:
            title = title[:_COMMIT_TITLE_MAX - 3] + "..."
        return f"{link} - {title}"
    return link


def format_new_failure_message(
    record: dict[str, Any],
    run_url: str,
    commit_titles: dict[str, str] | None = None,
    sha_to_tag: dict[str, str] | None = None,
) -> str:
    """Render a Zulip message for a newly opened regression episode.

    Includes linked commit SHAs with optional titles, failure stage, a link to
    the downstream validation run, and a spoiler block with the failure log when
    one is present.
    """
    downstream = record["downstream"]
    target_sha = record.get("target_commit")
    first_bad_sha = record.get("first_known_bad")
    failure_stage = record.get("failure_stage") or "unknown"
    ds_commit = record.get("downstream_commit")
    ds_repo = record.get("repo")

    target_link = _commit_link_with_title(target_sha, commit_titles, sha_to_tag)
    first_bad_link = _commit_link_with_title(first_bad_sha, commit_titles, sha_to_tag)
    ds_link = _downstream_commit_link(ds_commit, ds_repo, commit_titles)

    lines = [
        f"**New regression detected in {downstream}",
        "",
        f"- {downstream} at: {ds_link}"
        f"- Target Mathlib commit: {target_link}",
        f"- First known bad: {first_bad_link}",
        f"- Failure stage: {failure_stage}",
        "",
        f"[Downstream validation run]({run_url})",
    ]

    culprit_log = record.get("culprit_log_text")
    if culprit_log:
        lines.extend(["", "```spoiler Failure log", culprit_log, "```"])

    return "\n".join(lines)


def format_recovered_message(
    record: dict[str, Any],
    run_url: str,
    commit_titles: dict[str, str] | None = None,
    sha_to_tag: dict[str, str] | None = None,
) -> str:
    """Render a Zulip message for a downstream that has recovered."""
    downstream = record["downstream"]
    target_sha = record.get("target_commit")
    prev_bad_sha = record.get("previous_first_known_bad")
    ds_commit = record.get("downstream_commit")
    ds_repo = record.get("repo")

    target_link = _commit_link_with_title(target_sha, commit_titles, sha_to_tag)
    prev_bad_link = _commit_link_with_title(prev_bad_sha, commit_titles, sha_to_tag)
    ds_link = _downstream_commit_link(ds_commit, ds_repo, commit_titles)

    lines = [
        f"**{downstream} has recovered (at: {ds_link})**",
        "",
        f"- Target Mathlib commit: {target_link}",
        f"- Previous known-bad: {prev_bad_link}",
        "",
        f"[Downstream validation run]({run_url})",
    ]
    return "\n".join(lines)


def format_ondemand_failure_message(
    record: dict[str, Any],
    run_url: str,
    commit_titles: dict[str, str] | None = None,
    sha_to_tag: dict[str, str] | None = None,
) -> str:
    """Render a Zulip message for a downstream that is incompatible with the targeted Mathlib revision."""
    downstream = record["downstream"]
    target_sha = record.get("target_commit")
    first_bad_sha = record.get("first_known_bad")
    failure_stage = record.get("failure_stage") or "unknown"
    ds_commit = record.get("downstream_commit")
    ds_repo = record.get("repo")

    target_link = _commit_link_with_title(target_sha, commit_titles, sha_to_tag)
    first_bad_link = _commit_link_with_title(first_bad_sha, commit_titles, sha_to_tag)
    ds_link = _downstream_commit_link(ds_commit, ds_repo, commit_titles)

    lines = [
        f"## [On-demand hopscotch run]({run_url})",
        f"**{downstream} is incompatible with the targeted Mathlib revision**",
        "",
        f"- {downstream} at: {ds_link}"
        f"- Target Mathlib commit: {target_link}",
        f"- First known bad: {first_bad_link}",
        f"- Failure stage: {failure_stage}"
    ]

    culprit_log = record.get("culprit_log_text")
    if culprit_log:
        lines.extend(["", "```spoiler Failure log", culprit_log, "```"])

    return "\n".join(lines)


def format_ondemand_compatible_message(
    record: dict[str, Any],
    run_url: str,
    commit_titles: dict[str, str] | None = None,
    sha_to_tag: dict[str, str] | None = None,
) -> str:
    """Render a Zulip message for a downstream that is compatible with the targeted Mathlib revision."""
    downstream = record["downstream"]
    target_sha = record.get("target_commit")
    prev_bad_sha = record.get("previous_first_known_bad")
    ds_commit = record.get("downstream_commit")
    ds_repo = record.get("repo")

    target_link = _commit_link_with_title(target_sha, commit_titles, sha_to_tag)
    prev_bad_link = _commit_link_with_title(prev_bad_sha, commit_titles, sha_to_tag)
    ds_link = _downstream_commit_link(ds_commit, ds_repo, commit_titles)

    lines = [
        f"## [On-demand hopscotch run]({run_url})",
        f"**{downstream} is compatible with the targeted Mathlib revision**",
        "",
        f"- {downstream} at: {ds_link}"
        f"- Target Mathlib commit: {target_link}",
        f"- Previous known-bad: {prev_bad_link}"
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
    commit_titles: dict[str, str] | None = None,
    sha_to_tag: dict[str, str] | None = None,
    workflow: str = "regression",
) -> list[AlertAction]:
    """Determine which alerts to send from aggregated run results.

    Only ``new_failure`` and ``recovered`` transitions produce alerts.
    All alerts are sent to the same *stream* / *topic*.

    *commit_titles* is an optional ``{full_sha: title}`` mapping; pass the
    result of ``fetch_commit_titles()`` to annotate SHAs with their commit
    message subject lines.  *sha_to_tag* is an optional ``{full_sha: tag_name}``
    mapping; when present, tagged commits are displayed by tag name instead of
    short SHA.

    *workflow* selects the message formatters: ``"regression"`` (default) uses
    regression language; ``"ondemand"`` uses bumping-branch compatibility language.
    """
    actions: list[AlertAction] = []
    for record in records:
        episode_state = record.get("episode_state", "")
        if episode_state not in ALERTABLE_STATES:
            continue

        downstream_name = record.get("downstream", "")

        if workflow == "ondemand":
            if episode_state == "new_failure":
                content = format_ondemand_failure_message(record, run_url, commit_titles, sha_to_tag)
            else:
                content = format_ondemand_compatible_message(record, run_url, commit_titles, sha_to_tag)
        else:
            if episode_state == "new_failure":
                content = format_new_failure_message(record, run_url, commit_titles, sha_to_tag)
            else:
                content = format_recovered_message(record, run_url, commit_titles, sha_to_tag)

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
_GITHUB_API = "https://api.github.com"

# Zulip emoji for each episode state / outcome.
_STATUS_EMOJI: dict[str, str] = {
    "passing": ":check:",
    "new_failure": ":cross_mark:",
    "failing": ":cross_mark:",
    "recovered": ":check:",
    "error": ":warning:",
}


def fetch_tags(
    repo: str = _MATHLIB_REPO,
    token: str | None = None,
    max_pages: int = 5,
) -> dict[str, str]:
    """Return ``{full_sha: tag_name}`` for the most recent tags in *repo*.

    Fetches up to *max_pages* × 100 tags (newest first).  On any error the
    partial result collected so far is returned so callers degrade gracefully.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "downstream-reports/notifications",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    result: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        url = f"{_GITHUB_API}/repos/{repo}/tags?per_page=100&page={page}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if not data:
                break
            for tag in data:
                sha = tag.get("commit", {}).get("sha")
                name = tag.get("name")
                if sha and name and sha not in result:
                    result[sha] = name
        except Exception as exc:
            print(f"  warning: could not fetch tags for {repo} (page {page}): {exc}", file=sys.stderr)
            break
    return result


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
