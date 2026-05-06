"""Storage backend abstraction for downstream-regression state.

The ``StorageBackend`` protocol is designed around domain concepts, not around
how data happens to be physically stored.  This lets you swap out the
underlying store (git-branch JSON files today, a relational database tomorrow)
without touching any business logic.

Protocol overview
-----------------
``load_all_statuses(workflow)``
    Returns the current regression-episode state for every downstream tracked
    by *workflow*.  A relational backend returns rows from
    ``downstream_status``; the filesystem backend reads a JSON file.

``save_run(...)``
    Atomically persists the results of one workflow run, the updated episode
    state for each downstream, and an optional pre-rendered markdown report.
    A relational backend INSERTs into ``run`` and ``run_result``; the
    filesystem backend writes JSON and markdown files.

``load_tested_downstream_commits(workflow)``
    Return which (downstream, downstream_commit) pairs already have a
    non-error result in *workflow*, so the on-demand plan step can
    skip commits that have already been validated.

``load_prior_results(workflow, pairs)``
    Return rich prior-result details for specific (downstream, commit) pairs.
    Used to annotate skipped on-demand downstreams with their previous outcome.

Adding a new backend
--------------------
1.  Implement a class whose public methods match ``StorageBackend``.
2.  Instantiate it in ``aggregate_results.py`` (and the ``select_*``
    scripts for ``load_all_statuses``) based on a CLI flag, e.g.
    ``--backend postgres --dsn "$POSTGRES_DSN"``.
3.  Update the YAML workflows to supply connection credentials instead of the
    git-worktree setup steps — no Python business logic needs to change.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Domain data types
# ---------------------------------------------------------------------------


@dataclass
class DownstreamStatusRecord:
    """Current regression-episode state for one downstream.

    Maps to a row in ``downstream_status(downstream, workflow, ...)`` in a
    relational schema, or to one entry inside ``status/{key}.json`` on the
    filesystem.
    """

    last_known_good_commit: str | None = None
    first_known_bad_commit: str | None = None
    pinned_commit: str | None = None
    downstream_commit: str | None = None
    last_good_release: str | None = None         # e.g. "v4.13.0"
    last_good_release_commit: str | None = None  # resolved SHA for that tag


@dataclass
class ValidateJobRecord:
    """CI job metadata for one downstream's validate job in one run.

    Maps to a row in ``validate_job(run_id, downstream, ...)`` in a relational
    schema.  The filesystem backend does not persist this separately — the job
    URL is already embedded in the rendered markdown report.
    """

    downstream: str
    job_id: str
    job_url: str
    started_at: str | None = None   # ISO-8601 timestamp
    finished_at: str | None = None  # ISO-8601 timestamp
    conclusion: str | None = None   # 'success' | 'failure' | 'cancelled' | 'skipped'


@dataclass
class LatestRunRecord:
    """Subset of run metadata exposed publicly per downstream.

    Produced by ``load_latest_run_per_downstream`` (SQL-only).  Maps to one
    entry inside ``runs/latest.json`` on the public Azure blob endpoint.  Any
    field may be ``None`` when the underlying row is missing (e.g. no
    validate_job recorded for that run).
    """

    run_id: str
    run_url: str
    reported_at: str                  # ISO-8601 timestamp
    target_commit: str | None
    downstream_commit: str | None
    outcome: str                      # 'passed' | 'failed' | 'error'
    episode_state: str
    first_known_bad: str | None
    last_known_good: str | None
    job_id: str | None = None
    job_url: str | None = None
    # Download URL for the `culprit-log-<name>` artifact uploaded by the probe job.
    # The log text itself is not persisted — consumers fetch the artifact when they
    # need the contents, so the database stays free of arbitrary build output.
    culprit_log_artifact_url: str | None = None


@dataclass
class ManifestWatcherLedgerRow:
    """One row in the manifest-watcher dispatch ledger.

    Maps to a row in ``manifest_watcher_ledger`` (SQL) or to one entry inside
    ``manifest_watcher_ledger.json`` on the filesystem.  The watcher writes one
    row per dispatched downstream so subsequent ticks don't re-dispatch the
    same ``(downstream, observed_pin)`` until ``dispatched_at`` ages out.
    """

    downstream: str
    observed_pin: str
    dispatched_at: str           # ISO-8601 timestamp
    run_url: str | None = None


@dataclass
class RunResultRecord:
    """Complete result for one downstream in one workflow run.

    Maps to a row in ``run_result(run_id, downstream, ...)`` in a relational
    schema, or to one entry in ``results[*]`` of ``latest.json`` / a per-run
    history file on the filesystem.
    """

    upstream: str
    downstream: str
    repo: str
    downstream_commit: str | None
    outcome: str                     # 'passed' | 'failed' | 'error'
    episode_state: str               # 'passing' | 'new_failure' | 'failing' | 'recovered' | 'error'
    target_commit: str | None
    previous_last_known_good: str | None
    previous_first_known_bad: str | None
    last_known_good: str | None
    first_known_bad: str | None
    current_last_successful: str | None
    current_first_failing: str | None
    failure_stage: str | None
    search_mode: str
    commit_window_truncated: bool
    error: str | None
    head_probe_outcome: str | None
    head_probe_failure_stage: str | None
    culprit_log_text: str | None
    pinned_commit: str | None = None
    age_commits: int | None = None   # commits between pinned_commit and target_commit
    bump_commits: int | None = None  # commits between pinned_commit and last_known_good
    last_good_release: str | None = None  # latest semver release tag reachable from LKG
    search_base_not_ancestor: bool = False
    # Direct download URL for the `culprit-log-<name>` artifact, when one was uploaded.
    # Falls back to None when no culprit log was produced (passing run, error before
    # build) or when artifact-id resolution failed in the report job.  The log text
    # itself (`culprit_log_text` above) is held only in memory for the in-process
    # markdown report and Zulip alert payload — never written to SQL.
    culprit_log_artifact_url: str | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Read/write contract for downstream-regression state.

    All methods use domain types (``DownstreamStatusRecord``,
    ``RunResultRecord``) rather than raw dicts or filesystem keys.  The two
    supported workflow identifiers are ``"regression"`` (the main head-tracking
    run) and ``"ondemand"`` (the downstream on-demand/bumping-branch run).
    """

    def load_all_statuses(self, workflow: str, upstream: str) -> dict[str, DownstreamStatusRecord]:
        """Return current episode state for every downstream in *workflow*.

        Returns an empty dict when no state has been stored yet.
        """
        ...

    def save_run(
        self,
        *,
        run_id: str,
        workflow: str,
        upstream: str,
        upstream_ref: str,
        run_url: str,
        created_at: str,
        results: list[RunResultRecord],
        updated_statuses: dict[str, DownstreamStatusRecord],
        report_markdown: str | None = None,
        validate_jobs: list[ValidateJobRecord] | None = None,
    ) -> None:
        """Persist run results and updated regression state atomically.

        *report_markdown* is the pre-rendered GitHub-summary report.
        Filesystem backends cache it as a ``.md`` file; database backends may
        store it in a text column or ignore it — the markdown can always be
        regenerated from the structured data.

        *validate_jobs* carries CI job metadata (timing, conclusion) for each
        downstream's validate job.  The filesystem backend ignores this — the
        job URL is already embedded in the markdown report.  The SQL backend
        inserts one row per entry into ``validate_job``.
        """
        ...

    def load_tested_downstream_commits(self, workflow: str) -> set[tuple[str, str]]:
        """Return ``{(downstream, downstream_commit)}`` pairs with a non-error
        result in *workflow*, used for dedup in on-demand runs."""
        ...

    def load_prior_results(
        self, workflow: str, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """Return rich prior-result details for ``(downstream, downstream_commit)`` pairs.

        Used by the on-demand plan step to annotate skipped downstreams with
        their previous outcome, first-known-bad, and links to the original run.

        Returns ``{(downstream, downstream_commit): {...}}`` where each value
        contains at minimum: ``outcome``, ``episode_state``, ``first_known_bad``,
        ``target_commit``, ``failure_stage``, ``repo``, ``run_url``, ``job_url``.

        # FIXME: this docstring does not specify the tie-break when a
        # ``(downstream, commit)`` pair has multiple matching runs — the
        # implementation returns the **newest** (latest ``created_at``)
        # result, and on-demand callers depend on that ordering.  See
        # scripts/test_storage.py::test_load_prior_results_returns_newest_when_a_pair_has_multiple_runs
        # for the executable contract.  Update the docstring to call out
        # newest-wins next time this function is touched.

        Error outcomes are excluded — a run that errored is not a
        conclusive prior result.
        """
        ...

    def load_known_warm_shas(self, upstream: str) -> set[str]:
        """Return the set of upstream SHAs already confirmed warm in the cache.

        Used by the cache-warming planner to skip SHAs whose Azure cache state
        was already verified by a previous warm run. Backends with no
        persistence (filesystem, dry-run) return an empty set so planning
        degrades gracefully off SQL.
        """
        ...

    def record_warm_shas(self, upstream: str, shas: Iterable[str]) -> None:
        """Mark *shas* as confirmed warm for *upstream* (idempotent upsert)."""
        ...

    def load_manifest_watcher_ledger(
        self, upstream: str
    ) -> dict[str, "ManifestWatcherLedgerRow"]:
        """Return the watcher's per-downstream dispatch ledger for *upstream*.

        Empty dict when no rows exist.  The watcher uses this to skip
        re-dispatching the same ``(downstream, observed_pin)`` while a previous
        dispatch is still in flight, with a TTL fallback for stuck rows.
        """
        ...

    def upsert_manifest_watcher_ledger(
        self, upstream: str, rows: list["ManifestWatcherLedgerRow"]
    ) -> None:
        """Insert or replace ledger rows for *upstream* (one per downstream)."""
        ...


# ---------------------------------------------------------------------------
# Filesystem implementation
# ---------------------------------------------------------------------------

_WORKFLOW_STATUS_KEY: dict[str, str] = {
    "regression": "current",
    "ondemand": "ondemand-current",
}
_WORKFLOW_REPORT_KEY: dict[str, str] = {
    "regression": "latest",
    "ondemand": "ondemand-latest",
}
_WORKFLOW_HISTORY_PREFIX: dict[str, str] = {
    "regression": "",
    "ondemand": "ondemand",
}


def result_to_row(r: RunResultRecord) -> dict[str, Any]:
    """Serialise a ``RunResultRecord`` to a flat dict.

    Used by ``FilesystemBackend`` when writing JSON files and by
    ``aggregate_results.render_report`` which expects this shape.
    """
    return asdict(r)


class FilesystemBackend:
    """State backend backed by a local directory tree.

    Layout under *state_root* (e.g. ``state-branch/ci``):

    .. code-block:: text

        status/
            current.json           regression episode state
            ondemand-current.json  ondemand episode state
            (results also queried for dedup via load_tested_downstream_commits)
        reports/
            latest.json / latest.md
            ondemand-latest.json / ondemand-latest.md
        results/
            {day}/{run_id}/{downstream}.json
            ondemand/{day}/{run_id}/{downstream}.json

    The JSON files preserve the existing on-disk schema so that the git state
    branch remains readable without any migration.
    """

    def __init__(self, state_root: Path) -> None:
        self._root = state_root

    # ------------------------------------------------------------------
    # StorageBackend implementation
    # ------------------------------------------------------------------

    def load_all_statuses(self, workflow: str, upstream: str) -> dict[str, DownstreamStatusRecord]:
        path = self._root / "status" / f"{_WORKFLOW_STATUS_KEY[workflow]}.json"
        if not path.exists():
            return {}
        payload = json.loads(path.read_text())
        return {
            name: DownstreamStatusRecord(
                last_known_good_commit=data.get("last_known_good_commit"),
                first_known_bad_commit=data.get("first_known_bad_commit"),
                pinned_commit=data.get("pinned_commit"),
                downstream_commit=data.get("downstream_commit"),
                last_good_release=data.get("last_good_release"),
                last_good_release_commit=data.get("last_good_release_commit"),
            )
            for name, data in payload.get("downstreams", {}).items()
        }

    def save_run(
        self,
        *,
        run_id: str,
        workflow: str,
        upstream: str,
        upstream_ref: str,
        run_url: str,
        created_at: str,
        results: list[RunResultRecord],
        updated_statuses: dict[str, DownstreamStatusRecord],
        report_markdown: str | None = None,
        validate_jobs: list[ValidateJobRecord] | None = None,
    ) -> None:
        # validate_jobs is not persisted by the filesystem backend — the job
        # URL is already embedded in the rendered markdown report.
        status_key = _WORKFLOW_STATUS_KEY[workflow]
        report_key = _WORKFLOW_REPORT_KEY[workflow]
        history_prefix = _WORKFLOW_HISTORY_PREFIX[workflow]

        # Episode-state snapshot
        status_path = self._root / "status" / f"{status_key}.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "reported_at": created_at,
                    "downstreams": {
                        name: {
                            "last_known_good_commit": s.last_known_good_commit,
                            "first_known_bad_commit": s.first_known_bad_commit,
                            "pinned_commit": s.pinned_commit,
                            "downstream_commit": s.downstream_commit,
                            "last_good_release": s.last_good_release,
                            "last_good_release_commit": s.last_good_release_commit,
                        }
                        for name, s in sorted(updated_statuses.items())
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        # Latest-run JSON report
        rows = [result_to_row(r) for r in results]
        report_json_path = self._root / "reports" / f"{report_key}.json"
        report_json_path.parent.mkdir(parents=True, exist_ok=True)
        report_json_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "reported_at": created_at,
                    "upstream": upstream,
                    "upstream_ref": upstream_ref,
                    "run_id": run_id,
                    "run_url": run_url,
                    "results": rows,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

        # Append-only per-run history
        base = self._root / "results"
        if history_prefix:
            base = base / history_prefix
        dest = base / created_at[:10] / run_id
        dest.mkdir(parents=True, exist_ok=True)
        for r in results:
            (dest / f"{r.downstream}.json").write_text(
                json.dumps(result_to_row(r), indent=2, sort_keys=True) + "\n"
            )

        # Markdown report (optional — database backends can ignore this)
        if report_markdown is not None:
            report_md_path = self._root / "reports" / f"{report_key}.md"
            report_md_path.parent.mkdir(parents=True, exist_ok=True)
            report_md_path.write_text(report_markdown)

    def load_tested_downstream_commits(self, workflow: str) -> set[tuple[str, str]]:
        history_prefix = _WORKFLOW_HISTORY_PREFIX[workflow]
        base = self._root / "results"
        if history_prefix:
            base = base / history_prefix
        if not base.exists():
            return set()
        result: set[tuple[str, str]] = set()
        for path in base.rglob("*.json"):
            try:
                row = json.loads(path.read_text())
            except Exception:
                continue
            ds = row.get("downstream")
            commit = row.get("downstream_commit")
            outcome = row.get("outcome")
            if ds and commit and outcome in ("passed", "failed"):
                result.add((ds, commit))
        return result

    def load_prior_results(
        self, workflow: str, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if not pairs:
            return {}
        history_prefix = _WORKFLOW_HISTORY_PREFIX[workflow]
        base = self._root / "results"
        if history_prefix:
            base = base / history_prefix
        if not base.exists():
            return {}
        # Collect the newest result per (downstream, commit) pair.
        # File paths contain dates (results/{prefix}/{date}/{run_id}/{ds}.json)
        # so lexicographic path ordering approximates recency.
        best: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for path in base.rglob("*.json"):
            try:
                row = json.loads(path.read_text())
            except Exception:
                continue
            ds = row.get("downstream")
            commit = row.get("downstream_commit")
            outcome = row.get("outcome")
            if not (ds and commit and outcome in ("passed", "failed")):
                continue
            key = (ds, commit)
            if key not in pairs:
                continue
            # Use directory path as a recency proxy (date/run_id ordering).
            path_key = str(path)
            existing = best.get(key)
            if existing is None or path_key > existing[0]:
                best[key] = (path_key, row)
        return {
            key: {
                "outcome": row.get("outcome"),
                "episode_state": row.get("episode_state"),
                "first_known_bad": row.get("first_known_bad"),
                "target_commit": row.get("target_commit"),
                "failure_stage": row.get("failure_stage"),
                "repo": row.get("repo"),
                "run_url": None,   # filesystem backend doesn't store run_url per result
                "job_url": None,   # filesystem backend doesn't store job metadata
            }
            for key, (_, row) in best.items()
        }

    # The cache-warming workflow only runs against SQL in production. Off-SQL
    # backends report no known-warm SHAs (so the planner re-considers everything)
    # and silently drop record_warm_shas calls.
    def load_known_warm_shas(self, upstream: str) -> set[str]:
        return set()

    def record_warm_shas(self, upstream: str, shas: Iterable[str]) -> None:
        return None

    # ------------------------------------------------------------------
    # Manifest-watcher ledger (one JSON file per upstream)
    # ------------------------------------------------------------------

    def _ledger_path(self, upstream: str) -> Path:
        # `upstream` is "owner/repo" — keep it readable on disk.
        safe = upstream.replace("/", "__")
        return self._root / "watcher" / f"manifest-{safe}.json"

    def load_manifest_watcher_ledger(
        self, upstream: str
    ) -> dict[str, ManifestWatcherLedgerRow]:
        path = self._ledger_path(upstream)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text())
        except Exception:
            return {}
        return {
            name: ManifestWatcherLedgerRow(
                downstream=name,
                observed_pin=data["observed_pin"],
                dispatched_at=data["dispatched_at"],
                run_url=data.get("run_url"),
            )
            for name, data in payload.get("downstreams", {}).items()
            if isinstance(data, dict) and data.get("observed_pin") and data.get("dispatched_at")
        }

    def upsert_manifest_watcher_ledger(
        self, upstream: str, rows: list[ManifestWatcherLedgerRow]
    ) -> None:
        if not rows:
            return
        existing = self.load_manifest_watcher_ledger(upstream)
        for row in rows:
            existing[row.downstream] = row
        path = self._ledger_path(upstream)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "downstreams": {
                        name: {
                            "observed_pin": r.observed_pin,
                            "dispatched_at": r.dispatched_at,
                            "run_url": r.run_url,
                        }
                        for name, r in sorted(existing.items())
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


# ---------------------------------------------------------------------------
# SQL implementation (requires sqlalchemy)
# ---------------------------------------------------------------------------

try:
    from sqlalchemy import (
        Boolean,
        Column,
        DateTime,
        Integer,
        MetaData,
        String,
        Table,
        and_,
        func,
        select as sa_select,
        tuple_,
    )

    _sa_metadata = MetaData()

    _sa_downstream_status = Table(
        "downstream_status",
        _sa_metadata,
        Column("downstream", String, primary_key=True),
        Column("workflow", String, primary_key=True),
        Column("upstream", String, primary_key=True),
        Column("last_known_good", String),
        Column("first_known_bad", String),
        Column("pinned_commit", String),
        Column("downstream_commit", String),
        Column("last_good_release", String),
        Column("last_good_release_commit", String),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )

    _sa_run = Table(
        "run",
        _sa_metadata,
        Column("run_id", String, primary_key=True),
        Column("workflow", String, nullable=False),
        Column("upstream", String, nullable=False),
        Column("upstream_ref", String, nullable=False),
        Column("run_url", String, nullable=False),
        Column("started_at", DateTime(timezone=True)),
        Column("reported_at", DateTime(timezone=True), nullable=False),
    )

    _sa_run_result = Table(
        "run_result",
        _sa_metadata,
        Column("run_id", String, primary_key=True),
        Column("downstream", String, primary_key=True),
        Column("upstream", String, nullable=False),
        Column("repo", String, nullable=False),
        Column("downstream_commit", String),
        Column("outcome", String, nullable=False),
        Column("episode_state", String, nullable=False),
        Column("target_commit", String),
        Column("previous_last_known_good", String),
        Column("previous_first_known_bad", String),
        Column("last_known_good", String),
        Column("first_known_bad", String),
        Column("current_last_successful", String),
        Column("current_first_failing", String),
        Column("failure_stage", String),
        Column("search_mode", String, nullable=False),
        Column("commit_window_truncated", Boolean, nullable=False),
        Column("error", String),
        Column("head_probe_outcome", String),
        Column("head_probe_failure_stage", String),
        Column("pinned_commit", String),
        Column("age_commits", Integer),
        Column("bump_commits", Integer),
        Column("search_base_not_ancestor", Boolean, nullable=False, server_default="false"),
        Column("culprit_log_artifact_url", String),
    )

    _sa_validate_job = Table(
        "validate_job",
        _sa_metadata,
        Column("run_id", String, primary_key=True),
        Column("downstream", String, primary_key=True),
        Column("job_id", String, nullable=False),
        Column("job_url", String, nullable=False),
        Column("started_at", DateTime(timezone=True)),
        Column("finished_at", DateTime(timezone=True)),
        Column("conclusion", String),
    )

    # Records SHAs that the cache-warming workflow has confirmed as warm in the
    # mathlib Azure cache. Mathlib's olean cache is content-hashed and immutable
    # per SHA, so once a SHA is recorded here the planner can skip it forever.
    _sa_cache_warmth = Table(
        "cache_warmth",
        _sa_metadata,
        Column("upstream", String, primary_key=True),
        Column("sha", String, primary_key=True),
        Column("warmed_at", DateTime(timezone=True), nullable=False),
    )

    _sa_manifest_watcher_ledger = Table(
        "manifest_watcher_ledger",
        _sa_metadata,
        Column("downstream", String, primary_key=True),
        Column("upstream", String, primary_key=True),
        Column("observed_pin", String, nullable=False),
        Column("dispatched_at", DateTime(timezone=True), nullable=False),
        Column("run_url", String, nullable=True),
    )

    _SA_AVAILABLE = True

except ImportError:
    _sa_metadata = None  # type: ignore[assignment]
    _SA_AVAILABLE = False


def create_schema(engine: Any) -> None:
    """Create all SQL tables if they do not already exist.

    Idempotent — safe to call on a database that is already fully provisioned.
    Intended for local development and test environment setup; production
    schemas should be managed through a dedicated migration tool.
    """
    if not _SA_AVAILABLE or _sa_metadata is None:
        raise ImportError("sqlalchemy is required; pip install sqlalchemy")
    _sa_metadata.create_all(engine)


def _parse_dt(value: str) -> Any:
    """Parse an ISO-8601 timestamp string into a UTC-aware datetime."""
    from datetime import datetime, timezone
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class SqlBackend:
    """Storage backend backed by a SQL database via SQLAlchemy Core.

    Compatible with any SQLAlchemy engine — tested with PostgreSQL and SQLite.
    The SQLAlchemy table metadata defined in this module is used only for query
    construction; this class never creates or drops tables.

    Usage::

        from sqlalchemy import create_engine
        from scripts.storage import SqlBackend

        engine = create_engine("postgresql://user:pass@host/dbname")
        backend = SqlBackend(engine)
    """

    def __init__(self, engine: Any) -> None:
        if not _SA_AVAILABLE:
            raise ImportError("sqlalchemy is required; pip install sqlalchemy")
        self._engine = engine

    # ------------------------------------------------------------------
    # Dialect-level helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _insert_ignore(conn: Any, table: Any, values: dict) -> None:
        """INSERT … ON CONFLICT DO NOTHING, compatible with PostgreSQL and SQLite."""
        if conn.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        else:
            from sqlalchemy.dialects.sqlite import insert as _insert  # type: ignore[no-redef]
        conn.execute(_insert(table).values(**values).on_conflict_do_nothing())

    @staticmethod
    def _upsert(
        conn: Any,
        table: Any,
        values: dict,
        conflict_cols: list,
        update_cols: list,
    ) -> None:
        """INSERT … ON CONFLICT (…) DO UPDATE SET …, compatible with PostgreSQL and SQLite."""
        if conn.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        else:
            from sqlalchemy.dialects.sqlite import insert as _insert  # type: ignore[no-redef]
        stmt = _insert(table).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=conflict_cols,
            set_={col: getattr(stmt.excluded, col) for col in update_cols},
        )
        conn.execute(stmt)

    # ------------------------------------------------------------------
    # StorageBackend implementation
    # ------------------------------------------------------------------

    def load_all_statuses(self, workflow: str, upstream: str) -> dict[str, DownstreamStatusRecord]:
        t = _sa_downstream_status
        stmt = sa_select(
            t.c.downstream, t.c.last_known_good, t.c.first_known_bad,
            t.c.pinned_commit, t.c.downstream_commit,
            t.c.last_good_release, t.c.last_good_release_commit,
        ).where(t.c.workflow == workflow, t.c.upstream == upstream)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {
            row[0]: DownstreamStatusRecord(
                last_known_good_commit=row[1],
                first_known_bad_commit=row[2],
                pinned_commit=row[3],
                downstream_commit=row[4],
                last_good_release=row[5],
                last_good_release_commit=row[6],
            )
            for row in rows
        }

    def save_run(
        self,
        *,
        run_id: str,
        workflow: str,
        upstream: str,
        upstream_ref: str,
        run_url: str,
        created_at: str,
        results: list[RunResultRecord],
        updated_statuses: dict[str, DownstreamStatusRecord],
        report_markdown: str | None = None,
        validate_jobs: list[ValidateJobRecord] | None = None,
    ) -> None:
        from datetime import datetime, timezone

        reported_dt = _parse_dt(created_at)

        # started_at = earliest validate job start for this run.
        started_dt = None
        if validate_jobs:
            starts = [_parse_dt(j.started_at) for j in validate_jobs if j.started_at]
            if starts:
                started_dt = min(starts)

        with self._engine.begin() as conn:
            self._insert_ignore(conn, _sa_run, {
                "run_id": run_id,
                "workflow": workflow,
                "upstream": upstream,
                "upstream_ref": upstream_ref,
                "run_url": run_url,
                "started_at": started_dt,
                "reported_at": reported_dt,
            })

            for r in results:
                self._insert_ignore(conn, _sa_run_result, {
                    "run_id": run_id,
                    "downstream": r.downstream,
                    "upstream": r.upstream,
                    "repo": r.repo,
                    "downstream_commit": r.downstream_commit,
                    "outcome": r.outcome,
                    "episode_state": r.episode_state,
                    "target_commit": r.target_commit,
                    "previous_last_known_good": r.previous_last_known_good,
                    "previous_first_known_bad": r.previous_first_known_bad,
                    "last_known_good": r.last_known_good,
                    "first_known_bad": r.first_known_bad,
                    "current_last_successful": r.current_last_successful,
                    "current_first_failing": r.current_first_failing,
                    "failure_stage": r.failure_stage,
                    "search_mode": r.search_mode,
                    "commit_window_truncated": r.commit_window_truncated,
                    "error": r.error,
                    "head_probe_outcome": r.head_probe_outcome,
                    "head_probe_failure_stage": r.head_probe_failure_stage,
                    "pinned_commit": r.pinned_commit,
                    "age_commits": r.age_commits,
                    "bump_commits": r.bump_commits,
                    "search_base_not_ancestor": r.search_base_not_ancestor,
                    "culprit_log_artifact_url": r.culprit_log_artifact_url,
                })

            for downstream, s in updated_statuses.items():
                self._upsert(
                    conn,
                    _sa_downstream_status,
                    values={
                        "downstream": downstream,
                        "workflow": workflow,
                        "upstream": upstream,
                        "last_known_good": s.last_known_good_commit,
                        "first_known_bad": s.first_known_bad_commit,
                        "pinned_commit": s.pinned_commit,
                        "downstream_commit": s.downstream_commit,
                        "last_good_release": s.last_good_release,
                        "last_good_release_commit": s.last_good_release_commit,
                        "updated_at": reported_dt,
                    },
                    conflict_cols=["downstream", "workflow", "upstream"],
                    update_cols=["last_known_good", "first_known_bad", "pinned_commit", "downstream_commit", "last_good_release", "last_good_release_commit", "updated_at"],
                )

            for j in (validate_jobs or []):
                self._insert_ignore(conn, _sa_validate_job, {
                    "run_id": run_id,
                    "downstream": j.downstream,
                    "job_id": j.job_id,
                    "job_url": j.job_url,
                    "started_at": _parse_dt(j.started_at) if j.started_at else None,
                    "finished_at": _parse_dt(j.finished_at) if j.finished_at else None,
                    "conclusion": j.conclusion,
                })

        # report_markdown is not persisted — regenerate from structured data as needed

    def load_tested_downstream_commits(self, workflow: str) -> set[tuple[str, str]]:
        rr = _sa_run_result
        r = _sa_run
        stmt = (
            sa_select(rr.c.downstream, rr.c.downstream_commit)
            .join(r, rr.c.run_id == r.c.run_id)
            .where(
                r.c.workflow == workflow,
                rr.c.downstream_commit.isnot(None),
                rr.c.outcome.in_(["passed", "failed"]),
            )
            .distinct()
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {(row[0], row[1]) for row in rows}

    def load_prior_results(
        self, workflow: str, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        if not pairs:
            return {}
        rr = _sa_run_result
        r = _sa_run
        vj = _sa_validate_job

        # Build a subquery that ranks results by recency per (downstream, commit),
        # restricted to only the requested pairs.
        pair_values = list(pairs)
        ranked = (
            sa_select(
                rr.c.downstream,
                rr.c.downstream_commit,
                rr.c.outcome,
                rr.c.episode_state,
                rr.c.first_known_bad,
                rr.c.target_commit,
                rr.c.failure_stage,
                rr.c.repo,
                rr.c.run_id,
                r.c.run_url,
                func.row_number()
                .over(
                    partition_by=[rr.c.downstream, rr.c.downstream_commit],
                    order_by=r.c.reported_at.desc(),
                )
                .label("rn"),
            )
            .join(r, rr.c.run_id == r.c.run_id)
            .where(
                r.c.workflow == workflow,
                rr.c.downstream_commit.isnot(None),
                rr.c.outcome.in_(["passed", "failed"]),
                tuple_(rr.c.downstream, rr.c.downstream_commit).in_(pair_values),
            )
            .subquery()
        )

        stmt = (
            sa_select(
                ranked.c.downstream,
                ranked.c.downstream_commit,
                ranked.c.outcome,
                ranked.c.episode_state,
                ranked.c.first_known_bad,
                ranked.c.target_commit,
                ranked.c.failure_stage,
                ranked.c.repo,
                ranked.c.run_url,
                vj.c.job_url,
            )
            .outerjoin(
                vj,
                and_(
                    ranked.c.run_id == vj.c.run_id,
                    ranked.c.downstream == vj.c.downstream,
                ),
            )
            .where(ranked.c.rn == 1)
        )

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()

        result: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row[0], row[1])
            result[key] = {
                "outcome": row[2],
                "episode_state": row[3],
                "first_known_bad": row[4],
                "target_commit": row[5],
                "failure_stage": row[6],
                "repo": row[7],
                "run_url": row[8],
                "job_url": row[9],
            }
        return result

    def load_known_warm_shas(self, upstream: str) -> set[str]:
        t = _sa_cache_warmth
        stmt = sa_select(t.c.sha).where(t.c.upstream == upstream)
        with self._engine.connect() as conn:
            return {row[0] for row in conn.execute(stmt).fetchall()}

    def record_warm_shas(self, upstream: str, shas: Iterable[str]) -> None:
        from datetime import datetime, timezone

        deduped = sorted({sha for sha in shas if sha})
        if not deduped:
            return
        warmed_at = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            for sha in deduped:
                self._upsert(
                    conn,
                    _sa_cache_warmth,
                    values={"upstream": upstream, "sha": sha, "warmed_at": warmed_at},
                    conflict_cols=["upstream", "sha"],
                    update_cols=["warmed_at"],
                )

    def load_manifest_watcher_ledger(
        self, upstream: str
    ) -> dict[str, ManifestWatcherLedgerRow]:
        t = _sa_manifest_watcher_ledger
        stmt = sa_select(
            t.c.downstream, t.c.observed_pin, t.c.dispatched_at, t.c.run_url,
        ).where(t.c.upstream == upstream)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        result: dict[str, ManifestWatcherLedgerRow] = {}
        for row in rows:
            dispatched_at = row[2]
            dispatched_iso = (
                dispatched_at.isoformat().replace("+00:00", "Z")
                if dispatched_at is not None
                else ""
            )
            result[row[0]] = ManifestWatcherLedgerRow(
                downstream=row[0],
                observed_pin=row[1],
                dispatched_at=dispatched_iso,
                run_url=row[3],
            )
        return result

    def upsert_manifest_watcher_ledger(
        self, upstream: str, rows: list[ManifestWatcherLedgerRow]
    ) -> None:
        if not rows:
            return
        with self._engine.begin() as conn:
            for row in rows:
                self._upsert(
                    conn,
                    _sa_manifest_watcher_ledger,
                    values={
                        "downstream": row.downstream,
                        "upstream": upstream,
                        "observed_pin": row.observed_pin,
                        "dispatched_at": _parse_dt(row.dispatched_at),
                        "run_url": row.run_url,
                    },
                    conflict_cols=["downstream", "upstream"],
                    update_cols=["observed_pin", "dispatched_at", "run_url"],
                )


# ---------------------------------------------------------------------------
# Site-generation read-only queries
# ---------------------------------------------------------------------------


def latest_regression_run_id(engine: Any) -> str | None:
    """Return the run_id of the most recent regression run, or None."""
    if not _SA_AVAILABLE:
        raise ImportError("sqlalchemy is required; pip install sqlalchemy")
    stmt = (
        sa_select(_sa_run.c.run_id)
        .where(_sa_run.c.workflow == "regression")
        .order_by(_sa_run.c.reported_at.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).scalar()


def load_latest_run_per_downstream(
    engine: Any, workflow: str, upstream: str
) -> dict[str, LatestRunRecord]:
    """Return the most recent run_result per downstream for (workflow, upstream).

    The underlying query mirrors the "latest per downstream" subquery used by
    ``load_run_for_site`` but is scoped to a single ``(workflow, upstream)``
    pair so that the public runs snapshot can be built without any mixing of
    regression / on-demand state.  ``validate_job`` is left-joined so
    ``job_id`` / ``job_url`` are populated on a best-effort basis.
    """

    if not _SA_AVAILABLE:
        raise ImportError("sqlalchemy is required; pip install sqlalchemy")

    rr = _sa_run_result
    r = _sa_run
    vj = _sa_validate_job

    latest_per_ds = (
        sa_select(
            rr.c.downstream,
            func.max(r.c.reported_at).label("latest_at"),
        )
        .join(r, rr.c.run_id == r.c.run_id)
        .where(r.c.workflow == workflow, r.c.upstream == upstream)
        .group_by(rr.c.downstream)
        .subquery()
    )

    stmt = (
        sa_select(
            rr.c.downstream,
            rr.c.run_id,
            r.c.run_url,
            r.c.reported_at,
            rr.c.target_commit,
            rr.c.downstream_commit,
            rr.c.outcome,
            rr.c.episode_state,
            rr.c.first_known_bad,
            rr.c.last_known_good,
            vj.c.job_id,
            vj.c.job_url,
            rr.c.culprit_log_artifact_url,
        )
        .join(latest_per_ds, rr.c.downstream == latest_per_ds.c.downstream)
        .join(
            r,
            and_(
                rr.c.run_id == r.c.run_id,
                r.c.reported_at == latest_per_ds.c.latest_at,
            ),
        )
        .outerjoin(
            vj,
            and_(
                rr.c.run_id == vj.c.run_id,
                rr.c.downstream == vj.c.downstream,
            ),
        )
    )

    with engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()

    result: dict[str, LatestRunRecord] = {}
    for row in rows:
        reported_at = row[3]
        reported_at_str = (
            reported_at.isoformat().replace("+00:00", "Z")
            if reported_at is not None
            else ""
        )
        result[row[0]] = LatestRunRecord(
            run_id=row[1],
            run_url=row[2],
            reported_at=reported_at_str,
            target_commit=row[4],
            downstream_commit=row[5],
            outcome=row[6],
            episode_state=row[7],
            first_known_bad=row[8],
            last_known_good=row[9],
            job_id=row[10],
            job_url=row[11],
            culprit_log_artifact_url=row[12],
        )
    return result


def load_run_for_site(engine: Any, run_id: str) -> tuple[dict, list[dict]]:
    """Return (run_meta, rows) for the site renderer.

    *run_id* is used only for the banner metadata (title, run URL, upstream
    ref).  The table rows always reflect the **latest result per downstream**
    across all regression runs, so that downstreams absent from the specified
    run (e.g. because the workflow was triggered for a subset of downstreams,
    or a downstream was temporarily disabled) still appear with their most
    recent data and their own CI job link.
    """
    if not _SA_AVAILABLE:
        raise ImportError("sqlalchemy is required; pip install sqlalchemy")

    with engine.connect() as conn:
        run_row = conn.execute(
            sa_select(
                _sa_run.c.run_id,
                _sa_run.c.workflow,
                _sa_run.c.upstream,
                _sa_run.c.upstream_ref,
                _sa_run.c.run_url,
                _sa_run.c.started_at,
                _sa_run.c.reported_at,
            ).where(_sa_run.c.run_id == run_id)
        ).mappings().one()

        # Subquery: the most recent reported_at for each downstream across all
        # regression runs.  Downstreams absent from the banner run still appear
        # here using their own latest run's data.
        latest_per_ds = (
            sa_select(
                _sa_run_result.c.downstream,
                func.max(_sa_run.c.reported_at).label("latest_at"),
            )
            .join(_sa_run, _sa_run_result.c.run_id == _sa_run.c.run_id)
            .where(_sa_run.c.workflow == "regression")
            .group_by(_sa_run_result.c.downstream)
            .subquery()
        )

        ds_tbl = _sa_downstream_status
        upstream_val = dict(run_row)["upstream"]
        results = conn.execute(
            sa_select(
                _sa_run_result.c.downstream,
                _sa_run_result.c.repo,
                _sa_run_result.c.downstream_commit,
                _sa_run_result.c.outcome,
                _sa_run_result.c.episode_state,
                _sa_run_result.c.target_commit,
                _sa_run_result.c.last_known_good,
                _sa_run_result.c.first_known_bad,
                _sa_run_result.c.pinned_commit,
                _sa_run_result.c.age_commits,
                _sa_run_result.c.bump_commits,
                _sa_run_result.c.search_base_not_ancestor,
                _sa_run_result.c.culprit_log_artifact_url,
                _sa_run.c.run_url,
                _sa_validate_job.c.job_url,
                _sa_validate_job.c.started_at.label("job_started_at"),
                _sa_validate_job.c.finished_at.label("job_finished_at"),
                _sa_validate_job.c.conclusion.label("job_conclusion"),
                ds_tbl.c.last_good_release,
                ds_tbl.c.last_good_release_commit,
            )
            .join(latest_per_ds, _sa_run_result.c.downstream == latest_per_ds.c.downstream)
            .join(_sa_run, and_(
                _sa_run_result.c.run_id == _sa_run.c.run_id,
                _sa_run.c.reported_at == latest_per_ds.c.latest_at,
            ))
            .outerjoin(_sa_validate_job, and_(
                _sa_run_result.c.run_id == _sa_validate_job.c.run_id,
                _sa_run_result.c.downstream == _sa_validate_job.c.downstream,
            ))
            .outerjoin(ds_tbl, and_(
                _sa_run_result.c.downstream == ds_tbl.c.downstream,
                ds_tbl.c.workflow == "regression",
                ds_tbl.c.upstream == upstream_val,
            ))
            .order_by(_sa_run_result.c.downstream)
        ).mappings().all()

    rows = [dict(r) for r in results]
    return dict(run_row), rows


# ---------------------------------------------------------------------------
# Dry-run backend
# ---------------------------------------------------------------------------


class DryRunBackend:
    """A no-op backend that logs operations instead of persisting them.

    Reads return empty state (no history).  Writes print a summary of what
    *would* have been persisted, prefixed with ``[dry-run]``.
    """

    def load_all_statuses(self, workflow: str, upstream: str) -> dict[str, DownstreamStatusRecord]:
        print(f"[dry-run] load_all_statuses(workflow={workflow!r}, upstream={upstream!r}) -> {{}}")
        return {}

    def save_run(
        self,
        *,
        run_id: str,
        workflow: str,
        upstream: str,
        upstream_ref: str,
        run_url: str,
        created_at: str,
        results: list[RunResultRecord],
        updated_statuses: dict[str, DownstreamStatusRecord],
        report_markdown: str | None = None,
        validate_jobs: list[ValidateJobRecord] | None = None,
    ) -> None:
        lines = [
            "[dry-run] save_run:",
            f"  run_id:       {run_id}",
            f"  workflow:     {workflow}",
            f"  upstream:     {upstream}",
            f"  upstream_ref: {upstream_ref}",
            f"  run_url:      {run_url}",
            f"  created_at:   {created_at}",
        ]
        for r in results:
            lines.append(f"  result [{r.downstream}]:")
            lines.append(f"    repo:                    {r.repo}")
            lines.append(f"    outcome:                 {r.outcome}")
            lines.append(f"    episode_state:           {r.episode_state}")
            lines.append(f"    target_commit:           {r.target_commit}")
            lines.append(f"    downstream_commit:       {r.downstream_commit}")
            lines.append(f"    pinned_commit:           {r.pinned_commit}")
            lines.append(f"    previous_last_known_good:{r.previous_last_known_good}")
            lines.append(f"    previous_first_known_bad:{r.previous_first_known_bad}")
            lines.append(f"    last_known_good:         {r.last_known_good}")
            lines.append(f"    first_known_bad:         {r.first_known_bad}")
            lines.append(f"    current_last_successful: {r.current_last_successful}")
            lines.append(f"    current_first_failing:   {r.current_first_failing}")
            lines.append(f"    head_probe_outcome:      {r.head_probe_outcome}")
            lines.append(f"    head_probe_failure_stage:{r.head_probe_failure_stage}")
            lines.append(f"    failure_stage:           {r.failure_stage}")
            lines.append(f"    search_mode:             {r.search_mode}")
            lines.append(f"    age_commits:             {r.age_commits}")
            lines.append(f"    bump_commits:            {r.bump_commits}")
            lines.append(f"    commit_window_truncated: {r.commit_window_truncated}")
            lines.append(f"    error:                   {r.error}")
            lines.append(f"    culprit_log_artifact_url:{r.culprit_log_artifact_url}")
        for ds, status in updated_statuses.items():
            lines.append(f"  updated_status [{ds}]:")
            lines.append(f"    last_known_good_commit:  {status.last_known_good_commit}")
            lines.append(f"    first_known_bad_commit:  {status.first_known_bad_commit}")
            lines.append(f"    pinned_commit:           {status.pinned_commit}")
            lines.append(f"    downstream_commit:       {status.downstream_commit}")
        if validate_jobs:
            for job in validate_jobs:
                lines.append(f"  validate_job [{job.downstream}]:")
                lines.append(f"    job_id:     {job.job_id}")
                lines.append(f"    job_url:    {job.job_url}")
                lines.append(f"    started_at: {job.started_at}")
                lines.append(f"    finished_at:{job.finished_at}")
                lines.append(f"    conclusion: {job.conclusion}")
        print("\n".join(lines))

    def load_tested_downstream_commits(self, workflow: str) -> set[tuple[str, str]]:
        print(f"[dry-run] load_tested_downstream_commits({workflow!r}) -> set()")
        return set()

    def load_prior_results(
        self, workflow: str, pairs: set[tuple[str, str]]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        print(f"[dry-run] load_prior_results({workflow!r}, {len(pairs)} pair(s)) -> {{}}")
        return {}

    def load_known_warm_shas(self, upstream: str) -> set[str]:
        print(f"[dry-run] load_known_warm_shas(upstream={upstream!r}) -> set()")
        return set()

    def record_warm_shas(self, upstream: str, shas: Iterable[str]) -> None:
        deduped = sorted({sha for sha in shas if sha})
        print(f"[dry-run] record_warm_shas(upstream={upstream!r}, shas={deduped})")

    def load_manifest_watcher_ledger(
        self, upstream: str
    ) -> dict[str, ManifestWatcherLedgerRow]:
        print(f"[dry-run] load_manifest_watcher_ledger(upstream={upstream!r}) -> {{}}")
        return {}

    def upsert_manifest_watcher_ledger(
        self, upstream: str, rows: list[ManifestWatcherLedgerRow]
    ) -> None:
        lines = [f"[dry-run] upsert_manifest_watcher_ledger(upstream={upstream!r}):"]
        for r in rows:
            lines.append(
                f"  {r.downstream}: observed_pin={r.observed_pin[:12]} "
                f"dispatched_at={r.dispatched_at} run_url={r.run_url}"
            )
        print("\n".join(lines))


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def add_backend_args(parser: Any) -> None:
    """Add ``--backend``, ``--state-root``, and ``--dsn`` to an argument parser."""

    parser.add_argument(
        "--backend", choices=["filesystem", "sql", "dry-run"], default="filesystem",
        help="Storage backend to use.",
    )
    parser.add_argument(
        "--state-root", type=Path, default=None,
        help="State root directory; required when --backend=filesystem.",
    )
    parser.add_argument(
        "--dsn", default=None,
        help="Database connection string; required when --backend=sql.",
    )


def create_backend(
    backend: str,
    *,
    dsn: str | None = None,
    state_root: Path | None = None,
) -> StorageBackend:
    """Create a storage backend from CLI-style parameters.

    Resolves ``--dsn`` from the ``POSTGRES_DSN`` environment variable when not
    provided directly.  Raises ``SystemExit`` on invalid combinations.
    """
    if backend == "dry-run":
        return DryRunBackend()
    if backend == "sql":
        dsn = dsn or os.environ.get("POSTGRES_DSN")
        if not dsn:
            raise SystemExit("--dsn or POSTGRES_DSN environment variable is required when --backend=sql")
        from sqlalchemy import create_engine
        return SqlBackend(create_engine(dsn))
    if not state_root:
        raise SystemExit("--state-root is required when --backend=filesystem")
    return FilesystemBackend(state_root)
