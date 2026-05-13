#!/usr/bin/env python3
"""Summarise a single downstream's validation result for the GitHub Actions UI.

Reads ``<output-dir>/result.json`` and writes:
  - a short header + status to stdout (visible in the job log)
  - the same to ``$GITHUB_STEP_SUMMARY`` (visible on the run page)
  - the filtered tail of build.log when status != pass

Always exits 0: a ``fail`` status is the meaningful answer, not a workflow
error. Only a script-level error (missing result.json) raises.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Same dual-mode bootstrap as `scripts/probe_downstream_regression_window.py:25`
# so this module is importable as `scripts.pr_validation.summarize` *and*
# runnable directly via `python3 scripts/pr_validation/summarize.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.pr_validation.log_filter import read_log_tail

LOG_MAX_CHARS = 500_000  # GitHub step-summary limit is 1 MB per step

ICONS = {
    "pass": "✅",
    "fail": "❌",
    "infra_failure": "⚠️",
}

# Friendly headlines for non-generic infra-failure stages. Anything not in
# this map falls through to the generic "infra_failure (<stage>)" form.
_STAGE_HEADLINES = {
    "rebase_conflict": "PR conflicts with LKG (cannot validate)",
    "mathlib_build_at_lkg": "mathlib build failed at LKG (cannot validate)",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downstream", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    downstream = args.downstream
    output_dir = args.output_dir

    result_path = output_dir / "result.json"
    log_path = output_dir / "build.log"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not result_path.exists():
        print(
            f"::error::no result.json at {result_path} —"
            " validate step crashed before emitting one"
        )
        return 0

    with result_path.open() as handle:
        result = json.load(handle)

    status = result.get("status", "infra_failure")
    stage = result.get("stage", "unknown")
    message = result.get("message", "")
    mode = result.get("mode") or "merge"
    icon = ICONS.get(status, "❓")

    headline = _STAGE_HEADLINES.get(stage, status)
    mode_suffix = " [@lkg]" if mode == "lkg" else ""
    print(f"{icon} {downstream}{mode_suffix}: {headline} ({stage})")
    print(f"    {message}")

    summary_lines = [
        f"## {icon} `{downstream}{mode_suffix}` — {headline}",
        "",
        f"**Stage:** `{stage}`",
        "",
        f"**Message:** {message}",
        "",
    ]

    if status != "pass":
        tail = read_log_tail(log_path, LOG_MAX_CHARS)
        if tail:
            print()
            print("----- build.log (filtered) -----")
            print(tail)
            print("----------------------------------------------------")

    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write("\n".join(summary_lines))
            handle.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
