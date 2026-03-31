#!/usr/bin/env python3
"""Run the downstream regression probe for a preselected bisect window."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.run_downstream_regression import (
    DownstreamConfig,
    build_result_from_tool,
    build_selection_error_result,
    cache_env,
    clone_downstream,
    downstream_cache_dir,
    load_selection,
    run_validation_attempt,
    write_result,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the bisect probe step."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--tool-exe", type=Path)
    return parser


def main() -> int:
    """Execute the bisect probe described by the selection payload."""

    args = build_parser().parse_args()
    selection = load_selection(args.selection)
    if not selection.needs_probe:
        raise SystemExit(f"selection does not require a probe: {args.selection}")
    if selection.downstream is None or selection.repo is None or selection.default_branch is None:
        raise SystemExit(f"selection is missing downstream metadata: {args.selection}")

    config = DownstreamConfig(
        name=selection.downstream,
        repo=selection.repo,
        default_branch=selection.default_branch,
        dependency_name=selection.dependency_name,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        if selection.probe_from_ref is None or selection.probe_to_ref is None:
            raise ValueError(
                "selection is missing probe_from_ref/probe_to_ref; "
                "re-run the selection step to regenerate selection.json"
            )
        downstream_dir = args.workdir / "downstreams" / config.name
        search_dir = args.workdir / "downstreams-search" / config.name
        cache_dir = downstream_cache_dir(args.workdir, config.name)
        cache_dir.mkdir(parents=True, exist_ok=True)
        env = cache_env(cache_dir)

        clone_source = str(downstream_dir) if downstream_dir.exists() else None
        clone_downstream(config, search_dir, clone_source=clone_source)
        tool_run, state, tool_summary = run_validation_attempt(
            config=config,
            from_ref=selection.probe_from_ref,
            to_ref=selection.probe_to_ref,
            project_dir=search_dir,
            output_dir=args.output_dir / "bisect",
            tested_commits=selection.tested_commits,
            env=env,
            tool_exe=args.tool_exe,
            bisect=True,
            quiet=args.quiet,
        )
        result = build_result_from_tool(
            config=config,
            downstream_commit=selection.downstream_commit,
            upstream_ref=selection.upstream_ref or "master",
            target_commit=selection.target_commit,
            search_mode=selection.search_mode,
            tested_commits=selection.tested_commits,
            tested_commit_details=selection.tested_commit_details,
            truncated=selection.commit_window_truncated,
            tool_run=tool_run,
            state=state,
            tool_summary=tool_summary,
            head_probe_outcome=selection.head_probe_outcome,
            head_probe_failure_stage=selection.head_probe_failure_stage,
            head_probe_summary=selection.head_probe_summary,
            pinned_commit=selection.pinned_commit,
        )
    except Exception as error:  # pragma: no cover - exercised via workflow, not unit tests.
        result = build_selection_error_result(selection, str(error))

    write_result(args.output_dir / "result.json", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
