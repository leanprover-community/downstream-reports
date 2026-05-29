#!/usr/bin/env python3
"""Validate a single downstream against a mathlib4 PR.

Two modes, selected by ``--mode`` (default ``lkg``):

* ``--mode lkg`` — check out ``--lkg-commit`` (the downstream's
  last-known-good mathlib commit, supplied by ``build_matrix.py`` from
  the published ``lkg/latest.json`` snapshot), cherry-pick the PR's
  commits onto it, build mathlib's library target as a sanity check,
  then build the downstream.  Yields a verdict independent of current
  mathlib master health.  PR-touched source files miss the upstream
  olean cache and rebuild on every run.

* ``--mode merge`` — clone mathlib4 at ``--merge-sha`` (the
  would-be-merged tree GitHub computed) and build the downstream
  against it.  Faster because ``lake exe cache get`` for the merge
  commit is mostly a cache hit; the verdict is sensitive to current
  master health.

See ``python3 scripts/pr_validation/validate.py --help`` for the full
argument list.  The interface mirrors the audited pattern from
``scripts/probe_downstream_regression_window.py`` and
``scripts/select_downstream_regression_window.py``: every input is a
named CLI flag so the call site in the workflow YAML lists what it's
passing.

Writes:

    <output-dir>/result.json
        { status, stage, message, downstream, merge_sha, downstream_sha,
          mode, lkg_commit?, requested_name?, downstream_rev?,
          fkb_commit?, pr_base_sha?, pr_head_sha?, commits_replayed?,
          replayed_tree_sha? }
        status ∈ { pass, fail, infra_failure }
    <output-dir>/build.log
        Combined log of every subprocess (mirrors the live workflow log).

Exit code is 0 in all cases except a script-level error: a build failure
is the meaningful answer, not an infra failure.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import IO, Any, NoReturn

# Same dual-mode bootstrap as `scripts/probe_downstream_regression_window.py:25`
# so this module is importable as `scripts.pr_validation.validate` *and*
# runnable directly via `python3 scripts/pr_validation/validate.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODE_LKG = "lkg"
MODE_MERGE = "merge"
DEFAULT_UPSTREAM_REPO = "leanprover-community/mathlib4"


# ---------------------------------------------------------------------------
# Config and run state
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Config:
    """Env-derived configuration for one validation run.

    Frozen so the stage functions can't accidentally mutate it; mutable
    state lives on :class:`State`.
    """

    pr_number: str
    merge_sha: str
    downstream: str
    requested_name: str
    downstream_repo: str
    default_branch: str
    dependency_name: str
    downstream_rev: str
    workdir: Path
    output_dir: Path
    tool_bin: Path
    mode: str
    lkg_commit: str
    fkb_commit: str
    upstream_repo: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Config":
        """Build a Config from a parsed argparse Namespace.

        Mode-specific guards (``--lkg-commit`` is required when
        ``--mode lkg``) are enforced here so a misconfigured workflow
        fails fast with a clear message rather than several stages
        later.  ``argparse`` handles the rest of the required-flag
        contract for us.
        """
        if args.mode == MODE_LKG and not args.lkg_commit:
            raise ValueError("--mode lkg requires --lkg-commit")
        # When the user typed the short name, --requested-name mirrors
        # --downstream (or is omitted); we always store it so
        # result.json's optional field is set only when it carries new
        # information.
        requested_name = args.requested_name or args.downstream
        # --downstream-rev defaults to --default-branch so the validate
        # flow can always rely on a non-empty rev.
        downstream_rev = args.downstream_rev or args.default_branch
        return cls(
            pr_number=args.pr_number,
            merge_sha=args.merge_sha,
            downstream=args.downstream,
            requested_name=requested_name,
            downstream_repo=args.downstream_repo,
            default_branch=args.default_branch,
            dependency_name=args.dependency_name,
            downstream_rev=downstream_rev,
            workdir=args.workdir,
            output_dir=args.output_dir,
            tool_bin=args.tool_bin,
            mode=args.mode,
            lkg_commit=args.lkg_commit,
            fkb_commit=args.fkb_commit,
            upstream_repo=args.upstream_repo,
        )

    @property
    def mathlib_dir(self) -> Path:
        return self.workdir / "mathlib4"

    @property
    def downstream_dir(self) -> Path:
        return self.workdir / "downstream"


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for ``validate.py``.

    Every input is a named flag.  ``--lkg-commit`` and ``--fkb-commit``
    are optional at the argparse level (so merge-mode dispatches don't
    have to pass empty strings); the mode-specific guard in
    ``Config.from_args`` enforces ``--lkg-commit`` for LKG runs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--merge-sha", required=True)
    parser.add_argument("--downstream", required=True)
    parser.add_argument(
        "--requested-name",
        default="",
        help=(
            "Literal token the user typed (short name or owner/repo slug)."
            " Recorded on result.json when it differs from --downstream."
        ),
    )
    parser.add_argument("--downstream-repo", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--dependency-name", required=True)
    parser.add_argument(
        "--downstream-rev",
        default="",
        help=(
            "Git refspec for the downstream's checkout."
            " Defaults to --default-branch when empty."
        ),
    )
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tool-bin", type=Path, required=True)
    parser.add_argument(
        "--mode",
        choices=[MODE_LKG, MODE_MERGE],
        default=MODE_LKG,
        help=(
            f"{MODE_LKG}: rebase the PR onto --lkg-commit and build."
            f" {MODE_MERGE}: build against --merge-sha as-is."
        ),
    )
    parser.add_argument(
        "--lkg-commit",
        default="",
        help="Mathlib SHA to rebase onto in LKG mode (required if --mode lkg).",
    )
    parser.add_argument(
        "--fkb-commit",
        default="",
        help=(
            "First-known-bad mathlib commit for this downstream"
            " (propagated to result.json for the comment renderer)."
        ),
    )
    parser.add_argument(
        "--upstream-repo",
        default=DEFAULT_UPSTREAM_REPO,
        help="Mathlib4 fork to clone (defaults to the canonical repo).",
    )
    return parser


@dataclasses.dataclass
class State:
    """Fields populated as the validation progresses; flushed into result.json on emit."""

    downstream_sha: str = ""
    pr_base: str = ""
    pr_head: str = ""
    n_commits: int | None = None
    replayed_tree_sha: str = ""


# ---------------------------------------------------------------------------
# Live-streaming log
# ---------------------------------------------------------------------------


class Log:
    """Single chokepoint that writes to stdout + appends to a log file.

    Every line that lands in ``$OUTPUT_DIR/build.log`` also appears in
    the live workflow log via stdout — no buffering, no surprises.
    Subprocesses use :meth:`run`; the script itself uses :meth:`print`
    for headers / annotations.

    The log file content matches what bash's ``exec > >(tee -a "$LOG")
    2>&1`` produced, so ``post_results.py`` / ``log_filter.py`` see the
    same noisy transcript and apply the same filters.
    """

    def __init__(self, path: Path, sink: IO[str] | None = None) -> None:
        self._path = path
        self._sink = sink  # tests inject a StringIO; production uses path
        self._fh: IO[str] | None = None

    def __enter__(self) -> "Log":
        if self._sink is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self._path.open("a", encoding="utf-8")
        else:
            self._fh = self._sink
        return self

    def __exit__(self, *exc: object) -> None:
        if self._sink is None and self._fh is not None:
            self._fh.close()
        self._fh = None

    def print(self, line: str = "") -> None:
        """Print one line; mirror it to the log file."""
        assert self._fh is not None, "Log used outside its `with` block"
        print(line, flush=True)
        self._fh.write(line + "\n")
        self._fh.flush()

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> int:
        """Run *cmd*, streaming its stdout + stderr to stdout + the log file."""
        assert self._fh is not None, "Log used outside its `with` block"
        with subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                self._fh.write(line)
                self._fh.flush()
            return proc.wait()


# ---------------------------------------------------------------------------
# GitHub Actions annotation helpers
# ---------------------------------------------------------------------------


def section(log: Log, title: str) -> None:
    log.print(f"::group::{title}")


def endsection(log: Log) -> None:
    log.print("::endgroup::")


def notice(log: Log, title: str, message: str) -> None:
    log.print(f"::notice title={title}::{message}")


def warn(log: Log, title: str, message: str) -> None:
    log.print(f"::warning title={title}::{message}")


def err_ann(log: Log, title: str, message: str) -> None:
    log.print(f"::error title={title}::{message}")


# ---------------------------------------------------------------------------
# Result.json shape
# ---------------------------------------------------------------------------


def build_result_record(
    cfg: Config,
    state: State,
    *,
    status: str,
    stage: str,
    message: str,
) -> dict[str, Any]:
    """Build the dict written to ``$OUTPUT_DIR/result.json``.

    Optional fields are only included when they carry information
    beyond their default — that keeps result.json compact for the
    common case (short-name request, default branch, no FKB) and makes
    the comment renderer's ``result.get(...)`` falsy-defaults
    meaningful.
    """
    record: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "message": message,
        "downstream": cfg.downstream,
        "merge_sha": cfg.merge_sha,
        "downstream_sha": state.downstream_sha or None,
        "mode": cfg.mode,
    }
    if cfg.mode == MODE_LKG:
        record["lkg_commit"] = cfg.lkg_commit or None
    elif cfg.lkg_commit:
        # Merge mode doesn't build against LKG, but build_matrix may attach
        # the recorded last-known-good as the baseline behind the comment's
        # "master builds with <name>" claim. Pass it through when present.
        record["lkg_commit"] = cfg.lkg_commit

    # The literal token the user typed; only stored when it differs
    # from the canonical downstream name (i.e. the slug form was used).
    if cfg.requested_name and cfg.requested_name != cfg.downstream:
        record["requested_name"] = cfg.requested_name

    # FKB lets the comment renderer state master health definitively.
    if cfg.fkb_commit:
        record["fkb_commit"] = cfg.fkb_commit

    # Record the requested rev only when it actually changes what we'd
    # otherwise default to.
    if cfg.downstream_rev and cfg.downstream_rev != cfg.default_branch:
        record["downstream_rev"] = cfg.downstream_rev

    # PR endpoints (resolved from MERGE_SHA's two parents). Both modes
    # capture them once we've fetched the merge SHA; post_results uses
    # them for the explicit `Tested:` recipe and the compare-URL link.
    if state.pr_base:
        record["pr_base_sha"] = state.pr_base
    if state.pr_head:
        record["pr_head_sha"] = state.pr_head

    # commits_replayed = |MERGE_SHA^1..MERGE_SHA^2| — the PR's own commit
    # count, applies to both modes. replayed_tree_sha is LKG-only: the
    # synthetic post-cherry-pick tree SHA, written but not rendered.
    if state.n_commits is not None:
        record["commits_replayed"] = state.n_commits
    if cfg.mode == MODE_LKG and state.replayed_tree_sha:
        record["replayed_tree_sha"] = state.replayed_tree_sha

    return record


def write_result(cfg: Config, record: dict[str, Any]) -> None:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    (cfg.output_dir / "result.json").write_text(json.dumps(record))


# ---------------------------------------------------------------------------
# Failure helpers — emit result.json, annotation, and exit
# ---------------------------------------------------------------------------


def emit_and_exit(
    cfg: Config,
    state: State,
    *,
    status: str,
    stage: str,
    message: str,
) -> NoReturn:
    """Write result.json with *status* / *stage* / *message* and exit 0.

    Exit 0 (not nonzero) because a build failure or infra failure is
    the meaningful answer for this script — the report job consumes
    result.json and renders the comment regardless.
    """
    write_result(cfg, build_result_record(cfg, state, status=status, stage=stage, message=message))
    sys.exit(0)


def fail_infra(
    cfg: Config,
    state: State,
    log: Log,
    *,
    stage: str,
    title: str,
    message: str,
    annotation: str = "error",
) -> NoReturn:
    """Close any open ``::group::``, emit an annotation, write result.json, exit.

    *annotation* picks ``::error::`` (the default — alarming) versus
    ``::warning::`` (used for ``rebase_conflict`` and
    ``mathlib_build_at_lkg`` where the underlying signal is "the PR
    cannot be tested in isolation against an older mathlib", not
    "infra broke").
    """
    endsection(log)
    {"error": err_ann, "warning": warn}[annotation](log, title, message)
    emit_and_exit(cfg, state, status="infra_failure", stage=stage, message=message)


# ---------------------------------------------------------------------------
# PR endpoint derivation
# ---------------------------------------------------------------------------


def derive_pr_endpoints(
    log: Log, mathlib_dir: Path, merge_sha: str
) -> tuple[str, str]:
    """Resolve (PR_BASE, PR_HEAD) from a merge commit's parents.

    GitHub's ``refs/pull/N/merge`` is ``merge(base, head)``: parent ^1
    is the base ref tip, parent ^2 is the PR head.  A fast-forward
    merge has a single parent — we return ``(parent1, "")`` and the
    caller treats PR_HEAD as absent (no commits to cherry-pick).
    """
    pr_base = _rev_parse(log, mathlib_dir, f"{merge_sha}^1")
    if not pr_base:
        return "", ""
    pr_head = _rev_parse(log, mathlib_dir, f"{merge_sha}^2")
    return pr_base, pr_head


def _rev_parse(log: Log, repo: Path, rev: str) -> str:
    """Return ``git rev-parse`` output, or `""` on non-zero exit.

    Uses a captured-output subprocess (not the streaming ``Log.run``)
    because we want the resolved SHA back, not just stream it.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _rev_list_count(repo: Path, range_: str) -> int | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", range_],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Stage implementations
# ---------------------------------------------------------------------------


def header(cfg: Config, log: Log) -> None:
    """Print the validation-parameter banner."""
    section(log, "Validation parameters")
    log.print(f"  PR:         #{cfg.pr_number}")
    log.print(f"  Mode:       {cfg.mode}")
    log.print(f"  Upstream:   {cfg.upstream_repo}")
    log.print(f"  Merge SHA:  {cfg.merge_sha}")
    if cfg.mode == MODE_LKG:
        log.print(f"  LKG commit: {cfg.lkg_commit}")
    log.print(f"  Downstream: {cfg.downstream}")
    log.print(f"    repo:     {cfg.downstream_repo}")
    log.print(f"    rev:      {cfg.downstream_rev} (default_branch: {cfg.default_branch})")
    log.print(f"    dep:      {cfg.dependency_name}")
    endsection(log)


def clone_mathlib(cfg: Config, state: State, log: Log) -> None:
    """Stage 1: clone the upstream mathlib4 repo (no checkout yet)."""
    if cfg.mathlib_dir.exists():
        # Idempotent reruns from inside the same workdir.
        subprocess.run(["rm", "-rf", str(cfg.mathlib_dir)], check=True)
    section(log, f"Clone {cfg.upstream_repo}")
    rc = log.run(
        [
            "git", "clone", "--no-checkout",
            f"https://github.com/{cfg.upstream_repo}.git",
            str(cfg.mathlib_dir),
        ]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="clone",
            title="Clone failed",
            message=f"could not clone {cfg.upstream_repo}",
        )
    endsection(log)


def resolve_mathlib_tree(cfg: Config, state: State, log: Log) -> None:
    """Stage 2: fetch the merge SHA (+ LKG), check out the working tree.

    Branches on mode:

    * ``merge`` — fetch ``$MERGE_SHA``, resolve its parents into
      ``PR_BASE`` / ``PR_HEAD`` for result.json, check out the merge
      SHA detached.
    * ``lkg`` — fetch both ``$MERGE_SHA`` and ``$LKG_COMMIT``, resolve
      PR endpoints, check out ``$LKG_COMMIT`` detached, then
      cherry-pick ``PR_BASE..PR_HEAD`` onto it.
    """
    if cfg.mode == MODE_MERGE:
        _resolve_merge_mode(cfg, state, log)
    else:
        _resolve_lkg_mode(cfg, state, log)


def _resolve_merge_mode(cfg: Config, state: State, log: Log) -> None:
    section(log, f"Fetch merge SHA {cfg.merge_sha}")
    rc = log.run(["git", "-C", str(cfg.mathlib_dir), "fetch", "origin", cfg.merge_sha])
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="fetch",
            title="Fetch failed",
            message=(
                f"merge SHA {cfg.merge_sha} not fetchable; "
                "PR may have conflicts"
            ),
        )
    endsection(log)

    # Best-effort: capture PR_BASE / PR_HEAD for result.json. A
    # fast-forward merge has only one parent, in which case PR_HEAD
    # stays empty.
    state.pr_base, state.pr_head = derive_pr_endpoints(
        log, cfg.mathlib_dir, cfg.merge_sha
    )
    if state.pr_base and state.pr_head and state.pr_base != state.pr_head:
        state.n_commits = _rev_list_count(
            cfg.mathlib_dir, f"{state.pr_base}..{state.pr_head}"
        )

    section(log, "Check out merge SHA")
    rc = log.run(
        ["git", "-C", str(cfg.mathlib_dir), "checkout", "--detach", cfg.merge_sha]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="checkout",
            title="Checkout failed",
            message=f"could not check out {cfg.merge_sha}",
        )
    endsection(log)


def _resolve_lkg_mode(cfg: Config, state: State, log: Log) -> None:
    section(log, "Fetch merge SHA + LKG commit")
    rc = log.run(
        [
            "git", "-C", str(cfg.mathlib_dir), "fetch", "origin",
            cfg.merge_sha, cfg.lkg_commit,
        ]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="fetch",
            title="Fetch failed",
            message=(
                f"could not fetch MERGE_SHA {cfg.merge_sha} / "
                f"LKG_COMMIT {cfg.lkg_commit}"
            ),
        )
    endsection(log)

    section(log, "Resolve PR endpoints from merge commit")
    state.pr_base, state.pr_head = derive_pr_endpoints(
        log, cfg.mathlib_dir, cfg.merge_sha
    )
    if not state.pr_base:
        fail_infra(
            cfg, state, log,
            stage="rev_parse",
            title="rev-parse failed",
            message=f"could not resolve {cfg.merge_sha}^1 (PR base)",
        )
    log.print(f"  PR base: {state.pr_base}")
    pr_head_label = state.pr_head or "(fast-forward; same as merge SHA)"
    log.print(f"  PR head: {pr_head_label}")
    endsection(log)

    section(log, f"Check out LKG {cfg.lkg_commit}")
    rc = log.run(
        [
            "git", "-C", str(cfg.mathlib_dir),
            "checkout", "--detach", cfg.lkg_commit,
        ]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="checkout",
            title="Checkout failed",
            message=f"could not check out LKG {cfg.lkg_commit}",
        )
    endsection(log)

    if state.pr_head and state.pr_base != state.pr_head:
        state.n_commits = _rev_list_count(
            cfg.mathlib_dir, f"{state.pr_base}..{state.pr_head}"
        )
        n = state.n_commits if state.n_commits is not None else "?"
        section(log, f"Cherry-pick {n} PR commit(s) onto LKG")
        notice(
            log,
            "Cherry-pick",
            f"Replaying {n} PR commit(s) "
            f"({state.pr_base}..{state.pr_head}) onto LKG "
            f"{cfg.lkg_commit[:7]}",
        )
        rc = log.run(
            [
                "git", "-C", str(cfg.mathlib_dir),
                "-c", "user.email=ci@downstream-reports.invalid",
                "-c", "user.name=ci",
                "cherry-pick", f"{state.pr_base}..{state.pr_head}",
            ]
        )
        if rc != 0:
            # Best-effort cleanup; don't fail the abort itself.
            subprocess.run(
                ["git", "-C", str(cfg.mathlib_dir), "cherry-pick", "--abort"],
                check=False,
            )
            fail_infra(
                cfg, state, log,
                stage="rebase_conflict",
                title="Rebase conflict",
                annotation="warning",
                message=(
                    f"PR commits do not apply on top of LKG {cfg.lkg_commit}; "
                    "this PR likely depends on post-LKG mathlib changes"
                ),
            )
        state.replayed_tree_sha = _rev_parse(log, cfg.mathlib_dir, "HEAD")
        log.print(f"  resulting tree: {state.replayed_tree_sha}")
        endsection(log)
    else:
        # Fast-forward merge: PR head == merge SHA, nothing to replay.
        state.n_commits = 0
        state.replayed_tree_sha = cfg.lkg_commit
        notice(
            log,
            "Cherry-pick",
            "merge SHA is fast-forward; no commits to cherry-pick",
        )


def warm_cache(cfg: Config, log: Log) -> None:
    """Stage 3: ``lake exe cache get`` (best-effort)."""
    section(log, "lake exe cache get")
    rc = log.run(["lake", "exe", "cache", "get"], cwd=cfg.mathlib_dir)
    if rc != 0:
        log.print(
            "  (best-effort cache get failed; continuing — "
            "uncached files will be rebuilt)"
        )
    endsection(log)


def sanity_build_mathlib(cfg: Config, state: State, log: Log) -> None:
    """Stage 4 (LKG only): ``lake build Mathlib`` to disambiguate.

    A downstream's ``lake build`` only pulls in the mathlib targets it
    imports, so failures inside mathlib code can read as a downstream
    incompatibility.  Building ``Mathlib`` (the top-level library
    target) first distinguishes "PR depends on post-LKG mathlib
    changes" from a genuine downstream break.
    """
    if cfg.mode != MODE_LKG:
        return
    section(log, "Sanity-build mathlib library (lake build Mathlib)")
    rc = log.run(["lake", "build", "Mathlib"], cwd=cfg.mathlib_dir)
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="mathlib_build_at_lkg",
            title="Mathlib build failed at LKG",
            annotation="warning",
            message=(
                f"mathlib failed to build with this PR rebased onto LKG "
                f"{cfg.lkg_commit}; the PR likely depends on post-LKG "
                "mathlib changes"
            ),
        )
    endsection(log)


def clone_downstream(cfg: Config, state: State, log: Log) -> None:
    """Stage 5: clone the downstream and check out the requested rev."""
    if cfg.downstream_dir.exists():
        subprocess.run(["rm", "-rf", str(cfg.downstream_dir)], check=True)
    section(log, f"Clone downstream {cfg.downstream_repo} @ {cfg.downstream_rev}")
    rc = log.run(
        [
            "git", "clone", "--no-checkout",
            f"https://github.com/{cfg.downstream_repo}.git",
            str(cfg.downstream_dir),
        ]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="clone_downstream",
            title="Clone failed",
            message=f"could not clone {cfg.downstream_repo}",
        )

    rc = log.run(
        [
            "git", "-C", str(cfg.downstream_dir), "fetch", "origin",
            cfg.downstream_rev,
        ]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="fetch_downstream",
            title="Fetch failed",
            message=(
                f"could not fetch {cfg.downstream_rev} from "
                f"{cfg.downstream_repo}"
            ),
        )

    rc = log.run(
        ["git", "-C", str(cfg.downstream_dir), "checkout", "--detach", "FETCH_HEAD"]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="checkout_downstream",
            title="Checkout failed",
            message=(
                f"could not check out {cfg.downstream_rev} from "
                f"{cfg.downstream_repo}"
            ),
        )

    state.downstream_sha = _rev_parse(log, cfg.downstream_dir, "HEAD")
    log.print(f"  downstream HEAD: {state.downstream_sha}")
    endsection(log)


def lakedit_set(cfg: Config, state: State, log: Log) -> None:
    """Stage 6: rewrite the downstream's manifest to point at our mathlib4 clone."""
    section(log, f"lakedit set {cfg.dependency_name} --path $ML")
    rc = log.run(
        [
            str(cfg.tool_bin / "lakedit"), "set", cfg.dependency_name,
            "--path", str(cfg.mathlib_dir),
            "--project-dir", str(cfg.downstream_dir),
        ]
    )
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="lakedit",
            title="lakedit failed",
            message="lakedit failed; see log",
        )
    endsection(log)


def lake_update_and_build(cfg: Config, state: State, log: Log) -> None:
    """Stage 7: ``lake update`` + ``lake build`` against the downstream."""
    section(log, f"lake update {cfg.dependency_name}")
    rc = log.run(["lake", "update", cfg.dependency_name], cwd=cfg.downstream_dir)
    if rc != 0:
        fail_infra(
            cfg, state, log,
            stage="lake_update",
            title="lake update failed",
            message="lake update failed; see log",
        )
    endsection(log)

    section(log, "lake build (downstream)")
    rc = log.run(["lake", "build"], cwd=cfg.downstream_dir)
    endsection(log)
    if rc == 0:
        if cfg.mode == MODE_LKG:
            notice(
                log, "PASS",
                f"{cfg.downstream} builds against this PR rebased onto LKG "
                f"{cfg.lkg_commit[:7]}",
            )
            emit_and_exit(
                cfg, state,
                status="pass",
                stage="build",
                message="downstream builds against PR rebased onto LKG",
            )
        else:
            notice(
                log, "PASS",
                f"{cfg.downstream} builds against this PR (merge ref)",
            )
            emit_and_exit(
                cfg, state,
                status="pass",
                stage="build",
                message="downstream builds against PR merge ref",
            )
    else:
        warn(
            log, "FAIL",
            f"{cfg.downstream} failed to build against this PR (mode={cfg.mode})",
        )
        emit_and_exit(
            cfg, state,
            status="fail",
            stage="build",
            message="lake build failed; see log",
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(cfg: Config) -> None:
    """Run the full pipeline. Always exits 0 via :func:`emit_and_exit`."""
    cfg.workdir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.output_dir / "build.log"
    log_path.write_text("")  # truncate any prior content

    state = State()
    with Log(log_path) as log:
        header(cfg, log)
        clone_mathlib(cfg, state, log)
        resolve_mathlib_tree(cfg, state, log)
        warm_cache(cfg, log)
        sanity_build_mathlib(cfg, state, log)
        clone_downstream(cfg, state, log)
        lakedit_set(cfg, state, log)
        lake_update_and_build(cfg, state, log)
    # Unreachable: lake_update_and_build always exits via emit_and_exit.


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.from_args(args)
    run(cfg)
    return 0  # unreachable; satisfies the return-type checker


if __name__ == "__main__":
    sys.exit(main())
