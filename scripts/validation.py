"""Tool invocation, result building, and artifact management for hopscotch."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.git_ops import git_url_from_manifest
from scripts.models import (
    CommitDetail,
    DownstreamConfig,
    Outcome,
    ValidationResult,
    WindowSelection,
    utc_now,
)


# ---------------------------------------------------------------------------
# Artifact paths
# ---------------------------------------------------------------------------


def commit_plan_artifact_path(output_dir: Path) -> Path:
    """Return the artifact path for the full commit list text file."""

    return output_dir / "tested-commits.txt"


def selection_artifact_path(output_dir: Path) -> Path:
    """Return the artifact path for the window-selection payload."""

    return output_dir / "selection.json"


def selection_summary_path(output_dir: Path) -> Path:
    """Return the artifact path for the human-readable selection summary."""

    return output_dir / "selection-summary.md"


# ---------------------------------------------------------------------------
# Commit-plan rendering
# ---------------------------------------------------------------------------


def render_commit_plan(
    *,
    label: str,
    commits: list[CommitDetail],
    truncated: bool = False,
    bisect_window: bool = False,
) -> str:
    """Render the ordered upstream commits selected for this downstream run."""

    if not commits:
        return ""
    lines = [label]
    for commit in commits:
        lines.append(f"- {commit.sha} {commit.title}")
    if truncated:
        lines.append(f"window truncated to {len(commits)} commits")
    if bisect_window:
        lines.append("bisect will probe a subset of this ordered window")
    return "\n".join(lines) + "\n"


def append_commit_plan_artifact(
    *,
    output_dir: Path,
    label: str,
    commits: list[CommitDetail],
    truncated: bool = False,
    bisect_window: bool = False,
) -> None:
    """Append one full commit-plan section to the artifact text file."""

    if not commits:
        return
    artifact_path = commit_plan_artifact_path(output_dir)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    with artifact_path.open("a") as handle:
        if artifact_path.stat().st_size > 0:
            handle.write("\n")
        handle.write(
            render_commit_plan(
                label=label,
                commits=commits,
                truncated=truncated,
                bisect_window=bisect_window,
            )
        )


def print_commit_plan_summary(
    *,
    downstream: str,
    label: str,
    commits: list[CommitDetail],
    artifact_path: Path,
) -> None:
    """Print a short summary of the selected upstream commits."""

    if not commits:
        return
    plural = "commit" if len(commits) == 1 else "commits"
    print(f"[{downstream}] {label}: {len(commits)} {plural} (full list: {artifact_path.name})")


# ---------------------------------------------------------------------------
# Selection summary
# ---------------------------------------------------------------------------


def short_commit_label(commit: str | None) -> str:
    """Return a short stable label for a commit hash in human summaries."""

    if commit is None:
        return "unknown"
    return commit[:12]


def render_selection_summary(selection: WindowSelection) -> str:
    """Render the end-of-step summary for window selection."""

    lines = [
        "# Window Selection Summary",
        "",
        f"- Downstream: `{selection.downstream or 'unknown'}`",
        f"- Upstream ref: `{selection.upstream_ref or 'unknown'}`",
        f"- Target commit: `{short_commit_label(selection.target_commit)}`",
    ]
    head_probe = selection.head_probe_outcome or "unknown"
    if selection.head_probe_failure_stage is not None:
        head_probe = f"{head_probe} (stage={selection.head_probe_failure_stage})"
    lines.append(f"- Head probe outcome: `{head_probe}`")
    if selection.selected_lower_bound_commit is not None:
        lines.append(f"- Selected lower bound: `{short_commit_label(selection.selected_lower_bound_commit)}`")
    if selection.needs_probe:
        lines.append(f"- Bisect window size: `{len(selection.tested_commits)}` commits")
    lines.append("")
    lines.append("Decision:")
    lines.append(selection.decision_reason or "No decision reason recorded.")
    lines.append("")
    lines.append("Plan:")
    lines.append(selection.next_action or "No next action recorded.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Exit-code classification
# ---------------------------------------------------------------------------


def classify_exit_code(exit_code: int) -> Outcome:
    """Classify a `hopscotch` process exit code into the result schema."""

    if exit_code == 0:
        return Outcome.PASSED
    if exit_code == 1:
        return Outcome.FAILED
    return Outcome.ERROR


# ---------------------------------------------------------------------------
# Tool invocation
# ---------------------------------------------------------------------------


def copy_tool_artifacts(project_dir: Path, output_dir: Path) -> None:
    """Copy `.lake/hopscotch` into the workflow artifact directory."""

    state_root = project_dir / ".lake" / "hopscotch"
    if not state_root.exists():
        return
    destination = output_dir / "tool-state"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(state_root, destination)


def parse_state_file(output_dir: Path) -> dict[str, Any]:
    """Read the copied tool state when the CLI reached its persistence layer."""

    state_path = output_dir / "tool-state" / "state.json"
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text())


def parse_summary_file(output_dir: Path) -> str | None:
    """Read the copied human summary if the tool produced one."""

    summary_path = output_dir / "tool-state" / "summary.md"
    if not summary_path.exists():
        return None
    return summary_path.read_text().strip() or None


def tool_summary_text(tool_run: subprocess.CompletedProcess[str], tool_summary: str | None) -> str:
    """Return the preferred human-readable summary for one tool invocation."""

    return (
        tool_summary
        or tool_run.stdout.strip()
        or tool_run.stderr.strip()
        or "hopscotch did not emit a summary"
    )


def invoke_tool(
    config: DownstreamConfig,
    from_ref: str,
    to_ref: str,
    project_dir: Path,
    output_dir: Path,
    env: dict[str, str],
    tool_exe: Path | None,
    bisect: bool = False,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the Lean executable, streaming logs to the console and artifact files."""

    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = output_dir / "tool-stdout.txt"
    stderr_path = output_dir / "tool-stderr.txt"
    git_url = git_url_from_manifest(project_dir, config.dependency_name)
    command = (
        [
            str(tool_exe),
            "dep",
            config.dependency_name,
            "--from",
            from_ref,
            "--to",
            to_ref,
            "--project-dir",
            str(project_dir),
            "--scan-mode",
            "bisect" if bisect else "linear",
            *(["--git-url", git_url] if git_url else []),
            *(["--quiet"] if quiet else []),
        ]
    )

    # Output the command we are about to run
    print(f"Running: {' '.join(command)}")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )
    captured_lines: list[str] = []
    with stdout_path.open("w") as stdout_file, stderr_path.open("w") as stderr_file:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            stdout_file.write(line)
            stderr_file.write(line)
            captured_lines.append(line)
        return_code = process.wait()
    combined_output = "".join(captured_lines)
    return subprocess.CompletedProcess(
        process.args,
        return_code,
        stdout=combined_output,
        stderr=combined_output,
    )


def run_validation_attempt(
    *,
    config: DownstreamConfig,
    from_ref: str,
    to_ref: str,
    project_dir: Path,
    output_dir: Path,
    tested_commits: list[str],
    env: dict[str, str],
    tool_exe: Path | None,
    bisect: bool = False,
    quiet: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], str | None]:
    """Run one `hopscotch` attempt and return the process, state, and summary.

    `from_ref` and `to_ref` are the exclusive lower and inclusive upper commit
    bounds passed to `hopscotch` via `--from`/`--to`.  The tool fetches
    the commit range from the GitHub API so the caller does not need to write a
    commits file.  `tested_commits` is retained for Python-side reporting
    (commit counts, details written to `selection.json`).
    """
    tool_run = invoke_tool(
        config,
        from_ref,
        to_ref,
        project_dir,
        output_dir,
        env,
        tool_exe,
        bisect=bisect,
        quiet=quiet,
    )
    copy_tool_artifacts(project_dir, output_dir)
    return tool_run, parse_state_file(output_dir), parse_summary_file(output_dir)


# ---------------------------------------------------------------------------
# Result building
# ---------------------------------------------------------------------------


def build_result_from_tool(
    *,
    config: DownstreamConfig,
    downstream_commit: str | None,
    upstream_ref: str,
    target_commit: str | None,
    search_mode: str,
    tested_commits: list[str],
    tested_commit_details: list[CommitDetail],
    truncated: bool,
    tool_run: subprocess.CompletedProcess[str],
    state: dict[str, Any],
    tool_summary: str | None,
    head_probe_outcome: str | None = None,
    head_probe_failure_stage: str | None = None,
    head_probe_summary: str | None = None,
    pinned_commit: str | None = None,
) -> ValidationResult:
    """Translate the tool output into the JSON schema used by reporting."""

    summary = tool_summary_text(tool_run, tool_summary)
    base: dict[str, Any] = dict(
        schema_version=1,
        downstream=config.name,
        repo=config.repo,
        default_branch=config.default_branch,
        downstream_commit=downstream_commit,
        dependency_name=config.dependency_name,
        upstream_ref=upstream_ref,
        target_commit=target_commit,
        search_mode=search_mode,
        tested_commits=tested_commits,
        tested_commit_details=tested_commit_details,
        commit_window_truncated=truncated,
        summary=summary,
        generated_at=utc_now(),
        head_probe_outcome=head_probe_outcome,
        head_probe_failure_stage=head_probe_failure_stage,
        head_probe_summary=head_probe_summary,
        pinned_commit=pinned_commit,
    )

    if tool_run.returncode == 0:
        base.update(
            outcome=Outcome.PASSED,
            failure_stage=None,
            first_failing_commit=None,
            last_successful_commit=state.get("lastSuccessfulCommit", target_commit),
            error=None,
            culprit_log_path=None,
        )
    elif tool_run.returncode == 1:
        base.update(
            outcome=Outcome.FAILED,
            failure_stage=state.get("stage"),
            first_failing_commit=state.get("currentCommit"),
            last_successful_commit=state.get("lastSuccessfulCommit"),
            error=None,
            culprit_log_path=state.get("lastLogPath"),
        )
    else:
        base.update(
            outcome=Outcome.ERROR,
            failure_stage="runner",
            first_failing_commit=None,
            last_successful_commit=state.get("lastSuccessfulCommit"),
            error=tool_run.stderr.strip() or tool_run.stdout.strip() or "hopscotch failed before producing state",
            culprit_log_path=state.get("lastLogPath"),
        )

    return ValidationResult(**base)


def build_error_result(
    config: DownstreamConfig,
    upstream_ref: str,
    error: str,
) -> ValidationResult:
    """Build a structured error result when setup failed before the tool ran."""

    return ValidationResult(
        schema_version=1,
        downstream=config.name,
        repo=config.repo,
        default_branch=config.default_branch,
        downstream_commit=None,
        dependency_name=config.dependency_name,
        upstream_ref=upstream_ref,
        target_commit=None,
        search_mode="setup-error",
        tested_commits=[],
        tested_commit_details=[],
        commit_window_truncated=False,
        outcome=Outcome.ERROR,
        failure_stage="setup",
        first_failing_commit=None,
        last_successful_commit=None,
        summary=error,
        error=error,
        generated_at=utc_now(),
        culprit_log_path=None,
    )


def build_selection_error_result(
    selection: WindowSelection,
    error: str,
) -> ValidationResult:
    """Build a structured error result using the metadata from a selection step."""

    return ValidationResult(
        schema_version=1,
        downstream=selection.downstream or "unknown",
        repo=selection.repo or "unknown",
        default_branch=selection.default_branch or "unknown",
        downstream_commit=selection.downstream_commit,
        dependency_name=selection.dependency_name,
        upstream_ref=selection.upstream_ref or "master",
        target_commit=selection.target_commit,
        search_mode=selection.search_mode,
        tested_commits=selection.tested_commits,
        tested_commit_details=selection.tested_commit_details,
        commit_window_truncated=selection.commit_window_truncated,
        outcome=Outcome.ERROR,
        failure_stage="setup",
        first_failing_commit=None,
        last_successful_commit=None,
        summary=error,
        error=error,
        generated_at=utc_now(),
        head_probe_outcome=selection.head_probe_outcome,
        head_probe_failure_stage=selection.head_probe_failure_stage,
        head_probe_summary=selection.head_probe_summary,
        culprit_log_path=None,
    )


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------


def write_result(path: Path, result: ValidationResult) -> None:
    """Persist the machine-readable result JSON."""

    path.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n")


def write_selection(path: Path, selection: WindowSelection) -> None:
    """Persist the machine-readable window-selection JSON."""

    path.write_text(json.dumps(selection.to_json(), indent=2, sort_keys=True) + "\n")


def load_selection(path: Path) -> WindowSelection:
    """Load the persisted window-selection JSON payload."""

    return WindowSelection.from_json(json.loads(path.read_text()))
