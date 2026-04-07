"""Shared domain types for the downstream regression workflow."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Outcome(str, Enum):
    """Possible outcomes for one downstream validation attempt."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


@dataclass(frozen=True)
class DownstreamConfig:
    """Inventory entry for one downstream repository."""

    name: str
    repo: str
    default_branch: str
    dependency_name: str = "mathlib"
    enabled: bool = True
    bumping_branch: str | None = None
    skip_already_good: bool = True
    skip_known_bad_bisect: bool = True


@dataclass(frozen=True)
class CommitDetail:
    """One upstream commit plus the title shown in reports."""

    sha: str
    title: str


@dataclass
class WindowSelection:
    """Persisted output from the pre-probe window-selection step."""

    schema_version: int = 1
    needs_probe: bool = False
    downstream: str | None = None
    repo: str | None = None
    default_branch: str | None = None
    dependency_name: str = "mathlib"
    downstream_commit: str | None = None
    upstream_ref: str | None = None
    target_commit: str | None = None
    search_mode: str = "head-only"
    tested_commits: list[str] = field(default_factory=list)
    tested_commit_details: list[CommitDetail] = field(default_factory=list)
    commit_window_truncated: bool = False
    head_probe_outcome: str | None = None
    head_probe_failure_stage: str | None = None
    head_probe_summary: str | None = None
    pinned_commit: str | None = None
    selected_lower_bound_commit: str | None = None
    decision_reason: str | None = None
    next_action: str | None = None
    # `--from`/`--to` refs for the bisect probe step.  Computed by the window-
    # selection step (which has the local mathlib clone) and stored here so the
    # probe step can invoke the tool without its own mathlib clone.
    probe_from_ref: str | None = None
    probe_to_ref: str | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WindowSelection":
        """Decode one persisted selection payload."""

        field_names = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in payload.items() if k in field_names}
        # Handle nested CommitDetail objects.
        if "tested_commit_details" in kwargs:
            kwargs["tested_commit_details"] = [
                CommitDetail(**detail) for detail in kwargs["tested_commit_details"]
            ]
        return cls(**kwargs)

    def to_json(self) -> dict[str, Any]:
        """Serialize the selection using plain JSON-compatible values."""

        return asdict(self)


@dataclass
class ValidationResult:
    """Machine-readable result for one downstream validation run."""

    schema_version: int
    downstream: str
    repo: str
    default_branch: str
    downstream_commit: str | None
    dependency_name: str
    upstream_ref: str
    target_commit: str | None
    tested_commits: list[str]
    commit_window_truncated: bool
    outcome: Outcome
    failure_stage: str | None
    first_failing_commit: str | None
    last_successful_commit: str | None
    summary: str
    error: str | None
    generated_at: str
    search_mode: str = "head-only"
    tested_commit_details: list[CommitDetail] = field(default_factory=list)
    head_probe_outcome: str | None = None
    head_probe_failure_stage: str | None = None
    head_probe_summary: str | None = None
    culprit_log_path: str | None = None
    pinned_commit: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Serialize the result using plain JSON-compatible values."""

        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload


def utc_now() -> str:
    """Return a stable UTC timestamp string."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_inventory(path: Path) -> dict[str, DownstreamConfig]:
    """Load the JSON inventory and index it by downstream name."""

    payload = json.loads(path.read_text())
    entries = payload.get("downstreams", [])
    return {
        entry["name"]: DownstreamConfig(**entry)
        for entry in entries
        if entry.get("enabled", True)
    }
