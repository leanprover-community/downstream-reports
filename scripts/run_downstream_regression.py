#!/usr/bin/env python3
"""Run the current `hopscotch` executable against one downstream checkout.

The workflow here is intentionally narrow:

1. Resolve the target Mathlib commit and a bounded commit window.
2. Clone the downstream at its default branch head.
3. Invoke `lake exe hopscotch ...` from the tool package.
4. Copy the tool's state and logs into an artifact directory.
5. Emit one JSON result for the reporting step.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class Outcome(str, Enum):
    """Possible outcomes for one downstream validation attempt."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True)
class DownstreamConfig:
    """Inventory entry for one downstream repository."""

    name: str
    repo: str
    default_branch: str
    dependency_name: str = "mathlib"
    enabled: bool = True
    bumping_branch: str | None = None


@dataclass(frozen=True)
class CommitDetail:
    """One Mathlib commit plus the title shown in reports."""

    sha: str
    title: str


@dataclass
class WindowSelection:
    """Persisted output from the pre-probe window-selection step."""

    schema_version: int = 1
    needs_probe: bool = False
    downstream: str | None = None
    repo: str | None = None
    default_branch: str | None = None
    dependency_name: str = "mathlib"
    downstream_commit: str | None = None
    upstream_ref: str | None = None
    target_commit: str | None = None
    search_mode: str = "head-only"
    tested_commits: list[str] = field(default_factory=list)
    tested_commit_details: list[CommitDetail] = field(default_factory=list)
    commit_window_truncated: bool = False
    head_probe_outcome: str | None = None
    head_probe_failure_stage: str | None = None
    head_probe_summary: str | None = None
    pinned_commit: str | None = None
    selected_lower_bound_commit: str | None = None
    decision_reason: str | None = None
    next_action: str | None = None
    # `--from`/`--to` refs for the bisect probe step.  Computed by the window-
    # selection step (which has the local mathlib clone) and stored here so the
    # probe step can invoke the tool without its own mathlib clone.
    probe_from_ref: str | None = None
    probe_to_ref: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WindowSelection":
        """Decode one persisted selection payload."""

        return cls(
            schema_version=payload.get("schema_version", 1),
            needs_probe=payload.get("needs_probe", False),
            downstream=payload.get("downstream"),
            repo=payload.get("repo"),
            default_branch=payload.get("default_branch"),
            dependency_name=payload.get("dependency_name", "mathlib"),
            downstream_commit=payload.get("downstream_commit"),
            upstream_ref=payload.get("upstream_ref"),
            target_commit=payload.get("target_commit"),
            search_mode=payload.get("search_mode", "head-only"),
            tested_commits=payload.get("tested_commits", []),
            tested_commit_details=[
                CommitDetail(**detail) for detail in payload.get("tested_commit_details", [])
            ],
            commit_window_truncated=payload.get("commit_window_truncated", False),
            head_probe_outcome=payload.get("head_probe_outcome"),
            head_probe_failure_stage=payload.get("head_probe_failure_stage"),
            head_probe_summary=payload.get("head_probe_summary"),
            pinned_commit=payload.get("pinned_commit"),
            selected_lower_bound_commit=payload.get("selected_lower_bound_commit"),
            decision_reason=payload.get("decision_reason"),
            next_action=payload.get("next_action"),
            probe_from_ref=payload.get("probe_from_ref"),
            probe_to_ref=payload.get("probe_to_ref"),
        )

    def to_json(self) -> dict[str, Any]:
        """Serialize the selection using plain JSON-compatible values."""

        return asdict(self)


@dataclass
class ValidationResult:
    """Machine-readable result for one downstream validation run."""

    schema_version: int
    downstream: str
    repo: str
    default_branch: str
    downstream_commit: str | None
    dependency_name: str
    upstream_ref: str
    target_commit: str | None
    tested_commits: list[str]
    commit_window_truncated: bool
    outcome: Outcome
    failure_stage: str | None
    first_failing_commit: str | None
    last_successful_commit: str | None
    summary: str
    error: str | None
    generated_at: str
    search_mode: str = "head-only"
    tested_commit_details: list[CommitDetail] = field(default_factory=list)
    head_probe_outcome: str | None = None
    head_probe_failure_stage: str | None = None
    head_probe_summary: str | None = None
    culprit_log_path: str | None = None
    pinned_commit: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Serialize the result using plain JSON-compatible values."""

        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload


def utc_now() -> str:
    """Return a stable UTC timestamp string."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def repo_root() -> Path:
    """Return the repository root that contains the workflow scripts."""

    return Path(__file__).resolve().parent.parent

def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one external command and capture text output."""

    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def git(repo_dir: Path, *git_args: str) -> str:
    """Run `git` in `repo_dir` and return stripped stdout."""

    return run(["git", *git_args], cwd=repo_dir).stdout.strip()


def repo_clone_source(repo: str) -> str:
    """Resolve an inventory `repo` field to a `git clone` source.

    Supported forms:
    - `owner/name` for GitHub repositories
    - absolute or relative local filesystem paths
    - explicit remote URLs such as `https://...` or `git@...`
    """

    repo_path = Path(repo)
    if repo_path.exists():
        return str(repo_path.resolve())
    if "://" in repo or repo.startswith("git@"):
        return repo
    return f"https://github.com/{repo}.git"


def load_inventory(path: Path) -> dict[str, DownstreamConfig]:
    """Load the JSON inventory and index it by downstream name."""

    payload = json.loads(path.read_text())
    entries = payload.get("downstreams", [])
    return {
        entry["name"]: DownstreamConfig(**entry)
        for entry in entries
        if entry.get("enabled", True)
    }


def load_status(path: Path) -> dict[str, Any]:
    """Load the current status file if it exists."""

    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return payload.get("downstreams", {})


def ensure_clean_dir(path: Path) -> None:
    """Replace `path` with an empty directory."""

    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def clone_upstream(repo: str, repo_dir: Path) -> None:
    """Clone an upstream repository with commit history but without file blobs."""

    if repo_dir.exists():
        return
    run(
        [
            "git",
            "clone",
            "--bare",
            "--filter=blob:none",
            "--quiet",
            repo_clone_source(repo),
            str(repo_dir),
        ]
    )


def resolve_upstream_target(repo_dir: Path, upstream_ref: str) -> str:
    """Resolve the requested upstream ref to a commit SHA."""

    try:
        run(["git", "fetch", "--quiet", "origin", upstream_ref], cwd=repo_dir)
        return git(repo_dir, "rev-parse", "FETCH_HEAD")
    except subprocess.CalledProcessError:
        return git(repo_dir, "rev-parse", upstream_ref)


def commit_title(repo_dir: Path, commit: str) -> str:
    """Return the subject line for one Mathlib commit."""

    try:
        return git(repo_dir, "log", "-1", "--format=%s", commit)
    except subprocess.CalledProcessError:
        return "(title unavailable)"


def describe_commits(repo_dir: Path, commits: list[str]) -> list[CommitDetail]:
    """Attach subject lines to a list of Mathlib commits."""

    return [CommitDetail(sha=commit, title=commit_title(repo_dir, commit)) for commit in commits]


def commit_plan_artifact_path(output_dir: Path) -> Path:
    """Return the artifact path for the full commit list text file."""

    return output_dir / "tested-commits.txt"


def selection_artifact_path(output_dir: Path) -> Path:
    """Return the artifact path for the window-selection payload."""

    return output_dir / "selection.json"


def selection_summary_path(output_dir: Path) -> Path:
    """Return the artifact path for the human-readable selection summary."""

    return output_dir / "selection-summary.md"


def render_commit_plan(
    *,
    label: str,
    commits: list[CommitDetail],
    truncated: bool = False,
    bisect_window: bool = False,
) -> str:
    """Render the ordered Mathlib commits selected for this downstream run."""

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
    """Print a short summary of the selected Mathlib commits."""

    if not commits:
        return
    plural = "commit" if len(commits) == 1 else "commits"
    print(f"[{downstream}] {label}: {len(commits)} {plural} (full list: {artifact_path.name})")


def pinned_commit_from_manifest(project_dir: Path, dependency_name: str) -> str | None:
    """Return the resolved SHA for `dependency_name` from `lake-manifest.json`.

    The manifest is Lake's lock file and always records the exact commit SHA that
    was fetched, regardless of what `lakefile.toml` specifies (branch, tag, or
    explicit SHA).  Returns None if the manifest is absent or the dependency is
    not listed.
    """
    manifest_path = project_dir / "lake-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text())
    except Exception:
        return None
    for pkg in payload.get("packages", []):
        if pkg.get("name") == dependency_name and pkg.get("type") == "git":
            rev = pkg.get("rev")
            return rev if isinstance(rev, str) and rev else None
    return None


def pinned_dependency_rev(project_dir: Path, dependency_name: str) -> str | None:
    """Return the dependency rev from `lakefile.toml`, if one is explicitly set.

    This is a fallback for repositories that do not have a `lake-manifest.json`.
    The value may be a branch name or tag rather than a SHA, so callers must
    resolve it via git before use.
    """
    lakefile_path = project_dir / "lakefile.toml"
    if not lakefile_path.exists():
        return None
    payload = tomllib.loads(lakefile_path.read_text())
    for requirement in payload.get("require", []):
        if requirement.get("name") == dependency_name:
            rev = requirement.get("rev")
            return rev if isinstance(rev, str) and rev else None
    return None


def resolve_search_base_commit(
    *,
    project_dir: Path,
    dependency_name: str,
    upstream_dir: Path,
    last_known_good: str | None,
) -> str | None:
    """Resolve the pinned upstream commit for the downstream project.

    Checks sources in order of reliability:
    1. `lake-manifest.json` — Lake's lock file; always a full resolved SHA.
    2. `lakefile.toml` `rev` field — explicit pin, may be a branch/tag name
       that needs git resolution.
    3. `last_known_good` — stored episode state, used when no pin is available.
    """
    manifest_sha = pinned_commit_from_manifest(project_dir, dependency_name)
    if manifest_sha is not None:
        return manifest_sha

    pinned_rev = pinned_dependency_rev(project_dir, dependency_name)
    if pinned_rev is None:
        return last_known_good
    try:
        return resolve_upstream_target(upstream_dir, pinned_rev)
    except subprocess.CalledProcessError:
        return last_known_good


def is_strict_ancestor(repo_dir: Path, older_commit: str, newer_commit: str) -> bool:
    """Return whether `older_commit` is strictly older than `newer_commit` in git history."""

    if older_commit == newer_commit:
        return False
    ancestor_check = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older_commit, newer_commit],
        cwd=repo_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    return ancestor_check.returncode == 0


def should_verify_stored_last_known_good(
    *,
    upstream_dir: Path,
    pinned_commit: str | None,
    stored_last_known_good: str | None,
) -> bool:
    """Return whether a newer stored last-known-good should be re-verified."""

    if pinned_commit is None or stored_last_known_good is None:
        return False
    return is_strict_ancestor(upstream_dir, pinned_commit, stored_last_known_good)


def select_search_base_from_candidates(
    *,
    upstream_dir: Path,
    pinned_commit: str | None,
    last_known_good: str | None,
    verify_last_known_good: Callable[[str], bool] | None = None,
) -> str | None:
    """Choose the lower endpoint from an already-resolved pin and stored state."""

    if should_verify_stored_last_known_good(
        upstream_dir=upstream_dir,
        pinned_commit=pinned_commit,
        stored_last_known_good=last_known_good,
    ):
        if verify_last_known_good is None or last_known_good is None:
            return pinned_commit
        if verify_last_known_good(last_known_good):
            return last_known_good
        return pinned_commit
    if pinned_commit is not None:
        return pinned_commit
    if verify_last_known_good is None or last_known_good is None:
        return last_known_good
    if verify_last_known_good(last_known_good):
        return last_known_good
    return None


def select_search_base_commit(
    *,
    project_dir: Path,
    dependency_name: str,
    upstream_dir: Path,
    last_known_good: str | None,
    verify_last_known_good: Callable[[str], bool] | None = None,
) -> str | None:
    """Choose the lower endpoint for a post-head-failure bisect window."""

    pinned_commit = resolve_search_base_commit(
        project_dir=project_dir,
        dependency_name=dependency_name,
        upstream_dir=upstream_dir,
        last_known_good=last_known_good,
    )
    return select_search_base_from_candidates(
        upstream_dir=upstream_dir,
        pinned_commit=pinned_commit,
        last_known_good=last_known_good,
        verify_last_known_good=verify_last_known_good,
    )


def build_commit_window(
    repo_dir: Path,
    target_commit: str,
    base_commit: str | None,
    max_commits: int,
) -> tuple[list[str], bool]:
    """Return the commit window that will be handed to `hopscotch`.

    If the chosen base commit is available and is an ancestor of the target, the
    tool receives every commit after that point up to the target.
    For long gaps, the returned list stays chronological, starts near that base,
    and still includes the target commit so the bisect endpoint remains valid.
    """

    if base_commit is None or base_commit == target_commit:
        return [target_commit], False

    if not is_strict_ancestor(repo_dir, base_commit, target_commit):
        return [target_commit], False

    commit_output = git(repo_dir, "rev-list", "--reverse", target_commit, f"^{base_commit}")
    commits = [line for line in commit_output.splitlines() if line]
    if not commits:
        return [target_commit], False
    truncated = len(commits) > max_commits
    if truncated:
        commits = commits[: max_commits - 1] + [target_commit]
    return commits, truncated


def should_run_boundary_search(head_probe_exit_code: int, commit_window: list[str]) -> bool:
    """Return whether a failing head probe should be followed by a range search."""

    return head_probe_exit_code == 1 and len(commit_window) > 1


def classify_exit_code(exit_code: int) -> Outcome:
    """Classify a `hopscotch` process exit code into the result schema."""

    if exit_code == 0:
        return Outcome.PASSED
    if exit_code == 1:
        return Outcome.FAILED
    return Outcome.ERROR


def clone_downstream(
    config: DownstreamConfig,
    checkout_dir: Path,
    *,
    clone_source: str | None = None,
) -> str:
    """Clone one downstream repository and return its checked-out commit SHA."""

    ensure_clean_dir(checkout_dir)
    run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            config.default_branch,
            "--quiet",
            clone_source or repo_clone_source(config.repo),
            str(checkout_dir),
        ]
    )
    return git(checkout_dir, "rev-parse", "HEAD")


def downstream_cache_dir(workdir: Path, downstream_name: str) -> Path:
    """Return the per-downstream local Lake artifact cache directory."""

    return workdir / "lake-artifact-cache" / downstream_name


def cache_env(cache_dir: Path) -> dict[str, str]:
    """Build the environment that keeps only mathlib's `.ltar` cache local."""

    env = os.environ.copy()
    env["MATHLIB_CACHE_DIR"] = str(cache_dir / "mathlib")
    return env


def downstream_toolchain(project_dir: Path) -> str:
    """Read the downstream project's pinned Lean toolchain."""

    return (project_dir / "lean-toolchain").read_text().strip()


def downstream_lake_command(project_dir: Path, *lake_args: str) -> list[str]:
    """Build a `lake` command resolved through the downstream toolchain."""

    return ["elan", "run", downstream_toolchain(project_dir), "lake", *lake_args]


def github_cache_scope(repo: str) -> str | None:
    """Return the GitHub `owner/name` scope used by `lake cache get`."""

    repo_path = Path(repo)
    if repo_path.exists():
        return None

    path: str | None
    if "://" not in repo and not repo.startswith("git@"):
        path = repo
    elif repo.startswith("git@github.com:"):
        path = repo.split(":", 1)[1]
    else:
        parsed = urlparse(repo)
        if parsed.netloc.lower() != "github.com":
            return None
        path = parsed.path.lstrip("/")

    path = path.removesuffix(".git").strip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        return None
    return "/".join(parts)


def warm_downstream_cache(
    config: DownstreamConfig,
    *,
    project_dir: Path,
    output_dir: Path,
    env: dict[str, str],
) -> None:
    """Best-effort fetch any published downstream Lake artifacts into the local cache."""

    log_path = output_dir / "downstream-cache-get.log"
    scope = github_cache_scope(config.repo)
    if scope is None:
        log_path.write_text("Skipped `lake cache get`: downstream repo is not a GitHub cache scope.\n")
        return

    command = downstream_lake_command(project_dir, "cache", "get", f"--repo={scope}")
    result = run(command, cwd=project_dir, check=False, env=env)
    log_lines = [
        f"command: {' '.join(command)}",
        f"exit_code: {result.returncode}",
        "",
        "stdout:",
        result.stdout.rstrip(),
        "",
        "stderr:",
        result.stderr.rstrip(),
        "",
    ]
    log_path.write_text("\n".join(log_lines))


def write_commit_list(path: Path, commits: list[str]) -> None:
    """Write the commit list file consumed by `hopscotch`."""

    path.write_text("\n".join(commits) + "\n")


def parent_commit(repo_dir: Path, commit: str) -> str:
    """Return the immediate parent SHA of `commit` in the local repository.

    Used to derive the exclusive `--from` lower bound for `hopscotch`:
    passing `parent_commit(c)` as `--from` causes the tool to fetch exactly
    the single commit `c` (or the range from `c` through `to_ref` if `c` is
    the first element of a multi-commit list).
    """
    return git(repo_dir, "rev-parse", f"{commit}^")


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


def short_commit_label(commit: str | None) -> str:
    """Return a short stable label for a commit hash in human summaries."""

    if commit is None:
        return "unknown"
    return commit[:12]


def tool_summary_text(tool_run: subprocess.CompletedProcess[str], tool_summary: str | None) -> str:
    """Return the preferred human-readable summary for one tool invocation."""

    return (
        tool_summary
        or tool_run.stdout.strip()
        or tool_run.stderr.strip()
        or "hopscotch did not emit a summary"
    )


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
    generated_at = utc_now()
    if tool_run.returncode == 0:
        return ValidationResult(
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
            outcome=Outcome.PASSED,
            failure_stage=None,
            first_failing_commit=None,
            last_successful_commit=state.get("lastSuccessfulCommit", target_commit),
            summary=summary,
            error=None,
            generated_at=generated_at,
            head_probe_outcome=head_probe_outcome,
            head_probe_failure_stage=head_probe_failure_stage,
            head_probe_summary=head_probe_summary,
            culprit_log_path=None,
            pinned_commit=pinned_commit,
        )
    if tool_run.returncode == 1:
        return ValidationResult(
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
            outcome=Outcome.FAILED,
            failure_stage=state.get("stage"),
            first_failing_commit=state.get("currentCommit"),
            last_successful_commit=state.get("lastSuccessfulCommit"),
            summary=summary,
            error=None,
            generated_at=generated_at,
            head_probe_outcome=head_probe_outcome,
            head_probe_failure_stage=head_probe_failure_stage,
            head_probe_summary=head_probe_summary,
            culprit_log_path=state.get("lastLogPath"),
            pinned_commit=pinned_commit,
        )
    return ValidationResult(
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
        outcome=Outcome.ERROR,
        failure_stage="runner",
        first_failing_commit=None,
        last_successful_commit=state.get("lastSuccessfulCommit"),
        summary=summary,
        error=tool_run.stderr.strip() or tool_run.stdout.strip() or "hopscotch failed before producing state",
        generated_at=generated_at,
        head_probe_outcome=head_probe_outcome,
        head_probe_failure_stage=head_probe_failure_stage,
        head_probe_summary=head_probe_summary,
        culprit_log_path=state.get("lastLogPath"),
        pinned_commit=pinned_commit,
    )


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


def write_result(path: Path, result: ValidationResult) -> None:
    """Persist the machine-readable result JSON."""

    path.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n")


def write_selection(path: Path, selection: WindowSelection) -> None:
    """Persist the machine-readable window-selection JSON."""

    path.write_text(json.dumps(selection.to_json(), indent=2, sort_keys=True) + "\n")


def load_selection(path: Path) -> WindowSelection:
    """Load the persisted window-selection JSON payload."""

    return WindowSelection.from_json(json.loads(path.read_text()))


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the workflow runner."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--downstream", required=True)
    parser.add_argument("--upstream-ref", default="master")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-commits", type=int, default=100000)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--tool-exe", type=Path)
    return parser


def main() -> int:
    """Run one downstream validation and always emit a result JSON."""

    args = build_parser().parse_args()
    inventory = load_inventory(args.inventory)
    if args.downstream not in inventory:
        raise SystemExit(f"unknown downstream '{args.downstream}'")

    config = inventory[args.downstream]
    status = load_status(args.status)
    previous = status.get(config.name, {})
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        upstream_dir = args.workdir / "mathlib4.git"
        downstream_dir = args.workdir / "downstreams" / config.name
        search_dir = args.workdir / "downstreams-search" / config.name
        cache_dir = downstream_cache_dir(args.workdir, config.name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        env = cache_env(cache_dir)

        clone_upstream("leanprover-community/mathlib4", upstream_dir)
        target_commit = resolve_upstream_target(upstream_dir, args.upstream_ref)
        downstream_commit = clone_downstream(config, downstream_dir)
        pinned_commit = resolve_search_base_commit(
            project_dir=downstream_dir,
            dependency_name=config.dependency_name,
            upstream_dir=upstream_dir,
            last_known_good=None,
        )
        stored_last_known_good = previous.get("last_known_good_commit")

        def verify_last_known_good(candidate_commit: str) -> bool:
            verification_output_dir = args.output_dir / "stored-last-known-good-check"
            print(
                f"[{config.name}] verifying stored last-known-good "
                f"{candidate_commit[:12]} before extending bisect window"
            )
            clone_downstream(config, search_dir, clone_source=str(downstream_dir))
            tool_run, _, _ = run_validation_attempt(
                config=config,
                from_ref=parent_commit(upstream_dir, candidate_commit),
                to_ref=candidate_commit,
                project_dir=search_dir,
                output_dir=verification_output_dir,
                tested_commits=[candidate_commit],
                env=env,
                tool_exe=args.tool_exe,
                quiet=args.quiet,
            )
            return tool_run.returncode == 0

        search_base_commit = select_search_base_commit(
            project_dir=downstream_dir,
            dependency_name=config.dependency_name,
            upstream_dir=upstream_dir,
            last_known_good=stored_last_known_good,
            verify_last_known_good=verify_last_known_good,
        )
        commit_window, truncated = build_commit_window(
            upstream_dir,
            target_commit,
            search_base_commit,
            args.max_commits,
        )
        commit_plan_path = commit_plan_artifact_path(args.output_dir)
        head_probe_details = describe_commits(upstream_dir, [target_commit])
        append_commit_plan_artifact(
            output_dir=args.output_dir,
            label="head probe commit",
            commits=head_probe_details,
        )
        print_commit_plan_summary(
            downstream=config.name,
            label="head probe commit",
            commits=head_probe_details,
            artifact_path=commit_plan_path,
        )

        warm_downstream_cache(config, project_dir=downstream_dir, output_dir=args.output_dir, env=env)
        head_probe_run, head_probe_state, head_probe_summary_text = run_validation_attempt(
            config=config,
            from_ref=parent_commit(upstream_dir, target_commit),
            to_ref=target_commit,
            project_dir=downstream_dir,
            output_dir=args.output_dir,
            tested_commits=[target_commit],
            env=env,
            tool_exe=args.tool_exe,
            quiet=args.quiet,
        )

        tool_run = head_probe_run
        state = head_probe_state
        tool_summary = head_probe_summary_text
        search_mode = "head-only"
        tested_commits = [target_commit]
        tested_commit_details = head_probe_details

        if should_run_boundary_search(head_probe_run.returncode, commit_window):
            if search_base_commit is None:
                raise RuntimeError("boundary search requested without a known-good base commit")
            bisect_commits = [search_base_commit, *commit_window]
            tested_commit_details = describe_commits(upstream_dir, bisect_commits)
            append_commit_plan_artifact(
                output_dir=args.output_dir,
                label="bisect window (oldest to newest)",
                commits=tested_commit_details,
                truncated=truncated,
                bisect_window=True,
            )
            print_commit_plan_summary(
                downstream=config.name,
                label="bisect window (oldest to newest)",
                commits=tested_commit_details,
                artifact_path=commit_plan_path,
            )
            clone_downstream(config, search_dir, clone_source=str(downstream_dir))
            tool_run, state, tool_summary = run_validation_attempt(
                config=config,
                from_ref=parent_commit(upstream_dir, bisect_commits[0]),
                to_ref=bisect_commits[-1],
                project_dir=search_dir,
                output_dir=args.output_dir,
                tested_commits=bisect_commits,
                env=env,
                tool_exe=args.tool_exe,
                bisect=True,
                quiet=args.quiet,
            )
            search_mode = "bisect"
            tested_commits = bisect_commits

        result = build_result_from_tool(
            config=config,
            downstream_commit=downstream_commit,
            upstream_ref=args.upstream_ref,
            target_commit=target_commit,
            search_mode=search_mode,
            tested_commits=tested_commits,
            tested_commit_details=tested_commit_details,
            truncated=truncated,
            tool_run=tool_run,
            state=state,
            tool_summary=tool_summary,
            head_probe_outcome=classify_exit_code(head_probe_run.returncode).value,
            head_probe_failure_stage=head_probe_state.get("stage"),
            head_probe_summary=(
                head_probe_summary_text
                or head_probe_run.stdout.strip()
                or head_probe_run.stderr.strip()
                or "hopscotch did not emit a summary"
            ),
            pinned_commit=pinned_commit,
        )
    except Exception as error:  # pragma: no cover - exercised via workflow, not unit tests.
        result = build_error_result(config, args.upstream_ref, str(error))

    write_result(args.output_dir / "result.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
