# LKG publication pipeline

This document describes the pipeline that exports per-downstream state from
the database and publishes two public JSON files to Azure Blob Storage:

- **`lkg/latest.json`** — stable public API consumed by the bump actions.
  Carries per-downstream LKG / first-known-bad / last-good-release commits.
- **`runs/latest.json`** — per-downstream latest regression-run metadata
  (run URL, job URL, target commit, downstream commit at test time, outcome,
  episode state, timestamps).  Consumed by the `open-incompatibility-issue` action; also
  a building block for future tooling that fetches job logs or pulls the
  per-downstream `result-<name>` artifact via the GitHub API.

Both files are refreshed by the same `publish-lkg` workflow run.

---

## Overview

```
downstream-reports                         Downstream repos
─────────────────                         ────────────────
mathlib-downstream-report workflow        scheduled workflow
  └─ report job writes DB                   └─ bump-to-latest action
                                                ├─ fetches lkg/latest.json
publish-lkg workflow                            ├─ runs hopscotch dep
  └─ export_lkg_snapshot.py                    └─ outputs: updated, skipped, …
       └─ reads downstream_status table
  └─ az storage blob upload               open-bump-pr action
       └─ lkg/latest.json (public read)     └─ git commit + gh pr create/update
```

The snapshot is read from the **full accumulated database state**, not just the
results of the latest run. Publishing can therefore happen at any time and will
always reflect the current picture.

---

## Components

### `scripts/export_lkg_snapshot.py`

Reads `downstream_status` (workflow=`regression`) via the standard
`StorageBackend` interface and serialises enabled downstreams to JSON.

```
python3 scripts/export_lkg_snapshot.py \
  --backend sql \
  --upstream leanprover-community/mathlib4 \
  --inventory ci/inventory/downstreams.json \
  --output /tmp/lkg-snapshot.json
```

The core logic lives in `build_snapshot(backend, inventory, upstream,
source_run)` which is tested independently. `main()` wires up argparse and, for
the SQL backend, calls `latest_regression_run_id()` to attach provenance
metadata.

### `scripts/export_runs_snapshot.py`

Reads `run_result` joined with `run` and `validate_job` (via
`load_latest_run_per_downstream` in `scripts/storage.py`) and serialises the
most recent regression run per downstream to JSON.

```
python3 scripts/export_runs_snapshot.py \
  --backend sql \
  --upstream leanprover-community/mathlib4 \
  --inventory ci/inventory/downstreams.json \
  --output /tmp/runs-snapshot.json
```

Core logic lives in `build_runs_snapshot(latest_runs, inventory, upstream,
source_run)` and is tested independently against an in-memory SQLite engine.
Unlike the LKG snapshot, this script is effectively SQL-only: the filesystem
and dry-run backends do not persist per-run URL / job metadata, so they yield
null run fields for every downstream (same null-coalescing convention as the
LKG snapshot).

### `publish-lkg.yml`

Triggers on successful completion of `mathlib-downstream-report` on `main`, and
also supports `workflow_dispatch` for on-demand publishing.

```
on:
  workflow_dispatch:
  workflow_run:
    workflows: ["mathlib-downstream-report"]
    types: [completed]
    branches: [main]
```

The job condition allows all non-`workflow_run` triggers through, and for
`workflow_run` gates on `head_branch == 'main'` and `conclusion == 'success'`:

```
if: github.event_name != 'workflow_run' ||
    (github.event.workflow_run.head_branch == 'main' &&
     github.event.workflow_run.conclusion == 'success')
``` Authentication to Azure uses OIDC — no
long-lived secrets — via `azure/login@v2` with a federated credential scoped to
`refs/heads/main`.

### Composite actions

Four composite actions are available for downstream repos: `bump-to-latest`,
`open-bump-pr`, `query-latest`, and `open-incompatibility-issue`. See
[docs/actions.md](actions.md) for full input/output reference and example
workflows.

---

## Snapshot schema (v1)

```json
{
  "schema_version": 1,
  "exported_at": "2026-04-15T15:30:00Z",
  "upstream": "leanprover-community/mathlib4",
  "source_run": {
    "run_id": "12345678",
    "run_url": "https://github.com/.../actions/runs/12345678"
  },
  "downstreams": {
    "physlib": {
      "repo": "leanprover-community/physlib",
      "dependency_name": "mathlib",
      "last_known_good_commit": "abc123...",
      "first_known_bad_commit": null,
      "last_good_release": "v4.13.0",
      "last_good_release_commit": "def456..."
    }
  }
}
```

Only enabled downstreams are included. `first_known_bad_commit` is non-null
during an active regression — consumers can use this to skip bumping when the
LKG is inside a known-bad episode.

Schema evolution: new optional fields may be added freely without a version
bump. `schema_version` is incremented only for breaking changes (field removal
or rename).

## Runs snapshot schema (v1)

```json
{
  "schema_version": 1,
  "exported_at": "2026-04-20T06:05:00Z",
  "upstream": "leanprover-community/mathlib4",
  "source_run": {
    "run_id": "12345678",
    "run_url": "https://github.com/.../actions/runs/12345678"
  },
  "downstreams": {
    "physlib": {
      "repo": "leanprover-community/physlib",
      "dependency_name": "mathlib",
      "run_id": "12345678",
      "run_url": "https://github.com/.../actions/runs/12345678",
      "job_id": "987654321",
      "job_url": "https://github.com/.../actions/runs/12345678/job/987654321",
      "result_artifact_name": "result-physlib",
      "reported_at": "2026-04-20T06:00:00Z",
      "target_commit": "abc123...",
      "downstream_commit": "def456...",
      "outcome": "failed",
      "episode_state": "failing",
      "first_known_bad_commit": "abc123...",
      "last_known_good_commit": "0011..."
    }
  }
}
```

The inventory is iterated in full (including disabled entries); downstreams
with no run history yet get a record where every run/commit/job field is
`null`.  `job_id`, `job_url`, and the per-run `downstream_commit` are
best-effort (null when the underlying DB rows are missing).
`result_artifact_name` is always `result-<downstream-name>` — it's a convention
for the `result-<name>` artifact uploaded by the probe job, not a stored
field.

---

## Azure setup

The snapshot is hosted on Azure Blob Storage via the static website endpoint:

- **Storage account:** `downstreamreports`
- **Container:** `$web` (static website)
- **Public URLs:**
  - `https://downstreamreports.z13.web.core.windows.net/lkg/latest.json`
  - `https://downstreamreports.z13.web.core.windows.net/runs/latest.json`

The upload identity is an Entra app registration with a federated credential:

```
issuer:  https://token.actions.githubusercontent.com
subject: repo:leanprover-community/downstream-reports:ref:refs/heads/main
```

Required GitHub configuration:

| Type | Name | Value |
|------|------|-------|
| Secret | `DOWNSTREAM_REPORTS_WRITER_AZ_CLIENT_ID` | App registration `appId` |
| Secret | `AZURE_TENANT_ID` | Entra tenant ID |
| Variable | `AZURE_STORAGE_ACCOUNT` | `downstreamreports` |
| Variable | `AZURE_STORAGE_CONTAINER` | `$web` |

No subscription ID is needed — `azure/login` is called with
`allow-no-subscriptions: true` since the role is scoped to a single storage
container and no subscription-level operations are performed.

---

## Downstream integration

See [docs/actions.md](actions.md) for the full input/output reference and a
complete example workflow.

The `downstream` input to `bump-to-latest` must match the `name` field of the
entry in `ci/inventory/downstreams.json`.
