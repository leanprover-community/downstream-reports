"""Tests for the dispatched Batteries-to-Mathlib build."""

import json
import subprocess

import pytest

from scripts import batteries_pr_validation as validation


def _inputs(**overrides):
    inputs = {
        "request_id": "123-1", "pr_number": "42", "batteries_repo": "contributor/batteries",
        "batteries_sha": "a" * 40, "mathlib_sha": "b" * 40,
        "adaptation_fork": "mathlib4-adaptations", "adaptation_pr": "",
    }
    return {**inputs, **overrides}


class TestInputs:
    @pytest.mark.parametrize("field,value", [
        ("request_id", "latest"), ("pr_number", "-1"), ("batteries_repo", "repo;cmd"),
        ("mathlib_sha", "master"), ("adaptation_fork", "mathlib4"),
        ("adaptation_fork", "mathlib4-nightly-testing"), ("adaptation_pr", "draft"),
    ])
    def test_invalid_input(self, field, value):
        """Scenario: mutable revisions and unsafe or incorrect inputs are rejected."""
        env = {key.upper(): val for key, val in _inputs(**{field: value}).items()}
        with pytest.raises(ValueError):
            validation.inputs_from_env(env)

    def test_exact_dependency(self, tmp_path):
        """Scenario: an existing fork dependency is replaced with the tested head SHA."""
        lakefile = tmp_path / "lakefile.lean"
        lakefile.write_text('require "leanprover-community" / "batteries" from git "https://github.com/old/repo" @ "old"\n')
        validation.dependency_update(lakefile, _inputs())
        assert "contributor/batteries" in lakefile.read_text()
        assert "a" * 40 in lakefile.read_text()
        assert "old/repo" not in lakefile.read_text()

    def test_secret_scrub(self, monkeypatch):
        """Scenario: Lake subprocesses do not receive write tokens or service secrets."""
        for key in ["GH_TOKEN", "GITHUB_TOKEN", "APP_PRIVATE_KEY", "POSTGRES_DSN", "ZULIP_API_KEY"]:
            monkeypatch.setenv(key, "secret")
            assert key not in validation.build_env()


def _git(*args, cwd):
    return subprocess.check_output(["git", *args], cwd=cwd, stderr=subprocess.DEVNULL, text=True).strip()


@pytest.fixture
def repository(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-b", "master", cwd=upstream)
    _git("config", "user.name", "Fixture", cwd=upstream)
    _git("config", "user.email", "fixture@example.invalid", cwd=upstream)
    (upstream / "lakefile.lean").write_text('require "leanprover-community" / "batteries" @ git "main"\n')
    (upstream / "lake-manifest.json").write_text("{}\n")
    (upstream / "lean-toolchain").write_text("leanprover/lean4:v4.35.0-rc3\n")
    _git("add", ".", cwd=upstream)
    _git("commit", "-m", "base", cwd=upstream)
    fork = tmp_path / "fork.git"
    _git("init", "--bare", str(fork), cwd=tmp_path)
    return upstream, fork


def _mock_lake(monkeypatch, upstream, fork, *, fail_build=False, fail_update=False):
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "lake":
            if args[1] == "--keep-toolchain":
                if fail_update:
                    raise subprocess.CalledProcessError(1, args)
                manifest = kwargs["cwd"] / "lake-manifest.json"
                manifest.write_text(json.dumps({"rev": "a" * 40}))
            elif args[1] == "build" and fail_build:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        args = list(args)
        if args[:2] == ["git", "clone"]:
            args[2] = str(upstream)
        elif args[:2] == ["git", "fetch"] and args[2].startswith("https://github.com/"):
            args[2] = str(fork)
        return original(args, **kwargs)

    monkeypatch.setattr(validation.subprocess, "run", run)
    return calls


class TestValidation:
    def test_pass_publishes_no_candidate(self, tmp_path, repository, monkeypatch):
        """Scenario: a passing initial build returns a result but no publication bundle."""
        upstream, fork = repository
        _mock_lake(monkeypatch, upstream, fork)
        inputs = _inputs(mathlib_sha=_git("rev-parse", "HEAD", cwd=upstream))
        result = validation.validate(inputs, tmp_path / "work", tmp_path / "out")
        assert result["status"] == "pass"
        assert not (tmp_path / "out/mathlib-adaptation.bundle").exists()
        assert (tmp_path / "work/mathlib4/lean-toolchain").read_text() == "leanprover/lean4:v4.35.0-rc3\n"

    def test_setup_failure_is_infrastructure(self, tmp_path, repository, monkeypatch):
        """Scenario: a dependency update failure cannot open an adaptation PR."""
        upstream, fork = repository
        _mock_lake(monkeypatch, upstream, fork, fail_update=True)
        inputs = _inputs(mathlib_sha=_git("rev-parse", "HEAD", cwd=upstream))
        result = validation.validate(inputs, tmp_path / "work", tmp_path / "out")
        assert result["status"] == "infra_failure"
        assert result["stage"] == "prepare"
        assert not (tmp_path / "out/mathlib-adaptation.bundle").exists()

    def test_failed_build_transfers_branch_and_preserves_adaptations(self, tmp_path, repository, monkeypatch):
        """Scenario: a failed build produces a valid bundle; a refresh preserves human fixes."""
        upstream, fork = repository
        calls = _mock_lake(monkeypatch, upstream, fork, fail_build=True)
        inputs = _inputs(mathlib_sha=_git("rev-parse", "HEAD", cwd=upstream))
        first = validation.validate(inputs, tmp_path / "work", tmp_path / "out")
        assert first["status"] == "fail"
        branch = "adaptations/batteries-42"
        _git("fetch", str(upstream), inputs["mathlib_sha"], cwd=fork)
        bundle = str(tmp_path / "out/mathlib-adaptation.bundle")
        _git("bundle", "verify", bundle, cwd=fork)
        _git("fetch", bundle, f"refs/heads/{branch}:refs/heads/{branch}", cwd=fork)
        candidate = tmp_path / "work/mathlib4"
        (candidate / "HumanFix.lean").write_text("-- human adaptation\n")
        _git("add", ".", cwd=candidate)
        _git("commit", "-m", "human fix", cwd=candidate)
        _git("push", str(fork), branch, cwd=candidate)
        (upstream / "MasterChange.lean").write_text("-- master change\n")
        _git("add", ".", cwd=upstream)
        _git("commit", "-m", "master change", cwd=upstream)
        calls.clear()
        inputs.update(adaptation_pr="456", mathlib_sha=_git("rev-parse", "HEAD", cwd=upstream))
        second = validation.validate(inputs, tmp_path / "work", tmp_path / "out")
        assert second["status"] == "prepared"
        assert (candidate / "HumanFix.lean").exists()
        assert (candidate / "MasterChange.lean").exists()
        assert not any(args[:2] == ("lake", "build") for args in calls)
        assert json.loads((tmp_path / "out/result.json").read_text())["request_id"] == "123-1"
