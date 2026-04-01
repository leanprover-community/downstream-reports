"""Lake artifact caching helpers for downstream validation."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from scripts.git_ops import run
from scripts.models import DownstreamConfig


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
