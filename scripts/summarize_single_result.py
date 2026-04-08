#!/usr/bin/env python3
"""Print a per-downstream validation summary from a result.json artifact.

Used by the validate job to surface a readable summary in the step log
without cluttering the workflow summary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.aggregate_results import (
    ValidationResult,
    load_culprit_log_text,
    render_report,
)
from scripts.models import Outcome, utc_now


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--result-dir", type=Path, required=True,
        help="Directory containing result.json (and any log artifacts).",
    )
    parser.add_argument("--upstream", default="leanprover-community/mathlib4")
    parser.add_argument("--upstream-ref", default="master")
    args = parser.parse_args()

    result_file = args.result_dir / "result.json"
    if not result_file.exists():
        print(f"No result.json found in {args.result_dir}", file=sys.stderr)
        return 1

    result = ValidationResult.from_json(json.loads(result_file.read_text()))
    culprit_log_text = load_culprit_log_text(args.result_dir)

    if result.outcome is Outcome.PASSED:
        episode_state = "passing"
        last_known_good = result.target_commit
        first_known_bad = None
    elif result.outcome is Outcome.FAILED:
        episode_state = "new_failure"
        last_known_good = result.last_successful_commit
        first_known_bad = result.first_failing_commit or result.target_commit
    else:
        episode_state = "error"
        last_known_good = None
        first_known_bad = None

    row: dict = {
        "downstream": result.downstream,
        "outcome": result.outcome.value,
        "episode_state": episode_state,
        "target_commit": result.target_commit,
        "last_known_good": last_known_good,
        "first_known_bad": first_known_bad,
        "failure_stage": result.failure_stage,
        "search_mode": result.search_mode,
        "commit_window_truncated": result.commit_window_truncated,
        "error": result.error,
        "head_probe_outcome": result.head_probe_outcome,
        "head_probe_failure_stage": result.head_probe_failure_stage,
        "culprit_log_text": culprit_log_text,
        "tested_commit_details": [
            {"sha": d.sha, "title": d.title} for d in result.tested_commit_details
        ],
        # No prior DB state available in the validate job.
        "previous_last_known_good": None,
        "previous_first_known_bad": None,
        "current_last_successful": result.last_successful_commit,
        "current_first_failing": result.first_failing_commit,
        "pinned_commit": result.pinned_commit,
    }

    report = render_report(
        recorded_at=utc_now(),
        upstream_ref=args.upstream_ref,
        upstream=args.upstream,
        run_id="(validate job)",
        run_url="",
        rows=[row],
    )
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
