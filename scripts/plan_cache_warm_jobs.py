#!/usr/bin/env python3
"""Emit a GitHub Actions matrix of mathlib SHAs to cache-warm.

Two modes:

* **DB / inventory mode** (default): reads the regression-workflow
  ``downstream_status`` rows for every enabled inventory entry, collects
  every non-null ``last_known_good_commit`` and ``first_known_bad_commit``,
  and deduplicates by SHA. Warming covers every enabled downstream because
  ``lkg/latest.json`` publishes every enabled downstream — the published
  warmth contract has no opt-in carve-out.

* **Manual mode** (``--manual-shas a,b,c``): bypasses inventory + DB and
  emits one matrix entry per supplied SHA. Used by ``workflow_dispatch``
  for testing or one-off backfills.

Output JSON shape::

    {
      "include": [
        {"sha": "<40-hex>", "tag": "lkg|fkb|both|manual",
         "downstreams": ["physlib", "FLT"]},
        ...
      ],
      "skipped": [
        {"sha": "<40-hex>", "tag": "lkg|fkb|both",
         "downstreams": ["physlib", "FLT"],
         "status": "cache_warmth_hit|retry_backoff|retry_exhausted",
         "detail": "<human-readable reason>"},
        ...
      ]
    }

Empty matrices are valid (``include: []``); the orchestrator workflow
gates downstream jobs on a separate ``has_jobs`` boolean. ``skipped``
lists the SHAs the planner dropped this tick — verified warm, waiting
out a retry backoff, or out of retry budget — so the orchestrator's
summary can list them alongside the SHAs that actually went through
the matrix.

Retry policy: mathlib master always builds, so every failed warming
attempt (``build_failed`` / ``push_failed`` / ``verify_failed``) is infra
trouble, not a property of the SHA. A failed SHA is re-planned once its
backoff has elapsed (``BACKOFF_BASE_HOURS`` doubling per recorded
attempt) until ``MAX_WARM_ATTEMPTS`` is reached; after that the SHA is
reported as ``retry_exhausted`` and needs operator attention (or a
``--manual-shas`` backfill, which bypasses the filter entirely).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.models import DownstreamConfig, load_inventory
from scripts.storage import (
    CacheWarmthRecord,
    DownstreamStatusRecord,
    add_backend_args,
    create_backend,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Warming attempts recorded per SHA before the planner stops rescheduling
# it. A SHA that exhausts this budget is surfaced as `retry_exhausted`.
MAX_WARM_ATTEMPTS = 5

# Backoff before re-attempting a failed SHA: 6h (one scheduled tick) after
# the first recorded attempt, doubling per attempt (6h, 12h, 24h, 48h).
BACKOFF_BASE_HOURS = 6.0


def retry_delay(attempts: int) -> timedelta:
    """Return how long a SHA with *attempts* recorded failures must wait."""
    return timedelta(hours=BACKOFF_BASE_HOURS * 2 ** (max(attempts, 1) - 1))


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


def _classify(
    record: CacheWarmthRecord | None, now: datetime
) -> tuple[str | None, str]:
    """Classify one SHA against its warmth record.

    Returns ``(skip_status, detail)``. ``skip_status`` is ``None`` when the
    SHA should enter the matrix this tick.
    """
    if record is None:
        return None, ""
    if record.is_warm:
        return "cache_warmth_hit", f"verified {record.status}"
    detail = f"{record.status}, attempt {record.attempts}/{MAX_WARM_ATTEMPTS}"
    if record.attempts >= MAX_WARM_ATTEMPTS:
        return "retry_exhausted", detail
    try:
        last_attempt = datetime.fromisoformat(record.last_attempt_at)
    except ValueError:
        return None, detail
    due_at = last_attempt.astimezone(timezone.utc) + retry_delay(record.attempts)
    if now < due_at:
        return "retry_backoff", f"{detail}, retry after {due_at.isoformat()}"
    return None, detail


def build_matrix_from_db(
    inventory: dict[str, DownstreamConfig],
    statuses: dict[str, DownstreamStatusRecord],
    warmth: dict[str, CacheWarmthRecord] | None = None,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the matrix include list from inventory + DB statuses.

    Considers every inventory entry (callers pass enabled downstreams
    only). Each SHA's ``tag`` reflects the union of roles across
    downstreams: a SHA that's LKG for one project and FKB for another is
    tagged ``both``.

    Returns ``(include, skipped)``: the first list is the matrix of SHAs
    to probe this run, the second is candidate SHAs the *warmth* records
    dropped this tick — verified warm (skipped forever: mathlib's olean
    cache is content-hashed and immutable per SHA), in retry backoff, or
    out of retry budget. Skipped entries carry ``status`` and ``detail``
    fields so the orchestrator can render a unified summary of "what we
    considered" rather than just "what we ran".
    """
    warmth = warmth or {}
    now = now or datetime.now(timezone.utc)

    # sha -> {"downstreams": ordered list, "roles": set of "lkg"/"fkb"}
    by_sha: dict[str, dict[str, Any]] = {}

    for name in sorted(inventory):
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

    def _entry(sha: str) -> dict[str, Any]:
        meta = by_sha[sha]
        roles = meta["roles"]
        if roles == {"lkg"}:
            tag = "lkg"
        elif roles == {"fkb"}:
            tag = "fkb"
        else:
            tag = "both"
        return {
            "sha": sha,
            "short_sha": sha[:7],
            "tag": tag,
            "downstreams": meta["downstreams"],
        }

    include: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for sha in sorted(by_sha):
        skip_status, detail = _classify(warmth.get(sha), now)
        if skip_status is None:
            include.append(_entry(sha))
        else:
            skipped.append({**_entry(sha), "status": skip_status, "detail": detail})
    return include, skipped


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
        skipped: list[dict[str, Any]] = []
        mode = "manual"
    else:
        inventory = load_inventory(Path(args.inventory), include_disabled=False)
        backend = create_backend(args.backend, dsn=args.dsn)
        statuses = backend.load_all_statuses("regression", args.upstream)
        warmth = backend.load_cache_warmth(args.upstream)
        include, skipped = build_matrix_from_db(inventory, statuses, warmth)
        mode = "inventory+DB"

    payload = {"include": include, "skipped": skipped}
    Path(args.output).write_text(json.dumps(payload, indent=2))
    exhausted = [e for e in skipped if e["status"] == "retry_exhausted"]
    print(
        f"Cache-warming plan: {len(include)} SHA(s) to warm, "
        f"{len(skipped)} skipped via cache_warmth "
        f"({len(exhausted)} out of retry budget) ({mode})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
