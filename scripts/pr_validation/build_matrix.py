#!/usr/bin/env python3
"""Build the validation job matrix from the inventory and a list of entries.

Reads the downstream inventory and intersects it with the requested
entries, emitting a GitHub Actions matrix `include` list with the fields
the validate job needs.

Comment grammar (each comma-separated entry):

    <name-or-slug>[@<rev>] [--merge-branch]

* `<name-or-slug>` (required) — either the downstream's short inventory
  name (case-sensitive match against `inventory.downstreams[*].name`)
  or its GitHub `owner/repo` slug (case-insensitive match against
  `inventory.downstreams[*].repo`). The bare token is resolved against
  the inventory here; the mathlib-ci side passes it through verbatim
  so this script is the single source of truth for name resolution.
* `@<rev>` (optional) — any git refspec for the downstream checkout
  (branch / tag / commit SHA). Defaults to the inventory's
  `default_branch`.
* `--merge-branch` (optional, per-entry) — flips that single entry from
  the default LKG mode to merge mode (i.e. test against the PR's
  would-be-merged tree instead of cherry-picking onto the downstream's
  last-known-good mathlib commit).

This script is invoked by the plan job in
``.github/workflows/mathlib-pr-validation.yml``.
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

    # Two lookups: short inventory name (case-sensitive) and GitHub
    # `owner/repo` slug (case-insensitive, mirroring GitHub URL
    # semantics). Each entry's bare token is matched against either,
    # in that order.
    by_name = {entry["name"]: entry for entry in inventory["downstreams"]}
    by_slug = {
        entry["repo"].lower(): entry for entry in inventory["downstreams"]
    }

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

    # Resolve each bare token to a canonical inventory entry before dedup
    # so `FLT` and `leanprover-community/FLT` collapse to the same row.
    # We preserve the user's literal token as `requested_name` for display.
    resolved: list[tuple[str, dict[str, Any], str | None, str]] = []
    unknown: list[str] = []
    for requested_name, rev, mode in parsed:
        entry_meta = by_name.get(requested_name) or by_slug.get(
            requested_name.lower()
        )
        if entry_meta is None:
            unknown.append(requested_name)
            continue
        resolved.append((requested_name, entry_meta, rev, mode))
    if unknown:
        print(
            "error: unknown downstream(s): "
            + ", ".join(sorted(set(unknown)))
            + " (must match an inventory short name or owner/repo slug)",
            file=sys.stderr,
        )
        return 1

    # Dedup on (canonical-name, rev, mode). Two requests that resolve to
    # the same downstream (e.g. `FLT` and `leanprover-community/FLT`) with
    # the same rev + mode collapse to one matrix row; the first form the
    # user typed wins as `requested_name`.
    seen: set[tuple[str, str | None, str]] = set()
    deduped: list[tuple[str, dict[str, Any], str | None, str]] = []
    for requested_name, entry_meta, rev, mode in resolved:
        key = (entry_meta["name"], rev, mode)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((requested_name, entry_meta, rev, mode))

    # We always try to fetch the LKG snapshot. LKG mode entries need
    # `last_known_good_commit` from it (hard requirement); both modes use
    # `first_known_bad_commit` to surface a definitive master-health
    # caveat in the comment (best-effort enrichment — proceed without it
    # when the snapshot is unreachable).
    needs_lkg = any(mode == _MODE_LKG for _, _, _, mode in deduped)
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
    for requested_name, entry_meta, rev, mode in deduped:
        name = entry_meta["name"]
        item: dict[str, Any] = {
            "name": name,
            # The literal token the user typed (short name or slug). Used
            # by post_results.py for the displayed entry label so the
            # rendered comment mirrors the request; all internal keying
            # (artifacts, prose, dedup) uses the canonical `name`.
            "requested_name": requested_name,
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
            entry = snapshot.get("downstreams", {}).get(name, {})
            fkb = entry.get("first_known_bad_commit")
            if fkb:
                item["fkb_commit"] = fkb
            # Merge-mode entries surface the recorded last-known-good as an
            # informational baseline: it's the SHA behind the comment's
            # "master builds with <name>" claim, so naming it lets the
            # renderer flag when that baseline may be stale. LKG mode set
            # lkg_commit above (it's the commit actually built against), so
            # only fill the gap for the other modes here.
            if mode != _MODE_LKG and "lkg_commit" not in item:
                lkg = entry.get("last_known_good_commit")
                if lkg:
                    item["lkg_commit"] = lkg
        include.append(item)

    args.output.write_text(json.dumps({"include": include}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
