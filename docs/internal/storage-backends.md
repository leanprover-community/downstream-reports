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
provisioned. It creates *missing tables* but does **not** add columns to tables
that already exist (`create_all` only emits `CREATE TABLE … IF NOT EXISTS`). New
columns on an existing production table need a manual `ALTER`. For
`run_result.proposed_fixes`:

```sql
ALTER TABLE run_result ADD COLUMN IF NOT EXISTS proposed_fixes TEXT NOT NULL DEFAULT '[]';
```

The `DEFAULT '[]'` mirrors the SQLAlchemy `server_default`, so the backfill is
free and existing rows read back as empty lists.

### Schema

| Table | One row per | Key columns |
| --- | --- | --- |
| `run` | workflow run | `run_id`, `workflow`, `mathlib_ref`, `run_url`, `started_at`, `reported_at` |
| `run_result` | downstream × run | `outcome`, `episode_state`, mathlib target / LKG / FKB commits, `failure_stage`, `search_mode`, head-probe fields, tool `summary`, hopscotch boundary fixes (`proposed_fixes`) |
| `downstream_status` | downstream × workflow | `last_known_good`, `first_known_bad` — current episode state, upserted after every run |
| `validate_job` | downstream × run | CI job timing and conclusion for the validate step |
| `cache_warmth` | upstream × SHA | `warmed_at` — set when a SHA's olean cache has been confirmed warm; consulted by `plan_cache_warm_jobs.py` to skip already-warm SHAs |

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
    -- Hopscotch boundary fixes (results.json `proposedFixes`, schema v3+),
    -- stored as JSON text — hopscotch's verbatim ProposedFix objects.
    proposed_fixes           TEXT    NOT NULL DEFAULT '[]',
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

-- Set of upstream SHAs whose olean cache has been confirmed warm by the
-- cache-warming workflow.  Mathlib's cache is content-hashed and immutable
-- per SHA, so once a row lands here it is never invalidated automatically;
-- truncate by hand if the upstream Azure container is ever cleared.
-- Populated by record_warm_shas.py at the end of warm-mathlib-cache.yml's
-- summary job; consulted by plan_cache_warm_jobs.build_matrix_from_db so
-- scheduled warm ticks find an empty matrix in steady state.
CREATE TABLE cache_warmth (
    upstream  TEXT        NOT NULL,
    sha       TEXT        NOT NULL,
    warmed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (upstream, sha)
);
```

### Table relationships

```
run (1) ──< run_result (N)
         └< validate_job (N)

downstream_status  (upserted per save_run, keyed by downstream × workflow)
cache_warmth       (upserted per warm run, keyed by upstream × sha)
```


## Dry-run backend

`DryRunBackend()` (`--backend dry-run`) is a no-op backend for debugging:
reads return empty state and writes print a `[dry-run]`-prefixed summary of
what would have been persisted. The workflows select it when the `dry_run`
input is set so feature-branch test runs never touch the production database.

For local development against real state, point the SQL backend at a SQLite
file instead: `--backend sql --dsn sqlite:///state.db` (provision it once with
`create_schema`, as above).

## Status-snapshot file

The select matrix fan-outs (~30 legs on one cron tick) never instantiate a
backend at all: a simultaneous connection burst is exactly what provokes the
Neon pooler's cold-start timeouts, and keeping credentials out of the fan-out
shrinks the secret surface. Instead, the plan job runs
`export_status_snapshot.py`, which performs the one `load_all_statuses` read
and writes the result as a single JSON file
(`storage.write_status_snapshot`); the workflow uploads it as the
`status-snapshot` artifact and each select leg reads it back with
`--status-snapshot <file>` (`storage.read_status_snapshot`).

The payload embeds its provenance and the reader validates it — a missing
file, an unexpected `schema_version`, or a snapshot staged for a different
`(workflow, upstream)` fails the leg loudly rather than silently feeding the
skip heuristics empty prior state:

```json
{
  "schema_version": 3,
  "workflow": "regression",
  "upstream": "leanprover-community/mathlib4",
  "reported_at": "2026-06-11T00:00:00Z",
  "downstreams": {
    "physlib": {
      "last_known_good_commit": "…",
      "first_known_bad_commit": null,
      "pinned_commit": "…",
      "downstream_commit": "…",
      "last_good_release": "v4.13.0",
      "last_good_release_commit": "…"
    }
  }
}
```

Omitting `--status-snapshot` runs a select script with no prior state (every
downstream is treated as first-run), which is the convenient mode for local
invocations.

The helper `result_to_row(r: RunResultRecord) -> dict` serialises a
`RunResultRecord` to the flat dict shape used by the markdown report
renderer.
