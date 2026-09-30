"""Build Mathlib against a Batteries PR without publication credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

FIELDS = (
    "request_id", "pr_number", "batteries_repo", "batteries_sha",
    "mathlib_sha", "adaptation_fork", "adaptation_pr",
)


def inputs_from_env(env: dict[str, str]) -> dict[str, str]:
    inputs = {field: env.get(field.upper(), "") for field in FIELDS}
    patterns = {
        "request_id": r"[0-9]+-[0-9]+",
        "pr_number": r"[1-9][0-9]*",
        "batteries_repo": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        "batteries_sha": r"[a-f0-9]{40}",
        "mathlib_sha": r"[a-f0-9]{40}",
        "adaptation_fork": r"[A-Za-z0-9_.-]+",
        "adaptation_pr": r"(?:[1-9][0-9]*)?",
    }
    for field, pattern in patterns.items():
        if not re.fullmatch(pattern, inputs[field]):
            raise ValueError(f"Invalid {field}")
    if inputs["adaptation_fork"] in {"mathlib4", "mathlib4-nightly-testing"}:
        raise ValueError("Use a dedicated adaptation fork")
    return inputs


def dependency_update(path: Path, inputs: dict[str, str]) -> None:
    replacement = (
        'require "leanprover-community" / "batteries" from git '
        f'"https://github.com/{inputs["batteries_repo"]}" @ "{inputs["batteries_sha"]}"'
    )
    text, count = re.subn(
        r'^require "leanprover-community" / "batteries"[^\n]*$',
        lambda _: replacement, path.read_text(), flags=re.MULTILINE,
    )
    if count != 1:
        raise ValueError("Expected exactly one Batteries requirement")
    path.write_text(text)


def build_env() -> dict[str, str]:
    # The workflow supplies no privilege-bearing secrets. Also remove token
    # variables before Lake executes dependency configuration or build code.
    return {
        key: value for key, value in os.environ.items()
        if not key.endswith(("_TOKEN", "_PRIVATE_KEY"))
        and key not in {"POSTGRES_DSN", "POSTGRES_DSN_RO", "ZULIP_API_KEY", "ZULIP_EMAIL"}
    }


def validate(inputs: dict[str, str], workdir: Path, output_dir: Path) -> dict[str, str]:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # A reused self-hosted runner must start from a fresh candidate tree.
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    candidate = workdir / "mathlib4"
    branch = f'adaptations/batteries-{inputs["pr_number"]}'
    result = {**inputs, "status": "infra_failure", "stage": "prepare"}
    environment = build_env()

    with (output_dir / "build.log").open("w") as log:
        def run(*args: str, cwd: Path = candidate) -> None:
            print("+", *args, file=log, flush=True)
            subprocess.run(args, cwd=cwd, env=environment, stdout=log,
                           stderr=subprocess.STDOUT, check=True)

        try:
            run("git", "clone", "https://github.com/leanprover-community/mathlib4.git",
                str(candidate), cwd=workdir)
            run("git", "config", "user.name", "mathlib-nightly-testing[bot]")
            run("git", "config", "user.email", "mathlib-nightly-testing[bot]@users.noreply.github.com")
            if inputs["adaptation_pr"]:
                run("git", "fetch", f'https://github.com/leanprover-community/{inputs["adaptation_fork"]}.git',
                    f"refs/heads/{branch}:refs/remotes/adaptations/current")
                run("git", "switch", "-c", branch, "refs/remotes/adaptations/current")
                run("git", "merge", inputs["mathlib_sha"], "--no-edit")
            else:
                run("git", "switch", "-c", branch, inputs["mathlib_sha"])
            toolchain = (candidate / "lean-toolchain").read_text()
            dependency_update(candidate / "lakefile.lean", inputs)
            run("lake", "--keep-toolchain", "update", "batteries")
            if (candidate / "lean-toolchain").read_text() != toolchain:
                raise ValueError("The dependency update changes Mathlib's toolchain")
            run("git", "add", "lakefile.lean", "lake-manifest.json")
            run("git", "commit", "--allow-empty", "-m",
                f'chore: test Batteries PR #{inputs["pr_number"]} at {inputs["batteries_sha"]}')
            if inputs["adaptation_pr"]:
                result["status"] = "prepared"
            else:
                run("lake", "exe", "cache", "get", "--repo=leanprover-community/mathlib4")
                result["stage"] = "build"
                try:
                    run("lake", "build", "Mathlib", "Archive", "Counterexamples")
                    result["status"] = "pass"
                except subprocess.CalledProcessError:
                    result["status"] = "fail"
            if result["status"] in {"fail", "prepared"}:
                result["stage"] = "bundle"
                run("git", "bundle", "create", str(output_dir / "mathlib-adaptation.bundle"),
                    f"refs/heads/{branch}", f'^{inputs["mathlib_sha"]}')
            result["stage"] = "complete"
        except (subprocess.CalledProcessError, OSError, ValueError) as error:
            result["status"] = "infra_failure"
            result["message"] = str(error)
    (output_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = validate(inputs_from_env(dict(os.environ)), args.workdir.resolve(), args.output_dir.resolve())
    print(json.dumps(result))


if __name__ == "__main__":
    main()
