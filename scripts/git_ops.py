"""Git operations and commit-resolution helpers for downstream regression."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.models import CommitDetail, DownstreamConfig


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
    """Return the subject line for one upstream commit."""

    try:
        return git(repo_dir, "log", "-1", "--format=%s", commit)
    except subprocess.CalledProcessError:
        return "(title unavailable)"


def describe_commits(repo_dir: Path, commits: list[str]) -> list[CommitDetail]:
    """Attach subject lines to a list of upstream commits."""

    return [CommitDetail(sha=commit, title=commit_title(repo_dir, commit)) for commit in commits]


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


def parent_commit(repo_dir: Path, commit: str) -> str:
    """Return the immediate parent SHA of `commit` in the local repository.

    Used to derive the exclusive `--from` lower bound for `hopscotch`:
    passing `parent_commit(c)` as `--from` causes the tool to fetch exactly
    the single commit `c` (or the range from `c` through `to_ref` if `c` is
    the first element of a multi-commit list).
    """
    return git(repo_dir, "rev-parse", f"{commit}^")


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


RELEASE_TAG_GLOB = "v[0-9]*"


def latest_reachable_tag(
    repo_dir: Path, commit: str, pattern: str = RELEASE_TAG_GLOB
) -> str | None:
    """Return the latest matching tag reachable from `commit`, or None."""
    output = git(
        repo_dir,
        "tag", "--merged", commit,
        "--list", pattern,
        "--sort=-v:refname",
    )
    return output.splitlines()[0] if output else None


def resolve_tag(repo_dir: Path, tag: str) -> str:
    """Return the commit SHA for `tag`."""
    return git(repo_dir, "rev-list", "-n", "1", tag)


# Final semver release tags only (e.g. v4.32.0); excludes prereleases such as
# v4.32.0-rc1 and non-semver tags like master-2026-04-15.
FINAL_RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def next_release_tag_after(
    repo_dir: Path, commit: str, *, finals_only: bool = True
) -> str | None:
    """Return the earliest release tag strictly after `commit`, or None.

    "After" means the tag is a descendant of `commit` (so bumping to it moves
    the pin forward); a tag pointing at `commit` itself is skipped. With
    `finals_only` (the default) prerelease tags such as `v4.32.0-rc1` are
    excluded, so the result is the next *final* release. Returns None when no
    such tag exists (the commit is already at or past the newest tag).
    """
    output = git(
        repo_dir,
        "tag", "--contains", commit,
        "--list", RELEASE_TAG_GLOB,
        "--sort=v:refname",
    )
    for tag in (line for line in output.splitlines() if line):
        if finals_only and not FINAL_RELEASE_TAG_RE.fullmatch(tag):
            continue
        if resolve_tag(repo_dir, tag) != commit:
            return tag
    return None


def should_run_boundary_search(head_probe_exit_code: int, commit_window: list[str]) -> bool:
    """Return whether a failing head probe should be followed by a range search."""

    return head_probe_exit_code == 1 and len(commit_window) > 1


# ---------------------------------------------------------------------------
# Lakefile / manifest helpers
# ---------------------------------------------------------------------------


def _manifest_lookup_name(dependency_name: str) -> str:
    """Return the bare name `lake-manifest.json` records for a (possibly scoped) dep.

    The inventory's ``dependency_name`` can be a scoped Reservoir name like
    ``"leanprover-community/mathlib"`` (needed so hopscotch's lakefile parser
    matches ``require "leanprover-community" / mathlib`` etc.), but Lake's
    manifest only stores the bare package name (``"mathlib"``).  Strip the
    scope before doing manifest lookups.
    """
    _, _, bare = dependency_name.rpartition("/")
    return bare or dependency_name


def pinned_from_manifest_payload(payload: Any, dependency_name: str) -> str | None:
    """Return the resolved SHA for `dependency_name` from a parsed manifest payload.

    Shared core of `pinned_commit_from_manifest` (which reads from disk) and the
    manifest watcher (which fetches the file over HTTP without ever writing it
    out).  Returns None when the payload is unparseable, missing the dependency,
    or carrying a non-git entry.
    """
    if not isinstance(payload, dict):
        return None
    lookup_name = _manifest_lookup_name(dependency_name)
    for pkg in payload.get("packages", []):
        if pkg.get("name") == lookup_name and pkg.get("type") == "git":
            rev = pkg.get("rev")
            return rev if isinstance(rev, str) and rev else None
    return None


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
    return pinned_from_manifest_payload(payload, dependency_name)

def git_url_from_manifest(project_dir: Path, dependency_name: str) -> str | None:
    """Return the git URL for `dependency_name` from `lake-manifest.json`.

    Lake records the remote URL alongside the resolved SHA, so this works for
    projects using either `lakefile.toml` or `lakefile.lean`.  Returns None if
    the manifest is absent or the dependency is not listed.

    TODO: Remove this (and the --git-url workaround in invoke_tool) once
    hopscotch can infer the dependency URL automatically from the lakefile or
    lake-manifest.
    """
    manifest_path = project_dir / "lake-manifest.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text())
    except Exception:
        return None
    lookup_name = _manifest_lookup_name(dependency_name)
    for pkg in payload.get("packages", []):
        if pkg.get("name") == lookup_name and pkg.get("type") == "git":
            url = pkg.get("url")
            return url if isinstance(url, str) and url else None
    return None


# Files whose content pins the downstream's dependency context: the lock file
# (exact dependency revisions) and the Lean toolchain (the compiler itself).
# A change to either voids the monotonicity assumption behind boundary
# revalidation — the regression boundary can move when the dependency set or
# toolchain moves, even with downstream source untouched.
DEPENDENCY_FILES = ("lake-manifest.json", "lean-toolchain")


def file_blob_id(repo_dir: Path, commit: str, path: str) -> str | None:
    """Return the git blob id of `path` at `commit`, or None if unavailable.

    Resolving `<commit>:<path>` needs the commit and its root tree locally but
    never reads the blob content, so it works in shallow and partial clones as
    long as the commit itself has been fetched.
    """
    try:
        return git(repo_dir, "rev-parse", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


def fetch_commit(repo_dir: Path, commit: str) -> bool:
    """Shallow-fetch one commit by SHA from origin; return whether it succeeded.

    Fetching an arbitrary (non-ref-tip) SHA requires the server to enable
    `uploadpack.allowAnySHA1InWant` or similar — GitHub does.  A False return
    typically means the commit was garbage-collected after a force push, or
    the remote rejects SHA fetches.
    """
    try:
        run(["git", "fetch", "--quiet", "--depth", "1", "origin", commit], cwd=repo_dir)
        return True
    except subprocess.CalledProcessError:
        return False


def dependency_files_changed_between(
    repo_dir: Path,
    previous_commit: str,
    current_commit: str,
    files: tuple[str, ...] = DEPENDENCY_FILES,
) -> bool | None:
    """Compare the dependency-file blobs between two downstream commits.

    Returns False when every file in `files` is byte-identical at both
    commits, True when any differs, and None when the comparison cannot be
    made (the previous commit is unfetchable, or any file is missing at
    either commit).  Callers treating None as "changed" stay conservative.

    `repo_dir` is the downstream clone, which is shallow (depth 1) and so does
    not contain `previous_commit` — it is fetched on demand.
    """
    if previous_commit == current_commit:
        return False
    if file_blob_id(repo_dir, previous_commit, files[0]) is None:
        if not fetch_commit(repo_dir, previous_commit):
            return None
    changed = False
    for path in files:
        previous_blob = file_blob_id(repo_dir, previous_commit, path)
        current_blob = file_blob_id(repo_dir, current_commit, path)
        if previous_blob is None or current_blob is None:
            return None
        changed = changed or previous_blob != current_blob
    return changed


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
    2. `last_known_good` — stored episode state, used when no pin is available.
    """
    manifest_sha = pinned_commit_from_manifest(project_dir, dependency_name)
    if manifest_sha is not None:
        return manifest_sha

    return last_known_good


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
