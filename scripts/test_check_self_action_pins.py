"""Tests for check_self_action_pins.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.check_self_action_pins import (
    SELF_ACTION_PREFIX,
    Pin,
    action_files,
    check_pin,
    find_pins,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


INNER = ".github/actions/inner/action.yml"
INNER_TEXT = 'runs:\n  steps:\n    - run: bash "${{ github.action_path }}/../../scripts/x.sh"\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _commit(tmp_path, {INNER: INNER_TEXT, ".github/scripts/x.sh": "v1\n"})
    return tmp_path


class TestFindPins:
    def test_finds_self_references_and_action_path_files(self, repo: Path) -> None:
        """Scenario: a workflow and an action call this repo's actions; other uses: lines are ignored."""
        _commit(
            repo,
            {
                ".github/workflows/w.yml": (
                    "steps:\n"
                    "  - uses: actions/checkout@v6\n"
                    f"  - uses: {SELF_ACTION_PREFIX}inner@main\n"
                ),
                ".github/actions/outer/action.yml": (
                    f"runs:\n  steps:\n    - uses: {SELF_ACTION_PREFIX}inner@{'a' * 40} # main\n"
                ),
            },
        )

        assert find_pins(repo) == [
            Pin(".github/actions/outer/action.yml", 3, "inner", "a" * 40),
            Pin(".github/workflows/w.yml", 3, "inner", "main"),
        ]
        assert action_files(repo, "inner") == [
            ".github/actions/inner",
            ".github/scripts/x.sh",
        ]


class TestCheckPin:
    @pytest.mark.parametrize(
        "change, expected",
        [
            ({}, None),
            ({"README.md": "unrelated\n"}, None),
            ({INNER: INNER_TEXT + "# edit\n"}, "stale"),
            ({".github/scripts/x.sh": "v2\n"}, "stale"),
        ],
    )
    def test_pin_is_stale_when_action_files_change(
        self, repo: Path, change: dict[str, str], expected: str | None
    ) -> None:
        """Scenario: files change after the pinned commit; only the action's own files make the pin stale."""
        pinned = _git(repo, "rev-parse", "HEAD")
        if change:
            _commit(repo, change)

        problem = check_pin(repo, Pin("f.yml", 1, "inner", pinned))

        assert (problem[0] if problem else None) == expected

    @pytest.mark.parametrize("ref", ["main", "abc1234"])
    def test_ref_that_is_not_a_full_sha_is_an_error(self, repo: Path, ref: str) -> None:
        """Scenario: the pin is a branch name or a short SHA."""
        problem = check_pin(repo, Pin("f.yml", 1, "inner", ref))

        assert problem is not None and problem[0] == "error"

    def test_pin_off_the_head_history_is_an_error(self, repo: Path) -> None:
        """Scenario: the pin names a commit on a branch that HEAD does not contain."""
        _git(repo, "switch", "-q", "-c", "side")
        side = _commit(repo, {"README.md": "side\n"})
        _git(repo, "switch", "-q", "main")

        problem = check_pin(repo, Pin("f.yml", 1, "inner", side))

        assert problem is not None and problem[0] == "error"
