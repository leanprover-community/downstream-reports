# LKG publication pipeline

This document describes the pipeline that exports the per-downstream
last-known-good (LKG) mathlib commit from the database and publishes it as a
public JSON file that downstream repos can consume to bump their mathlib pin
automatically.

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

Three composite actions are available for downstream repos: `bump-to-latest`,
`open-bump-pr`, and `query-latest`. See [docs/actions.md](actions.md) for full
input/output reference and example workflows.

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

---

## Azure setup

The snapshot is hosted on Azure Blob Storage via the static website endpoint:

- **Storage account:** `downstreamreports`
- **Container:** `$web` (static website)
- **Public URL:** `https://downstreamreports.z13.web.core.windows.net/lkg/latest.json`

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
