#!/usr/bin/env python3
"""Export the latest per-downstream regression-run snapshot from the database.

Reads the ``run_result`` table (joined with ``run`` and ``validate_job``) and
the downstream inventory, then writes a versioned JSON file that downstream
repos consume to link back to validation runs (and, in the future, to pull
per-downstream logs or the ``result-<name>`` artifact via the GitHub API).

The runs snapshot is published alongside ``lkg/latest.json`` at
``runs/latest.json``.  ``lkg/latest.json`` stays focused on the LKG/FKB
commits (a stable public API consumed by the bump actions); this runs
snapshot is additive and evolves independently.

Usage:
    python3 scripts/export_runs_snapshot.py \\
        --backend sql \\
        --upstream leanprover-community/mathlib4 \\
        --inventory ci/inventory/downstreams.json \\
        --output /tmp/runs-snapshot.json

Requires POSTGRES_DSN in the environment when --backend=sql.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.models import DownstreamConfig, load_inventory, utc_now
from scripts.storage import (
    LatestRunRecord,
    add_backend_args,
    create_backend,
)

SCHEMA_VERSION = 1

# GitHub base URL used when constructing source run URLs from a run ID.
_GITHUB_BASE = "https://github.com"
# Fallback repo slug when GITHUB_REPOSITORY is not set.
_DEFAULT_REPO = "leanprover-community/downstream-reports"


def _entry_from_run(config: DownstreamConfig, run: LatestRunRecord | None) -> dict[str, Any]:
    """Render one downstream entry for the runs snapshot.

    When *run* is ``None`` (no history for this downstream yet), most fields
    are ``None`` so consumers can still iterate the full inventory.
    """

    base: dict[str, Any] = {
        "repo": config.repo,
        "dependency_name": config.dependency_name,
        "run_id": None,
        "run_url": None,
        "job_id": None,
        "job_url": None,
        "result_artifact_name": f"result-{config.name}",
        "reported_at": None,
        "target_commit": None,
        "downstream_commit": None,
        "outcome": None,
        "episode_state": None,
        "first_known_bad_commit": None,
        "last_known_good_commit": None,
    }
    if run is None:
        return base
    base.update(
        {
            "run_id": run.run_id,
            "run_url": run.run_url,
            "job_id": run.job_id,
            "job_url": run.job_url,
            "reported_at": run.reported_at or None,
            "target_commit": run.target_commit,
            "downstream_commit": run.downstream_commit,
            "outcome": run.outcome,
            "episode_state": run.episode_state,
            "first_known_bad_commit": run.first_known_bad,
            "last_known_good_commit": run.last_known_good,
        }
    )
    return base


def build_runs_snapshot(
    latest_runs: dict[str, LatestRunRecord],
    inventory: dict[str, DownstreamConfig],
    upstream: str,
    source_run: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build and return the runs snapshot dict.

    All downstreams present in *inventory* are included.  Downstreams with no
    stored run get a record with ``null`` run/job/commit fields.

    Args:
        latest_runs: Mapping of downstream name → latest ``LatestRunRecord``,
            usually produced by ``load_latest_run_per_downstream``.  Entries
            not in *inventory* are silently ignored.
        inventory: Downstreams keyed by name (from ``load_inventory``).
        upstream: Upstream repository slug.
        source_run: Optional provenance dict with ``run_id`` and ``run_url``.

    Returns:
        A dict conforming to the v1 runs-snapshot schema.
    """

    downstreams: dict[str, dict[str, Any]] = {}
    for name, config in inventory.items():
        downstreams[name] = _entry_from_run(config, latest_runs.get(name))

    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": utc_now(),
        "upstream": upstream,
        "source_run": source_run,
        "downstreams": downstreams,
    }


def _fetch_source_run(dsn: str | None) -> dict[str, str] | None:
    """Return provenance metadata for the latest regression run, or None.

    Swallows any error (missing DSN, DB unreachable, sqlalchemy not installed)
    and returns ``None`` so a transient failure never blocks publication.
    """

    dsn = dsn or os.environ.get("POSTGRES_DSN")
    if not dsn:
        return None
    try:
        from sqlalchemy import create_engine  # type: ignore[import]

        from scripts.storage import latest_regression_run_id

        engine = create_engine(dsn)
        run_id = latest_regression_run_id(engine)
        if not run_id:
            return None
        repo = os.environ.get("GITHUB_REPOSITORY", _DEFAULT_REPO)
        run_url = f"{_GITHUB_BASE}/{repo}/actions/runs/{run_id}"
        return {"run_id": str(run_id), "run_url": run_url}
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not fetch run metadata: {exc}", file=sys.stderr)
        return None


def _load_latest_runs(dsn: str | None, upstream: str) -> dict[str, LatestRunRecord]:
    """Fetch latest-run-per-downstream from SQL; return empty dict on any error.

    The snapshot only carries data for the SQL backend (filesystem /dry-run
    backends do not persist per-run URL / job metadata), so any other
    configuration yields an empty result — downstreams then appear with
    ``null`` run fields, matching the LKG snapshot convention.
    """

    dsn = dsn or os.environ.get("POSTGRES_DSN")
    if not dsn:
        return {}
    try:
        from sqlalchemy import create_engine  # type: ignore[import]

        from scripts.storage import load_latest_run_per_downstream

        engine = create_engine(dsn)
        return load_latest_run_per_downstream(engine, "regression", upstream)
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not fetch latest runs: {exc}", file=sys.stderr)
        return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the latest per-downstream regression-run snapshot."
    )
    add_backend_args(parser)
    parser.add_argument(
        "--upstream",
        default="leanprover-community/mathlib4",
        help="Upstream repository slug (default: leanprover-community/mathlib4).",
    )
    parser.add_argument(
        "--inventory",
        default="ci/inventory/downstreams.json",
        help="Path to the downstreams.json inventory file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output path for the JSON snapshot.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    inventory = load_inventory(Path(args.inventory), include_disabled=True)
    # Backend is constructed for CLI symmetry with the LKG export, but the
    # runs snapshot itself is sourced from SQL directly (run metadata lives
    # in the relational schema).
    create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)

    source_run: dict[str, str] | None = None
    latest_runs: dict[str, LatestRunRecord] = {}
    if args.backend == "sql":
        source_run = _fetch_source_run(args.dsn)
        latest_runs = _load_latest_runs(args.dsn, args.upstream)

    snapshot = build_runs_snapshot(latest_runs, inventory, args.upstream, source_run)

    output_path = Path(args.output)
    output_path.write_text(json.dumps(snapshot, indent=2))
    print(
        f"Exported runs snapshot with {len(snapshot['downstreams'])} downstream(s) "
        f"to {output_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
