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

``load_bumping_seen() / save_bumping_seen(updates)``
    Track which bumping-branch commit was last processed per downstream, so
    the bumping workflow can skip re-testing the same HEAD.

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
from typing import Any, Protocol, runtime_checkable


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


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Read/write contract for downstream-regression state.

    All methods use domain types (``DownstreamStatusRecord``,
    ``RunResultRecord``) rather than raw dicts or filesystem keys.  The two
    supported workflow identifiers are ``"regression"`` (the main head-tracking
    run) and ``"bumping"`` (the downstream bumping-branch run).
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

    def load_bumping_seen(self) -> dict[str, str]:
        """Return ``{downstream: last_seen_branch_commit}`` for all downstreams."""
        ...

    def save_bumping_seen(self, updates: dict[str, str]) -> None:
        """Advance ``last_seen_branch_commit`` for the given downstreams.

        Only the downstreams present in *updates* are modified; others are
        left unchanged.
        """
        ...


# ---------------------------------------------------------------------------
# Filesystem implementation
# ---------------------------------------------------------------------------

_WORKFLOW_STATUS_KEY: dict[str, str] = {
    "regression": "current",
    "bumping": "bumping-current",
}
_WORKFLOW_REPORT_KEY: dict[str, str] = {
    "regression": "latest",
    "bumping": "bumping-latest",
}
_WORKFLOW_HISTORY_PREFIX: dict[str, str] = {
    "regression": "",
    "bumping": "bumping",
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
            current.json          regression episode state
            bumping-current.json  bumping episode state
            bumping-seen.json
        reports/
            latest.json / latest.md
            bumping-latest.json / bumping-latest.md
        results/
            {day}/{run_id}/{downstream}.json
            bumping/{day}/{run_id}/{downstream}.json

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

    def load_bumping_seen(self) -> dict[str, str]:
        path = self._root / "status" / "bumping-seen.json"
        if not path.exists():
            return {}
        payload = json.loads(path.read_text())
        return {
            name: data["last_seen_branch_commit"]
            for name, data in payload.get("downstreams", {}).items()
            if "last_seen_branch_commit" in data
        }

    def save_bumping_seen(self, updates: dict[str, str]) -> None:
        path = self._root / "status" / "bumping-seen.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            payload = json.loads(path.read_text())
        else:
            payload = {"schema_version": 1, "downstreams": {}}
        for downstream, commit in updates.items():
            payload.setdefault("downstreams", {})[downstream] = {
                "last_seen_branch_commit": commit
            }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
    )

    _sa_bumping_seen = Table(
        "bumping_seen",
        _sa_metadata,
        Column("downstream", String, primary_key=True),
        Column("last_seen_branch_commit", String, nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
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
        ).where(t.c.workflow == workflow, t.c.upstream == upstream)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {
            row[0]: DownstreamStatusRecord(
                last_known_good_commit=row[1],
                first_known_bad_commit=row[2],
                pinned_commit=row[3],
                downstream_commit=row[4],
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
                        "updated_at": reported_dt,
                    },
                    conflict_cols=["downstream", "workflow", "upstream"],
                    update_cols=["last_known_good", "first_known_bad", "pinned_commit", "downstream_commit", "updated_at"],
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

    def load_bumping_seen(self) -> dict[str, str]:
        t = _sa_bumping_seen
        stmt = sa_select(t.c.downstream, t.c.last_seen_branch_commit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return {row[0]: row[1] for row in rows}

    def save_bumping_seen(self, updates: dict[str, str]) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            for downstream, commit in updates.items():
                self._upsert(
                    conn,
                    _sa_bumping_seen,
                    values={
                        "downstream": downstream,
                        "last_seen_branch_commit": commit,
                        "updated_at": now,
                    },
                    conflict_cols=["downstream"],
                    update_cols=["last_seen_branch_commit", "updated_at"],
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
                _sa_run.c.run_url,
                _sa_validate_job.c.job_url,
                _sa_validate_job.c.started_at.label("job_started_at"),
                _sa_validate_job.c.finished_at.label("job_finished_at"),
                _sa_validate_job.c.conclusion.label("job_conclusion"),
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

    def load_bumping_seen(self) -> dict[str, str]:
        print("[dry-run] load_bumping_seen() -> {}")
        return {}

    def save_bumping_seen(self, updates: dict[str, str]) -> None:
        print(f"[dry-run] save_bumping_seen(updates={updates!r})")


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
