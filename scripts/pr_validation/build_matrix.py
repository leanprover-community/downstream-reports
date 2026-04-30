#!/usr/bin/env python3
"""Build the validation job matrix from the inventory and a list of names.

Reads the downstream inventory and intersects it with the requested names,
emitting a GitHub Actions matrix include list with the fields the validate
job needs.

This script is invoked by the plan job in
``.github/workflows/mathlib-pr-validation.yml``. The dispatching mathlib4
workflow has already validated the names against the inventory; we re-validate
here as defence in depth because the dispatched payload is untrusted input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inventory",
        required=True,
        type=Path,
        help="Path to ci/inventory/downstreams.json.",
    )
    parser.add_argument(
        "--names",
        required=True,
        help="Comma-separated downstream names.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write the matrix JSON.",
    )
    args = parser.parse_args()

    with args.inventory.open() as handle:
        inventory = json.load(handle)

    by_name = {entry["name"]: entry for entry in inventory["downstreams"]}

    requested = [n.strip() for n in args.names.split(",") if n.strip()]
    if not requested:
        print("error: --names is empty", file=sys.stderr)
        return 1

    unknown = [n for n in requested if n not in by_name]
    if unknown:
        print(
            f"error: unknown downstream(s): {', '.join(unknown)}",
            file=sys.stderr,
        )
        return 1

    include = []
    for name in requested:
        entry = by_name[name]
        include.append(
            {
                "name": entry["name"],
                "repo": entry["repo"],
                "default_branch": entry["default_branch"],
                "dependency_name": entry.get("dependency_name", "mathlib"),
            }
        )

    args.output.write_text(json.dumps({"include": include}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
