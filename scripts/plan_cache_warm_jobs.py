#!/usr/bin/env python3
"""Emit a GitHub Actions matrix of mathlib SHAs to cache-warm.

Two modes:

* **DB / inventory mode** (default): reads the regression-workflow
  ``downstream_status`` rows for the inventory entries that opt in via
  ``warm_cache: true``, collects every non-null ``last_known_good_commit``
  and ``first_known_bad_commit``, and deduplicates by SHA.

* **Manual mode** (``--manual-shas a,b,c``): bypasses inventory + DB and
  emits one matrix entry per supplied SHA. Used by ``workflow_dispatch``
  for testing or one-off backfills.

Output JSON shape::

    {
      "include": [
        {"sha": "<40-hex>", "tag": "lkg|fkb|both|manual",
         "downstreams": ["physlib", "FLT"]},
        ...
      ]
    }

Empty matrices are valid (``include: []``); the orchestrator workflow
gates downstream jobs on a separate ``has_jobs`` boolean.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.models import DownstreamConfig, load_inventory
from scripts.storage import (
    DownstreamStatusRecord,
    StorageBackend,
    add_backend_args,
    create_backend,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _parse_manual_shas(raw: str) -> list[str]:
    """Validate and split a comma-separated SHA list.

    Raises ``ValueError`` if any token isn't a 40-char lowercase hex SHA.
    """
    shas: list[str] = []
    for token in raw.split(","):
        sha = token.strip().lower()
        if not sha:
            continue
        if not _SHA_RE.match(sha):
            raise ValueError(f"invalid SHA (expected 40 lowercase hex chars): {token!r}")
        shas.append(sha)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for sha in shas:
        if sha not in seen:
            seen.add(sha)
            out.append(sha)
    return out


def build_matrix_from_db(
    inventory: dict[str, DownstreamConfig],
    statuses: dict[str, DownstreamStatusRecord],
    known_warm_shas: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the matrix include list from inventory + DB statuses.

    Considers only inventory entries with ``warm_cache=True``. Each
    SHA's ``tag`` reflects the union of roles across downstreams: a
    SHA that's LKG for one project and FKB for another is tagged
    ``both``.

    SHAs in *known_warm_shas* are dropped after dedup so the matrix only
    carries cold work. Mathlib's olean cache is content-hashed and
    immutable per SHA, so a SHA confirmed warm by a previous run never
    needs to be re-probed.
    """
    warm = known_warm_shas or set()

    # sha -> {"downstreams": ordered list, "roles": set of "lkg"/"fkb"}
    by_sha: dict[str, dict[str, Any]] = {}

    for name, config in sorted(inventory.items()):
        if not config.warm_cache:
            continue
        status = statuses.get(name)
        if status is None:
            continue

        for role, sha in (("lkg", status.last_known_good_commit),
                          ("fkb", status.first_known_bad_commit)):
            if not sha:
                continue
            entry = by_sha.setdefault(sha, {"downstreams": [], "roles": set()})
            entry["roles"].add(role)
            if name not in entry["downstreams"]:
                entry["downstreams"].append(name)

    include: list[dict[str, Any]] = []
    for sha in sorted(by_sha):
        if sha in warm:
            continue
        entry = by_sha[sha]
        roles = entry["roles"]
        if roles == {"lkg"}:
            tag = "lkg"
        elif roles == {"fkb"}:
            tag = "fkb"
        else:
            tag = "both"
        include.append({
            "sha": sha,
            "short_sha": sha[:7],
            "tag": tag,
            "downstreams": entry["downstreams"],
        })
    return include


def build_matrix_manual(shas: list[str]) -> list[dict[str, Any]]:
    """Build the matrix from an operator-supplied SHA list."""
    return [
        {"sha": sha, "short_sha": sha[:7], "tag": "manual", "downstreams": []}
        for sha in shas
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit a GitHub Actions matrix of mathlib SHAs to cache-warm."
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
        "--manual-shas",
        default="",
        help=(
            "Optional comma-separated mathlib SHAs to warm. When non-empty, "
            "the inventory + DB are ignored and only these SHAs are emitted."
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output path for the matrix JSON.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.manual_shas.strip():
        manual = _parse_manual_shas(args.manual_shas)
        include = build_matrix_manual(manual)
    else:
        inventory = load_inventory(Path(args.inventory), include_disabled=False)
        backend = create_backend(args.backend, dsn=args.dsn, state_root=args.state_root)
        statuses = backend.load_all_statuses("regression", args.upstream)
        known_warm = backend.load_known_warm_shas(args.upstream)
        include = build_matrix_from_db(inventory, statuses, known_warm)

    payload = {"include": include}
    Path(args.output).write_text(json.dumps(payload, indent=2))
    print(
        f"Cache-warming plan: {len(include)} unique SHA(s) "
        f"({'manual' if args.manual_shas.strip() else 'inventory+DB'})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
