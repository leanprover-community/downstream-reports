#!/usr/bin/env python3
"""Summarise a single downstream's validation result for the GitHub Actions UI.

Reads ``$OUTPUT_DIR/result.json`` and writes:
  - a short header + status to stdout (visible in the job log)
  - the same to ``$GITHUB_STEP_SUMMARY`` (visible on the run page)
  - the filtered tail of build.log when status != pass

Always exits 0: a ``fail`` status is the meaningful answer, not a workflow
error. Only a script-level error (missing result.json) raises.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from log_filter import read_log_tail

LOG_TAIL_LINES = 50

ICONS = {
    "pass": "✅",
    "fail": "❌",
    "infra_failure": "⚠️",
}


def main() -> int:
    downstream = os.environ["DOWNSTREAM"]
    output_dir = Path(os.environ["OUTPUT_DIR"])

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
    icon = ICONS.get(status, "❓")

    print(f"{icon} {downstream}: {status} ({stage})")
    print(f"    {message}")

    summary_lines = [
        f"## {icon} `{downstream}` — {status}",
        "",
        f"**Stage:** `{stage}`",
        "",
        f"**Message:** {message}",
        "",
    ]

    if status != "pass":
        tail = read_log_tail(log_path, LOG_TAIL_LINES)
        if tail:
            print()
            print(f"----- last {LOG_TAIL_LINES} lines of build.log (filtered) -----")
            print(tail)
            print("----------------------------------------------------")

            summary_lines.extend(
                [
                    f"<details><summary>last {LOG_TAIL_LINES} lines of"
                    " <code>build.log</code> (filtered)</summary>",
                    "",
                    "```",
                    tail,
                    "```",
                    "",
                    "</details>",
                    "",
                ]
            )

    if summary_path:
        with open(summary_path, "a") as handle:
            handle.write("\n".join(summary_lines))
            handle.write("\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
