# Storage backends

The Python scripts that run the downstream regression workflow persist two kinds
of state:

- **Episode state** — for each downstream, whether it is currently passing or
  failing, and which Mathlib commits form the boundary of the failure episode.
- **Run history** — the full per-downstream results of every workflow run,
  including any bisect window that was probed.

The `StorageBackend` protocol in `scripts/storage.py` abstracts all reads and
writes behind a typed interface so the underlying store can be swapped without
touching business logic.


## The protocol

`StorageBackend` is a Python `typing.Protocol` — any class that implements the
right methods satisfies it structurally, with no inheritance required.

```python
class StorageBackend(Protocol):
    def load_all_statuses(self, workflow: str) -> dict[str, DownstreamStatusRecord]: ...
    def save_run(
        self,
        *,
        run_id: str,
        workflow: str,
        mathlib_ref: str,
        run_url: str,
        created_at: str,
        results: list[RunResultRecord],
        updated_statuses: dict[str, DownstreamStatusRecord],
        report_markdown: str | None = None,
        validate_jobs: list[ValidateJobRecord] | None = None,
    ) -> None: ...
```

All methods use **domain types**, not raw dicts or file paths:

| Type | What it represents | Relational analogue |
|---|---|---|
| `DownstreamStatusRecord` | Current episode state for one downstream | Row in `downstream_status` |
| `RunResultRecord` | Full result for one downstream in one run | Row in `run_result` |
| `ValidateJobRecord` | CI job metadata for one downstream's validate step | Row in `validate_job` |

### Methods

**`load_all_statuses(workflow)`** — returns the current episode state for every
downstream tracked by *workflow* (`"regression"`). Returns an empty dict when no
state has been stored yet.

**`save_run(...)`** — atomically persists everything about a completed run: the
run metadata, all per-downstream results, the updated episode state, and an
optional pre-rendered markdown report. The SQL backend wraps all writes in a
single transaction.


## SQL backend

`SqlBackend(engine)` is the production implementation used by the GitHub Actions
workflows. It requires SQLAlchemy and a PostgreSQL database.

The workflow scripts select it with `--backend sql` and read the connection
string from the `POSTGRES_DSN` environment variable. No `--dsn` flag is needed
in workflow invocations.

Provision the schema once with:

```python
from sqlalchemy import create_engine
from scripts.storage import create_schema

create_schema(create_engine("postgresql://user:pass@host/dbname"))
```

`create_schema` is idempotent — safe to re-run on a database that is already
provisioned.

### Schema

| Table | One row per | Key columns |
| --- | --- | --- |
| `run` | workflow run | `run_id`, `workflow`, `mathlib_ref`, `run_url`, `started_at`, `reported_at` |
| `run_result` | downstream × run | `outcome`, `episode_state`, mathlib target / LKG / FKB commits, `failure_stage`, `search_mode`, head-probe fields, tool `summary` |
| `downstream_status` | downstream × workflow | `last_known_good`, `first_known_bad` — current episode state, upserted after every run |
| `validate_job` | downstream × run | CI job timing and conclusion for the validate step |

```sql
-- Current episode state per downstream.
-- Upserted on every save_run call.
CREATE TABLE downstream_status (
    downstream      TEXT        NOT NULL,
    workflow        TEXT        NOT NULL,
    last_known_good TEXT,                  -- Mathlib commit SHA, nullable
    first_known_bad TEXT,                  -- Mathlib commit SHA, nullable
    updated_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (downstream, workflow)
);

-- One row per workflow run.
CREATE TABLE run (
    run_id      TEXT        PRIMARY KEY,
    workflow    TEXT        NOT NULL,
    mathlib_ref TEXT        NOT NULL,
    run_url     TEXT        NOT NULL,
    started_at  TIMESTAMPTZ,
    reported_at TIMESTAMPTZ NOT NULL
);

-- One row per downstream per run.  Maps directly to RunResultRecord.
CREATE TABLE run_result (
    run_id                   TEXT    NOT NULL REFERENCES run(run_id),
    downstream               TEXT    NOT NULL,
    repo                     TEXT    NOT NULL,
    downstream_commit        TEXT,
    outcome                  TEXT    NOT NULL,  -- 'passed' | 'failed' | 'error'
    episode_state            TEXT    NOT NULL,
    mathlib_target_commit    TEXT,
    previous_last_known_good TEXT,
    previous_first_known_bad TEXT,
    last_known_good          TEXT,
    first_known_bad          TEXT,
    current_last_successful  TEXT,
    current_first_failing    TEXT,
    failure_stage            TEXT,
    search_mode              TEXT    NOT NULL,
    commit_window_truncated  BOOLEAN NOT NULL,
    error                    TEXT,
    summary                  TEXT    NOT NULL,
    head_probe_outcome       TEXT,
    head_probe_failure_stage TEXT,
    head_probe_summary       TEXT,
    PRIMARY KEY (run_id, downstream)
);

-- CI job metadata for each downstream's validate step.
CREATE TABLE validate_job (
    run_id      TEXT        NOT NULL REFERENCES run(run_id),
    downstream  TEXT        NOT NULL,
    job_id      TEXT        NOT NULL,
    job_url     TEXT        NOT NULL,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    conclusion  TEXT,
    PRIMARY KEY (run_id, downstream)
);
```

### Table relationships

```
run (1) ──< run_result (N)
         └< validate_job (N)

downstream_status  (upserted per save_run, keyed by downstream × workflow)
```


## Filesystem backend

`FilesystemBackend(state_root: Path)` maps the protocol methods onto a local
directory tree. It is used for local development and testing where no database
is available — for example, `scripts/local_downstream_sandbox.py` uses it with
`--backend filesystem --state-root <path>`.

### Directory layout

```
{state_root}/
  status/
    current.json          regression episode state
  reports/
    latest.json           latest regression run (structured)
    latest.md             latest regression run (rendered markdown)
  results/
    {YYYY-MM-DD}/{run_id}/{downstream}.json   append-only run history
```

### Method behaviour

**`load_all_statuses(workflow)`** reads `status/current.json` and constructs one
`DownstreamStatusRecord` per entry in the `downstreams` object.

**`save_run(...)`** writes three things:

1. `status/current.json` — full snapshot of the updated episode state for all
   downstreams.
2. `reports/latest.json` and optionally `reports/latest.md` — the latest-run
   report. Each call overwrites the previous one.
3. `results/{day}/{run_id}/{downstream}.json` — one file per downstream,
   append-only run history. These files are never overwritten.

The helper `result_to_row(r: RunResultRecord) -> dict` serialises a
`RunResultRecord` to the flat dict shape used in both JSON files and the
markdown report. It is the only place the domain types touch the on-disk format.
