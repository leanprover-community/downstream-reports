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

_DROP_PREFIXES = (
    "✔",        # successful build targets
    "trace: .>",  # lake trace lines
    "info: ",   # lake update / elan progress (cloning, toolchain updates, etc.)
    # GitHub Actions log directives emitted by validate.sh's section /
    # endsection / annotation helpers. These are workflow-UI markers; in a
    # Markdown PR comment they show up as literal text and add nothing.
    "::group::",
    "::endgroup::",
    "::notice",
    "::warning",
    "::error",
    # `lake exe cache get` progress noise. Each download tick of the form
    # `Downloaded: N file(s) [attempted N/M = X%, K KB/s], Decompressed: M`
    # gets emitted dozens of times during a cache get and dominates the
    # failure log otherwise. The trailing summary `Decompressed N file(s)`
    # / `Already decompressed N file(s)` lines are also progress noise.
    "Downloaded: ",
    "Decompressed",
    "Already decompressed",
    "Decompressing",
    # The cache-warning paragraph that always prints when not every olean
    # is in the upstream cache (which is the norm during PR validation,
    # since we test commits CI hasn't built yet).
    "Warning: some files were not found in the cache.",
    "This usually means that your local checkout",
)


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
