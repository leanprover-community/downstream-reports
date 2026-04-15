#!/usr/bin/env python3
"""Aggregate per-downstream validation results into persisted regression state.

State machine overview:

    passing --failed--> failing
    failing --failed--> failing
    failing --passed--> passing
    passing --passed--> passing

Results tagged as `error` do not open or close a regression episode. They are
recorded on the downstream status entry but leave the episode cursor unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.models import Outcome, utc_now
from scripts.storage import (
    DownstreamStatusRecord,
    RunResultRecord,
    ValidateJobRecord,
    add_backend_args,
    create_backend,
    result_to_row,
)


GITHUB_API = "https://api.github.com"


def _gh_get(url: str, headers: dict[str, str]) -> dict | None:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        print(f"  warning: GitHub API request failed ({url}): {exc}")
        return None


def fetch_commit_distances(
    pairs: set[tuple[str, str]],
    repo: str,
    token: str | None,
) -> dict[tuple[str, str], int | None]:
    """Return {(base_sha, head_sha): ahead_by} for every pair.

    `ahead_by` is the number of commits reachable from head but not base.
    Returns None for a pair when the API call fails.
    """
    headers: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "downstream-reports/aggregate_results",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    cache: dict[tuple[str, str], int | None] = {}
    for base, head in sorted(pairs):
        if base == head:
            cache[(base, head)] = 0
            continue
        url = f"{GITHUB_API}/repos/{repo}/compare/{base}...{head}"
        data = _gh_get(url, headers)
        cache[(base, head)] = data.get("ahead_by") if data is not None else None
    return cache


class EpisodeState(str, Enum):
    """High-level transition labels used in the markdown report."""

    PASSING = "passing"
    NEW_FAILURE = "new_failure"
    FAILING = "failing"
    RECOVERED = "recovered"
    ERROR = "error"


@dataclass(frozen=True)
class ValidationResult:
    """Subset of the per-downstream result schema needed for aggregation."""

    @dataclass(frozen=True)
    class CommitDetail:
        """One Mathlib commit plus the title shown in reports."""

        sha: str
        title: str

    downstream: str
    repo: str
    downstream_commit: str | None
    target_commit: str | None
    outcome: Outcome
    failure_stage: str | None
    first_failing_commit: str | None
    commit_window_truncated: bool
    error: str | None
    last_successful_commit: str | None = None
    search_mode: str = "head-only"
    tested_commit_details: list["ValidationResult.CommitDetail"] = field(default_factory=list)
    head_probe_outcome: str | None = None
    head_probe_failure_stage: str | None = None
    pinned_commit: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ValidationResult":
        """Decode one JSON result file."""

        return cls(
            downstream=payload["downstream"],
            repo=payload["repo"],
            downstream_commit=payload.get("downstream_commit"),
            target_commit=payload.get("target_commit"),
            outcome=Outcome(payload["outcome"]),
            failure_stage=payload.get("failure_stage"),
            first_failing_commit=payload.get("first_failing_commit"),
            last_successful_commit=payload.get("last_successful_commit"),
            commit_window_truncated=payload.get("commit_window_truncated", False),
            error=payload.get("error"),
            search_mode=payload.get("search_mode", "head-only"),
            tested_commit_details=[
                cls.CommitDetail(**detail) for detail in payload.get("tested_commit_details", [])
            ],
            head_probe_outcome=payload.get("head_probe_outcome"),
            head_probe_failure_stage=payload.get("head_probe_failure_stage"),
            pinned_commit=payload.get("pinned_commit"),
        )


@dataclass(frozen=True)
class LoadedResult:
    """One loaded result plus any auxiliary artifact content used in reports."""

    result: ValidationResult
    culprit_log_text: str | None = None


def short_commit(commit: str | None) -> str:
    """Abbreviate a commit hash for markdown tables."""

    if commit is None:
        return "-"
    return commit[:12]


def upstream_commit_url(commit: str | None, upstream: str) -> str | None:
    """Return the GitHub commit URL for one SHA in the given upstream repo."""

    if commit is None:
        return None
    return f"https://github.com/{upstream}/commit/{commit}"


def render_commit_link(commit: str | None, upstream: str = "leanprover-community/mathlib4") -> str:
    """Render one commit hash as a markdown link when possible."""

    if commit is None:
        return "`-`"
    url = upstream_commit_url(commit, upstream)
    return f"[`{short_commit(commit)}`]({url})"


def render_commit_detail(detail: dict[str, Any]) -> str:
    """Render one commit and its title for markdown output."""

    return f"{render_commit_link(detail.get('sha'))} {detail.get('title', '')}".rstrip()


def find_commit_detail(details: list[dict[str, Any]], sha: str | None) -> dict[str, Any] | None:
    """Return the matching commit detail when it appears in the recorded window."""

    if sha is None:
        return None
    for detail in details:
        if detail.get("sha") == sha:
            return detail
    return None


def render_named_commit(details: list[dict[str, Any]], sha: str | None, upstream: str = "leanprover-community/mathlib4") -> str:
    """Render one named commit using its recorded title when available."""

    detail = find_commit_detail(details, sha)
    if detail is not None:
        return render_commit_detail(detail)
    return render_commit_link(sha, upstream)


def truncate_log_text(text: str, *, max_lines: int = 200, max_chars: int = 40000) -> str:
    """Limit embedded log output so reports stay readable."""

    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    limited = "\n".join(lines)
    if len(limited) > max_chars:
        limited = limited[:max_chars].rstrip()
        truncated = True
    if truncated:
        limited += "\n[log truncated]"
    return limited


def exclude_culprit_log_line(line: str) -> str:
    """
    Exclude line from the 'culprit log' that we want to show
    We want to drop:
        - Successful target messages
        - The log trace
    """
    l = line.strip()
    filtered_prefixes = ["✔", "trace: .>"]
    for pfx in filtered_prefixes:
        if l.startswith(pfx):
            return True
    return False


def filter_culprit_log_text(text: str) -> str:
    """Drop successful-target lines from the embedded failing-commit log."""
    return "\n".join(line for line in text.splitlines() if not exclude_culprit_log_line(line))


def load_culprit_log_text(artifact_root: Path) -> str | None:
    """Load the culprit log from the well-known hopscotch logs/culprit/ directory.

    Checks the culprit-probe output first (skip-known-bad-bisect path), then the
    bisect probe output, then the head probe output.  The tool writes
    ``build.log`` or ``update.log`` under ``.lake/hopscotch/logs/culprit/``;
    ``copy_tool_artifacts`` mirrors that tree under the per-step ``tool-state``
    subdirectory, hence the path prefixes below.
    """
    candidates = [
        artifact_root / "culprit-probe" / "tool-state" / "logs" / "culprit",
        artifact_root / "bisect" / "tool-state" / "logs" / "culprit",
        artifact_root / "head-probe" / "tool-state" / "logs" / "culprit",
    ]
    for directory in candidates:
        if directory.is_dir():
            files = sorted(directory.glob("*.log"))
            if files:
                return truncate_log_text(filter_culprit_log_text(files[0].read_text(errors="replace")))
    return None


def first_bad_position(details: list[dict[str, Any]], first_bad_sha: str | None) -> tuple[int, int] | None:
    """Return the 1-based position of the first bad commit inside the bisect window."""

    if first_bad_sha is None or not details:
        return None
    for index, detail in enumerate(details, start=1):
        if detail.get("sha") == first_bad_sha:
            return index, len(details)
    return None


def load_results(results_dir: Path) -> list[LoadedResult]:
    """Load all `result.json` files under the downloaded artifact tree."""

    results: list[LoadedResult] = []
    for path in sorted(results_dir.rglob("result.json")):
        result = ValidationResult.from_json(json.loads(path.read_text()))
        results.append(
            LoadedResult(
                result=result,
                culprit_log_text=load_culprit_log_text(path.parent),
            )
        )
    if not results:
        print(f"[aggregate] Warning: no result.json files found under {results_dir}")
    return results


def apply_result(
    current: DownstreamStatusRecord | None,
    result: ValidationResult,
) -> tuple[DownstreamStatusRecord, EpisodeState]:
    """Apply one validation result to the persisted regression state.

    Returns a *new* ``DownstreamStatusRecord`` — the input is never mutated.
    """

    was_failing = current is not None and current.first_known_bad_commit is not None

    pin = result.pinned_commit
    ds_commit = result.downstream_commit

    if result.outcome is Outcome.ERROR:
        prior = current or DownstreamStatusRecord()
        return DownstreamStatusRecord(
            last_known_good_commit=prior.last_known_good_commit,
            first_known_bad_commit=prior.first_known_bad_commit,
            pinned_commit=pin or prior.pinned_commit,
            downstream_commit=ds_commit or prior.downstream_commit,
        ), EpisodeState.ERROR

    if result.outcome is Outcome.PASSED:
        episode_state = EpisodeState.RECOVERED if was_failing else EpisodeState.PASSING
        return DownstreamStatusRecord(
            last_known_good_commit=result.target_commit,
            first_known_bad_commit=None,
            pinned_commit=pin,
            downstream_commit=ds_commit,
        ), episode_state

    # FAILED
    last_good = result.last_successful_commit or (
        current.last_known_good_commit if current else None
    )
    if was_failing and current is not None:
        return DownstreamStatusRecord(
            last_known_good_commit=last_good,
            first_known_bad_commit=current.first_known_bad_commit,
            pinned_commit=pin,
            downstream_commit=ds_commit,
        ), EpisodeState.FAILING

    first_bad = result.first_failing_commit or result.target_commit
    return DownstreamStatusRecord(
        last_known_good_commit=last_good,
        first_known_bad_commit=first_bad,
        pinned_commit=pin,
        downstream_commit=ds_commit,
    ), EpisodeState.NEW_FAILURE


def render_report(
    *,
    recorded_at: str,
    upstream_ref: str,
    upstream: str = "leanprover-community/mathlib4",
    run_id: str,
    run_url: str,
    rows: list[dict[str, Any]],
    job_urls: dict[str, str] | None = None,
    skipped_rows: list[dict[str, Any]] | None = None,
) -> str:
    """Render the human-readable markdown report used in GitHub summaries."""

    _outcome_label = {"passed": "compatible", "failed": "incompatible"}
    _episode_label = {
        "passing":     "compatible",
        "recovering":  "recovered",
        "recovered":   "recovered",
        "new_failure": "new incompatibility",
        "failing":     "incompatible",
        "error":       "error",
    }

    lines = [
        "# Downstream Regression Report",
        "",
        f"- Generated at: `{recorded_at}`",
        f"- Upstream ref: `{upstream_ref}`",
        "",
        "| Downstream | Outcome | Status | Target | Last known good | First known bad |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        notes = []
        if row["failure_stage"] is not None:
            notes.append(f"stage={row['failure_stage']}")
        if row["search_mode"] != "head-only":
            notes.append(f"mode={row['search_mode']}")
        if row["commit_window_truncated"]:
            notes.append("window-truncated")
        if row["error"] is not None:
            notes.append(row["error"])
        lines.append(
            "| {downstream} | {outcome} | {episode_state} | {target} | {last_known_good} | {first_known_bad}".format(
                downstream=row["downstream"],
                outcome=_outcome_label.get(row["outcome"], row["outcome"]),
                episode_state=_episode_label.get(row["episode_state"], row["episode_state"]),
                target=render_commit_link(row["target_commit"], upstream),
                last_known_good=render_commit_link(row["last_known_good"], upstream),
                first_known_bad=render_commit_link(row["first_known_bad"], upstream),
            )
        )
    lines.append("")

    for row in rows:
        details = row["tested_commit_details"]
        downstream_name = row["downstream"]
        job_url = (job_urls or {}).get(downstream_name)
        name_html = (
            f'<a href="{job_url}"><strong>{downstream_name}</strong></a>'
            if job_url
            else f"<strong>{downstream_name}</strong>"
        )
        lines.extend(
            [
                "<details>",
                f"<summary>{name_html} &mdash; <code>{_outcome_label.get(row['outcome'], row['outcome'])}</code> &mdash; <code>{_episode_label.get(row['episode_state'], row['episode_state'])}</code></summary>",
                "",
            ]
        )
        lines.append("- Previous state before this run:")
        lines.append(
            "  - last known good: "
            + render_named_commit(details, row.get("previous_last_known_good"), upstream)
        )
        lines.append(
            "  - first known bad: "
            + render_named_commit(details, row.get("previous_first_known_bad"), upstream)
        )
        if row["head_probe_outcome"] is not None:
            head_probe = row["head_probe_outcome"]
            if row["head_probe_failure_stage"] is not None:
                head_probe = f"{head_probe} (stage={row['head_probe_failure_stage']})"
            lines.append(f"- Head probe: `{head_probe}`")
        if row["tested_commit_details"]:
            if row["search_mode"] == "bisect":
                lines.append("- Bisect window boundary:")
                lines.append(f"  - oldest: {render_commit_detail(details[0])}")
                lines.append(f"  - newest: {render_commit_detail(details[-1])}")
                lines.append("")
                lines.append("**Results**")
                lines.append("")
                lines.append("- Current run frontier:")
                lines.append(
                    "  - last known good: "
                    + render_named_commit(details, row.get("current_last_successful"), upstream)
                )
                lines.append(
                    "  - first incompatible commit found this run: "
                    + render_named_commit(details, row.get("current_first_failing"), upstream)
                )
                position = first_bad_position(details, row.get("current_first_failing"))
                if position is not None:
                    index, total = position
                    lines.append(
                        f"- First incompatible commit position: `{index}/{total}` in the bisect window "
                        f"(advanced {index - 1} of {total - 1} commits from the lower bound)"
                    )
            else:
                lines.append("- Commit list:")
                for detail in row["tested_commit_details"]:
                    lines.append(f"  - {render_commit_detail(detail)}")
        lines.append("- State after this run:")
        lines.append(
            "  - last known good: "
            + render_named_commit(details, row["last_known_good"], upstream)
        )
        lines.append(
            "  - first known bad: "
            + render_named_commit(details, row["first_known_bad"], upstream)
        )
        if row.get("culprit_log_text"):
            lines.extend(["", "First incompatible commit logs:", "```text", row["culprit_log_text"], "```"])
        lines.extend(["", "</details>", ""])

    if skipped_rows:
        _outcome_label_skip = {"passed": "compatible", "failed": "incompatible"}
        lines.extend([
            "## Previously Tested (Skipped This Run)",
            "",
            "These downstreams had no new commits on their bumping branch since the last test.",
            "",
            "| Downstream | Previous Outcome | Previous Status | First known bad | Previous Run |",
            "| --- | --- | --- | --- | --- |",
        ])
        for srow in skipped_rows:
            prev_url = srow.get("previous_job_url") or srow.get("previous_run_url")
            prev_link = f"[previous run]({prev_url})" if prev_url else "-"
            lines.append(
                "| {downstream} | {outcome} | {status} | {fkb} | {prev} |".format(
                    downstream=srow.get("downstream", "?"),
                    outcome=_outcome_label_skip.get(srow.get("outcome", ""), srow.get("outcome") or "-"),
                    status=srow.get("episode_state") or "-",
                    fkb=render_commit_link(srow.get("first_known_bad"), upstream),
                    prev=prev_link,
                )
            )
        lines.append("")
        for srow in skipped_rows:
            downstream_name = srow.get("downstream", "?")
            prev_job_url = srow.get("previous_job_url")
            prev_run_url = srow.get("previous_run_url")
            name_html = (
                f'<a href="{prev_job_url}"><strong>{downstream_name}</strong></a>'
                if prev_job_url
                else f"<strong>{downstream_name}</strong>"
            )
            outcome_label = _outcome_label_skip.get(srow.get("outcome", ""), srow.get("outcome") or "?")
            lines.extend([
                "<details>",
                f"<summary>{name_html} &mdash; <code>{outcome_label}</code> (skipped, same commit as previous run)</summary>",
                "",
                f"- Downstream commit: `{srow.get('downstream_commit', '?')[:12]}`",
                f"- Previous outcome: `{srow.get('outcome') or '-'}`",
                f"- Previous status: `{srow.get('episode_state') or '-'}`",
                "- Target Mathlib commit: " + render_commit_link(srow.get("target_commit"), upstream),
                "- First known bad: " + render_commit_link(srow.get("first_known_bad"), upstream),
            ])
            if prev_job_url:
                lines.append(f"- Previous validate job: [link]({prev_job_url})")
            elif prev_run_url:
                lines.append(f"- Previous run: [link]({prev_run_url})")
            lines.extend(["", "</details>", ""])

    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the aggregation step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--workflow", required=True, choices=["regression", "ondemand"])
    parser.add_argument("--upstream", default="leanprover-community/mathlib4")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--upstream-ref", required=True)
    parser.add_argument("--job-urls", type=Path)
    add_backend_args(parser)
    parser.add_argument(
        "--report-output", type=Path, default=None,
        help="Path to write the markdown report; useful when --backend=sql.",
    )
    parser.add_argument(
        "--alert-output", type=Path, default=None,
        help="Path to write the alert payload JSON (consumed by the alert job).",
    )
    parser.add_argument(
        "--github-token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token for commit distance lookups (default: $GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--skipped", type=Path, default=None,
        help="Path to skipped.json from the plan job (on-demand skipped downstreams).",
    )
    return parser


def main() -> int:
    """Aggregate result artifacts into persisted regression state and reports."""

    args = build_parser().parse_args()
    recorded_at = utc_now()

    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)

    prior_statuses = backend.load_all_statuses(args.workflow, args.upstream)
    if args.results_dir.exists():
        loaded_results = load_results(args.results_dir)
    else:
        loaded_results = []

    updated_statuses: dict[str, DownstreamStatusRecord] = {}
    result_records: list[RunResultRecord] = []
    # tested_commit_details is not stored; keep it alongside for the markdown report.
    tested_details_per_record: list[list[dict[str, Any]]] = []

    for loaded in sorted(loaded_results, key=lambda item: item.result.downstream):
        result = loaded.result
        prior = prior_statuses.get(result.downstream)
        updated, episode_state = apply_result(prior, result)
        updated_statuses[result.downstream] = updated
        record = RunResultRecord(
            upstream=args.upstream,
            downstream=result.downstream,
            repo=result.repo,
            downstream_commit=result.downstream_commit,
            outcome=result.outcome.value,
            episode_state=episode_state.value,
            target_commit=result.target_commit,
            previous_last_known_good=prior.last_known_good_commit if prior else None,
            previous_first_known_bad=prior.first_known_bad_commit if prior else None,
            last_known_good=updated.last_known_good_commit,
            first_known_bad=updated.first_known_bad_commit,
            current_last_successful=result.last_successful_commit,
            current_first_failing=result.first_failing_commit,
            failure_stage=result.failure_stage,
            search_mode=result.search_mode,
            commit_window_truncated=result.commit_window_truncated,
            error=result.error,
            head_probe_outcome=result.head_probe_outcome,
            head_probe_failure_stage=result.head_probe_failure_stage,
            culprit_log_text=loaded.culprit_log_text,
            pinned_commit=result.pinned_commit,
        )
        result_records.append(record)
        tested_details_per_record.append(
            [{"sha": d.sha, "title": d.title} for d in result.tested_commit_details]
        )

    # Compute commit distances (pinned→target = age, pinned→lkg = bump) via the
    # GitHub compare API, then annotate each record.  Batched after the loop so
    # each unique pair is fetched exactly once.
    compare_pairs: set[tuple[str, str]] = set()
    for r in result_records:
        if r.pinned_commit and r.target_commit and r.pinned_commit != r.target_commit:
            compare_pairs.add((r.pinned_commit, r.target_commit))
        if r.pinned_commit and r.last_known_good and r.pinned_commit != r.last_known_good:
            compare_pairs.add((r.pinned_commit, r.last_known_good))
    if compare_pairs:
        print(f"Fetching commit distances for {len(compare_pairs)} unique pair(s)…")
        distances = fetch_commit_distances(compare_pairs, args.upstream, args.github_token)
        for r in result_records:
            pin = r.pinned_commit
            r.age_commits = distances.get((pin, r.target_commit)) if pin and r.target_commit else None
            r.bump_commits = distances.get((pin, r.last_known_good)) if pin and r.last_known_good else None

    render_rows: list[dict[str, Any]] = [
        {**result_to_row(r), "tested_commit_details": details}
        for r, details in zip(result_records, tested_details_per_record)
    ]

    job_urls: dict[str, str] = {}
    validate_jobs: list[ValidateJobRecord] = []
    if args.job_urls and args.job_urls.exists():
        raw_job_data: dict[str, Any] = json.loads(args.job_urls.read_text())
        for downstream, entry in raw_job_data.items():
            if isinstance(entry, str):
                # Legacy format: just a URL string.
                job_urls[downstream] = entry
            else:
                job_urls[downstream] = entry["url"]
                validate_jobs.append(ValidateJobRecord(
                    downstream=downstream,
                    job_id=entry["job_id"],
                    job_url=entry["url"],
                    started_at=entry.get("started_at"),
                    finished_at=entry.get("finished_at"),
                    conclusion=entry.get("conclusion"),
                ))
    skipped_rows: list[dict[str, Any]] = []
    if args.skipped and args.skipped.exists():
        skipped_rows = json.loads(args.skipped.read_text())

    markdown = render_report(
        recorded_at=recorded_at,
        upstream_ref=args.upstream_ref,
        upstream=args.upstream,
        run_id=args.run_id,
        run_url=args.run_url,
        rows=render_rows,
        job_urls=job_urls,
        skipped_rows=skipped_rows or None,
    )

    if result_records:
        backend.save_run(
            run_id=args.run_id,
            workflow=args.workflow,
            upstream=args.upstream,
            upstream_ref=args.upstream_ref,
            run_url=args.run_url,
            created_at=recorded_at,
            results=result_records,
            updated_statuses=updated_statuses,
            report_markdown=markdown,
            validate_jobs=validate_jobs or None,
        )
    else:
        print("[aggregate] No result records to persist — skipping save_run.")

    if args.report_output is not None:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_text(markdown)

    if args.alert_output is not None:
        args.alert_output.parent.mkdir(parents=True, exist_ok=True)
        alert_payload: dict[str, Any] = {
            "run_id": args.run_id,
            "run_url": args.run_url,
            "upstream_ref": args.upstream_ref,
            "results": [asdict(r) for r in result_records],
        }
        if skipped_rows:
            alert_payload["skipped"] = skipped_rows
        args.alert_output.write_text(json.dumps(alert_payload, sort_keys=True))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
