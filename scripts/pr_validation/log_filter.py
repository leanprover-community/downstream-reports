"""Build-log filtering for PR-validation surface area.

The filter mirrors ``filter_culprit_log_text`` in ``scripts/aggregate_results``:
drop successful-target ticks (``✔``) and lake trace lines (``trace: .>``) so the
log tail shown in PR comments / step summaries is the actual failure context,
not surrounding noise.

Kept local to ``pr_validation/`` rather than imported from ``aggregate_results``
because the PR-validation pipeline is otherwise standalone and does not share
state with the regression scripts (see ``CLAUDE.md``).
"""

from __future__ import annotations

from pathlib import Path

_DROP_PREFIXES = ("✔", "trace: .>")


def is_noise_line(line: str) -> bool:
    stripped = line.strip()
    return any(stripped.startswith(prefix) for prefix in _DROP_PREFIXES)


def filter_log_text(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not is_noise_line(line))


def read_log_tail(log_path: Path, max_chars: int) -> str:
    """Return up to ``max_chars`` from the end of the filtered ``log_path``, or '' on miss."""
    if not log_path.exists():
        return ""
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        return ""
    filtered = filter_log_text(text)
    return filtered[-max_chars:] if len(filtered) > max_chars else filtered
