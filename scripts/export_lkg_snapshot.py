#!/usr/bin/env python3
"""Export the latest per-downstream LKG snapshot from the database to JSON.

Reads the downstream_status table (workflow=regression) and the downstream
inventory, then writes a versioned JSON file that downstream repos can fetch
to discover the latest known-good mathlib commit.

Usage:
    python3 scripts/export_lkg_snapshot.py \\
        --backend sql \\
        --upstream leanprover-community/mathlib4 \\
        --inventory ci/inventory/downstreams.json \\
        --output /tmp/lkg-snapshot.json

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
    DownstreamStatusRecord,
    StorageBackend,
    add_backend_args,
    create_backend,
)

SCHEMA_VERSION = 1

# GitHub base URL used when constructing source run URLs from a run ID.
_GITHUB_BASE = "https://github.com"
# Fallback repo slug when GITHUB_REPOSITORY is not set.
_DEFAULT_REPO = "leanprover-community/hopscotch-reports"


def build_snapshot(
    backend: StorageBackend,
    inventory: dict[str, DownstreamConfig],
    upstream: str,
    source_run: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build and return the LKG snapshot dict.

    Only enabled downstreams (those present in *inventory*) are included.
    Downstreams with no stored status get ``null`` commit fields.

    Args:
        backend: Storage backend to query for per-downstream status.
        inventory: Enabled downstreams keyed by name (from ``load_inventory``).
        upstream: Upstream repository slug, e.g. ``"leanprover-community/mathlib4"``.
        source_run: Optional provenance dict with ``run_id`` and ``run_url``.

    Returns:
        A dict conforming to the v1 snapshot schema.
    """
    statuses: dict[str, DownstreamStatusRecord] = backend.load_all_statuses(
        "regression", upstream
    )

    downstreams: dict[str, dict[str, Any]] = {}
    for name, config in inventory.items():
        status = statuses.get(name)
        downstreams[name] = {
            "repo": config.repo,
            "dependency_name": config.dependency_name,
            "last_known_good_commit": status.last_known_good_commit if status else None,
            "first_known_bad_commit": status.first_known_bad_commit if status else None,
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": utc_now(),
        "upstream": upstream,
        "source_run": source_run,
        "downstreams": downstreams,
    }


def _fetch_source_run(dsn: str | None) -> dict[str, str] | None:
    """Try to fetch the latest regression run metadata for provenance.

    Returns ``None`` on any error so that missing run metadata never blocks
    the snapshot export.
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the latest LKG snapshot from the downstream_status table."
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

    inventory = load_inventory(Path(args.inventory))
    backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)

    # Provenance metadata — only available for the SQL backend.
    source_run: dict[str, str] | None = None
    if args.backend == "sql":
        source_run = _fetch_source_run(args.dsn)

    snapshot = build_snapshot(backend, inventory, args.upstream, source_run)

    output_path = Path(args.output)
    output_path.write_text(json.dumps(snapshot, indent=2))
    print(
        f"Exported LKG snapshot with {len(snapshot['downstreams'])} downstream(s) "
        f"to {output_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
