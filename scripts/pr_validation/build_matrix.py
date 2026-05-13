#!/usr/bin/env python3
"""Build the validation job matrix from the inventory and a list of entries.

Reads the downstream inventory and intersects it with the requested
entries, emitting a GitHub Actions matrix `include` list with the fields
the validate job needs.

Comment grammar (each comma-separated entry):

    <name>[@<rev>] [--merge-branch]

* `<name>` (required) — must match an `inventory.downstreams[*].name`
  (case-sensitive).
* `@<rev>` (optional) — any git refspec for the downstream checkout
  (branch / tag / commit SHA). Defaults to the inventory's
  `default_branch`.
* `--merge-branch` (optional, per-entry) — flips that single entry from
  the default LKG mode to merge mode (i.e. test against the PR's
  would-be-merged tree instead of cherry-picking onto the downstream's
  last-known-good mathlib commit).

This script is invoked by the plan job in
``.github/workflows/mathlib-pr-validation.yml``. The dispatching
mathlib4 workflow has already parsed and validated the entries; we
re-validate here as defence in depth because the dispatched payload is
untrusted input.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_LKG_SNAPSHOT_URL = (
    "https://downstreamreports.z13.web.core.windows.net/lkg/latest.json"
)

_MERGE_BRANCH_FLAG = "--merge-branch"

# Mode tokens stored in matrix entries / result.json (kept stable).
_MODE_LKG = "lkg"
_MODE_MERGE = "merge"

# Sentinel inserted into artifact / job names when no `@<rev>` was given.
_DEFAULT_REV_SLUG = "default"

# Artifact names must be filesystem-safe; we sanitise revs aggressively.
_REV_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slugify_rev(rev: str | None) -> str:
    """Return a filesystem-safe slug for a rev, or the default sentinel."""

    if not rev:
        return _DEFAULT_REV_SLUG
    slug = _REV_SLUG_RE.sub("_", rev).strip("_")
    return slug or _DEFAULT_REV_SLUG


def _parse_entry(entry: str) -> tuple[str, str | None, str]:
    """Split `<name>[@<rev>] [--merge-branch]` into (name, rev, mode).

    `rev` is `None` when no `@<rev>` suffix was supplied; the validate
    step then falls back to the inventory's `default_branch`. Raises
    ``ValueError`` on unknown flags or empty bare names.
    """

    tokens = entry.split()
    if not tokens:
        raise ValueError("empty entry")

    name_rev = tokens[0]
    flags = tokens[1:]

    bare, sep, rev = name_rev.partition("@")
    bare = bare.strip()
    if not bare:
        raise ValueError(f"empty downstream name in entry: {entry!r}")
    rev_value: str | None
    if not sep:
        rev_value = None
    else:
        rev_value = rev.strip() or None
        if rev_value is None:
            raise ValueError(
                f"empty rev after `@` in entry: {entry!r}"
            )

    mode = _MODE_LKG
    for flag in flags:
        if flag == _MERGE_BRANCH_FLAG:
            mode = _MODE_MERGE
        else:
            raise ValueError(
                f"unknown flag {flag!r} in entry {entry!r}"
                f" (only {_MERGE_BRANCH_FLAG} is supported)"
            )

    return bare, rev_value, mode


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
            "wait for a successful regression run or use --merge-branch"
        )
    commit = entry.get("last_known_good_commit")
    if not commit:
        raise ValueError(
            f"{name} has no recorded last_known_good_commit; "
            "wait for a successful regression run or use --merge-branch"
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
        help=(
            "Comma-separated `<name>[@<rev>] [--merge-branch]` entries"
            " as resolved by the mathlib-ci validate_names.sh step."
        ),
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
            "URL of the published LKG snapshot. Only fetched when at least"
            " one requested entry uses the default LKG mode."
        ),
    )
    args = parser.parse_args()

    with args.inventory.open() as handle:
        inventory = json.load(handle)

    by_name = {entry["name"]: entry for entry in inventory["downstreams"]}

    raw_entries = [t.strip() for t in args.names.split(",") if t.strip()]
    if not raw_entries:
        print("error: --names is empty", file=sys.stderr)
        return 1

    parsed: list[tuple[str, str | None, str]] = []
    for entry in raw_entries:
        try:
            parsed.append(_parse_entry(entry))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # Dedup on (name, rev, mode) — distinct fields are intentionally allowed
    # (e.g. `FLT, FLT@main` runs twice if the user wrote two entries; `FLT,
    # FLT --merge-branch` runs twice in distinct modes). Exact duplicates
    # are silently collapsed.
    seen: set[tuple[str, str | None, str]] = set()
    deduped: list[tuple[str, str | None, str]] = []
    for triple in parsed:
        if triple in seen:
            continue
        seen.add(triple)
        deduped.append(triple)

    unknown = sorted({name for name, _, _ in deduped if name not in by_name})
    if unknown:
        print(
            f"error: unknown downstream(s): {', '.join(unknown)}",
            file=sys.stderr,
        )
        return 1

    # We always try to fetch the LKG snapshot. LKG mode entries need
    # `last_known_good_commit` from it (hard requirement); both modes use
    # `first_known_bad_commit` to surface a definitive master-health
    # caveat in the comment (best-effort enrichment — proceed without it
    # when the snapshot is unreachable).
    needs_lkg = any(mode == _MODE_LKG for _, _, mode in deduped)
    snapshot: dict[str, Any] | None = None
    try:
        snapshot = _fetch_lkg_snapshot(args.lkg_snapshot_url)
    except RuntimeError as exc:
        if needs_lkg:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(
            f"warning: {exc} (proceeding without FKB enrichment)",
            file=sys.stderr,
        )

    include: list[dict[str, Any]] = []
    for name, rev, mode in deduped:
        entry_meta = by_name[name]
        item: dict[str, Any] = {
            "name": entry_meta["name"],
            "repo": entry_meta["repo"],
            "default_branch": entry_meta["default_branch"],
            "dependency_name": entry_meta.get("dependency_name", "mathlib"),
            "mode": mode,
            # `rev` is the verbatim refspec the user requested, or `""` when
            # they didn't supply one (the validate step then falls back to
            # default_branch). The slug form is a filesystem-safe version
            # used in artifact names / job titles.
            "rev": rev or "",
            "rev_slug": _slugify_rev(rev),
        }
        if mode == _MODE_LKG:
            assert snapshot is not None  # set above when needs_lkg
            try:
                item["lkg_commit"] = _resolve_lkg_commit(snapshot, name)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        # FKB enrichment: when the snapshot is reachable and records a
        # first_known_bad_commit for this downstream, attach it. The
        # comment renderer turns this into a definitive "master is
        # currently broken for <name>" caveat.
        if snapshot is not None:
            fkb = (
                snapshot.get("downstreams", {})
                .get(name, {})
                .get("first_known_bad_commit")
            )
            if fkb:
                item["fkb_commit"] = fkb
        include.append(item)

    args.output.write_text(json.dumps({"include": include}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
