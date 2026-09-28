#!/usr/bin/env python3
"""Check that this repo's references to its own actions use current commit pins.

A workflow or composite action that calls one of this repo's actions by its
full repo path (``leanprover-community/downstream-reports/.github/actions/<name>``)
pins a commit SHA, so an external caller that pins this repo by SHA gets the
whole call chain at fixed versions. A pin is current when the pinned action's
files are the same at the pinned commit and at HEAD. The files of an action are
its directory plus each path that its ``action.yml`` reads through
``${{ github.action_path }}``.

A pin must be an ancestor of HEAD, so it cannot name a commit that a squash
merge discards. A pin cannot name the commit that changes the action, so a
change to a pinned action makes its pin stale until a later commit moves the
pin. ``--stale-is-warning`` reports a stale pin without failing, for pull
requests that change a pinned action.
"""

from __future__ import annotations

import argparse
import posixpath
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SELF_ACTION_PREFIX = "leanprover-community/downstream-reports/.github/actions/"

_USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*" + re.escape(SELF_ACTION_PREFIX) + r"([\w.-]+)@(\S+)"
)
_ACTION_PATH = re.compile(r"\$\{\{\s*github\.action_path\s*\}\}/([^\s\"'\\]+)")
_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Pin:
    """One ``uses:`` reference to an action of this repo."""

    file: str
    line: int
    action: str
    ref: str


def find_pins(repo: Path) -> list[Pin]:
    """Return every reference to this repo's actions in its workflows and actions."""

    files = sorted(
        [*repo.glob(".github/workflows/*.yml"), *repo.glob(".github/actions/*/action.yml")]
    )
    pins: list[Pin] = []
    for path in files:
        for number, text in enumerate(path.read_text().splitlines(), start=1):
            match = _USES.match(text)
            if match:
                pins.append(
                    Pin(path.relative_to(repo).as_posix(), number, match[1], match[2])
                )
    return pins


def action_files(repo: Path, action: str) -> list[str]:
    """Return the repo paths that the action ``action`` reads."""

    directory = f".github/actions/{action}"
    paths = {directory}
    for relative in _ACTION_PATH.findall((repo / directory / "action.yml").read_text()):
        paths.add(posixpath.normpath(posixpath.join(directory, relative)))
    return sorted(paths)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def check_pin(repo: Path, pin: Pin) -> tuple[str, str] | None:
    """Return ``(level, message)`` for a pin that is not current, else ``None``.

    ``level`` is ``"error"`` for a pin that is not a reachable SHA and
    ``"stale"`` for a pin whose action files changed after the pinned commit.
    """

    if not _SHA.fullmatch(pin.ref):
        return "error", f"{pin.action}@{pin.ref} is not pinned to a full commit SHA"
    if _git(repo, "merge-base", "--is-ancestor", pin.ref, "HEAD").returncode != 0:
        return "error", (
            f"{pin.action}@{pin.ref} is not an ancestor of HEAD; pin a commit on main"
        )
    paths = action_files(repo, pin.action)
    diff = _git(repo, "diff", "--name-only", pin.ref, "HEAD", "--", *paths)
    if diff.returncode != 0:
        return "error", f"git diff failed for {pin.action}@{pin.ref}: {diff.stderr.strip()}"
    changed = diff.stdout.split()
    if changed:
        return "stale", (
            f"{pin.action}@{pin.ref} is stale: {', '.join(changed)} changed after the "
            "pinned commit. Pin a commit on main that contains the change."
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument(
        "--stale-is-warning",
        action="store_true",
        help="report a stale pin as a warning instead of an error",
    )
    args = parser.parse_args()

    failed = False
    for pin in find_pins(args.repo):
        problem = check_pin(args.repo, pin)
        if problem is None:
            print(f"ok: {pin.file}:{pin.line} {pin.action}@{pin.ref}")
            continue
        level, message = problem
        annotation = "warning" if level == "stale" and args.stale_is_warning else "error"
        failed = failed or annotation == "error"
        print(f"::{annotation} file={pin.file},line={pin.line}::{message}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
