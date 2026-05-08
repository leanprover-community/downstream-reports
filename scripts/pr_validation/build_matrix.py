#!/usr/bin/env python3
"""Build the validation job matrix from the inventory and a list of names.

Reads the downstream inventory and intersects it with the requested names,
emitting a GitHub Actions matrix include list with the fields the validate
job needs.

Per-name modes
--------------
A name may carry an optional ``@<mode>`` suffix selecting how the PR should
be tested against that downstream. The recognised modes are:

* (no suffix) — *merge mode* (default). Validate the downstream against the
  PR's would-be-merged tree exactly as GitHub computed it.
* ``@lkg`` — *LKG mode*. Cherry-pick the PR's commits onto the downstream's
  last-known-good mathlib commit (looked up from the published
  ``lkg/latest.json`` snapshot) and validate the downstream against that
  rebased tree. Yields a verdict that's independent of current mathlib
  master health.

Mode selection is per-downstream: ``FLT@lkg, Toric`` runs FLT in LKG mode
and Toric in merge mode in the same dispatch.

This script is invoked by the plan job in
``.github/workflows/mathlib-pr-validation.yml``. The dispatching mathlib4
workflow has already validated the names against the inventory; we
re-validate here as defence in depth because the dispatched payload is
untrusted input.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_LKG_SNAPSHOT_URL = (
    "https://downstreamreports.z13.web.core.windows.net/lkg/latest.json"
)

# Mode tokens recognised on the right-hand side of an ``@`` suffix.
_LKG_MODE = "lkg"
_MERGE_MODE = "merge"


def _parse_name_token(token: str) -> tuple[str, str]:
    """Split a ``name[@mode]`` token into ``(name, mode)``.

    Raises ``ValueError`` if the suffix is unknown. The bare-name half is
    returned trimmed; the only currently recognised suffix is ``@lkg``.
    """

    bare, _, mode = token.partition("@")
    bare = bare.strip()
    if not bare:
        raise ValueError(f"empty downstream name in token: {token!r}")
    if not mode:
        return bare, _MERGE_MODE
    if mode != _LKG_MODE:
        raise ValueError(
            f"unknown mode '@{mode}' for {bare}; only @lkg is supported"
        )
    return bare, _LKG_MODE


def _fetch_lkg_snapshot(url: str) -> dict[str, Any]:
    """Fetch the LKG snapshot JSON from *url*.

    Raises ``RuntimeError`` with a stable message on transport / parse error
    so callers can surface a single ``error: ...`` line to ``$GITHUB_OUTPUT``.
    """

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"could not fetch LKG snapshot at {url}: {exc}") from exc

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LKG snapshot at {url} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or "downstreams" not in data:
        raise RuntimeError(
            f"LKG snapshot at {url} missing 'downstreams' key (got: {sorted(data)})"
        )
    return data


def _resolve_lkg_commit(snapshot: dict[str, Any], name: str) -> str:
    """Return the recorded LKG commit for *name* or raise ``ValueError``."""

    downstreams = snapshot.get("downstreams", {})
    entry = downstreams.get(name)
    if entry is None:
        raise ValueError(
            f"{name} is not present in the LKG snapshot; "
            "remove the @lkg suffix or wait for a successful regression run"
        )
    commit = entry.get("last_known_good_commit")
    if not commit:
        raise ValueError(
            f"{name} has no recorded last_known_good_commit; "
            "remove the @lkg suffix or wait for a successful regression run"
        )
    return commit


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
        help="Comma-separated downstream names; each may carry an @lkg suffix.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write the matrix JSON.",
    )
    parser.add_argument(
        "--lkg-snapshot-url",
        default=DEFAULT_LKG_SNAPSHOT_URL,
        help=(
            "URL of the published LKG snapshot. Only fetched when at least one"
            " requested name has an @lkg suffix."
        ),
    )
    args = parser.parse_args()

    with args.inventory.open() as handle:
        inventory = json.load(handle)

    by_name = {entry["name"]: entry for entry in inventory["downstreams"]}

    raw_tokens = [t.strip() for t in args.names.split(",") if t.strip()]
    if not raw_tokens:
        print("error: --names is empty", file=sys.stderr)
        return 1

    parsed: list[tuple[str, str]] = []  # (name, mode)
    for token in raw_tokens:
        try:
            parsed.append(_parse_name_token(token))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # Reject duplicate-mode requests (same name appearing twice in the same mode).
    # `FLT` and `FLT@lkg` are distinct entries and intentionally allowed
    # together — they exercise the same downstream against different mathlib
    # trees, which can be useful for diagnostics.
    seen: set[tuple[str, str]] = set()
    for name, mode in parsed:
        if (name, mode) in seen:
            print(
                f"error: duplicate request for {name} in mode {mode}",
                file=sys.stderr,
            )
            return 1
        seen.add((name, mode))

    unknown = [name for name, _ in parsed if name not in by_name]
    if unknown:
        print(
            f"error: unknown downstream(s): {', '.join(sorted(set(unknown)))}",
            file=sys.stderr,
        )
        return 1

    needs_snapshot = any(mode == _LKG_MODE for _, mode in parsed)
    snapshot: dict[str, Any] | None = None
    if needs_snapshot:
        try:
            snapshot = _fetch_lkg_snapshot(args.lkg_snapshot_url)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    include: list[dict[str, Any]] = []
    for name, mode in parsed:
        entry = by_name[name]
        item: dict[str, Any] = {
            "name": entry["name"],
            "repo": entry["repo"],
            "default_branch": entry["default_branch"],
            "dependency_name": entry.get("dependency_name", "mathlib"),
            "mode": mode,
        }
        if mode == _LKG_MODE:
            assert snapshot is not None  # set above when needs_snapshot
            try:
                item["lkg_commit"] = _resolve_lkg_commit(snapshot, name)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        include.append(item)

    args.output.write_text(json.dumps({"include": include}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
