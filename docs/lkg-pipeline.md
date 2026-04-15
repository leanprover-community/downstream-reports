# LKG publication pipeline

This document describes the pipeline that exports the per-downstream
last-known-good (LKG) mathlib commit from the database and publishes it as a
public JSON file that downstream repos can consume to bump their mathlib pin
automatically.

---

## Overview

```
hopscotch-reports                         Downstream repos
─────────────────                         ────────────────
mathlib-downstream-report workflow        scheduled workflow
  └─ report job writes DB                   └─ bump-to-lkg action
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

The job condition gates on `head_branch == 'main'` (for `workflow_run`) or
`event_name == 'workflow_dispatch'`. Authentication to Azure uses OIDC — no
long-lived secrets — via `azure/login@v2` with a federated credential scoped to
`refs/heads/main`.

### `.github/actions/bump-to-lkg`

Composite action for downstream repos. Given a `downstream` name (must match the
inventory entry), it:

1. Fetches and validates `lkg/latest.json` from the public blob URL
2. Reads the current pin from `lake-manifest.json`
3. Skips if already at LKG (`skipped=true`)
4. Installs elan + hopscotch, runs `hopscotch dep --scan-mode linear --keep-last-good`
5. Resolves outputs with safe defaults

**Inputs:**

| Input | Default | Description |
|-------|---------|-------------|
| `downstream` | *(required)* | Name as registered in the inventory |
| `project-dir` | `.` | Path to the downstream project root |
| `dependency-name` | `mathlib` | Dependency name in the lakefile |
| `hopscotch-version` | `v1.3.0` | Hopscotch release tag |
| `snapshot-url` | production URL | Override for testing |

**Outputs:** `lkg-commit`, `current-pin`, `updated`, `skipped`, `build-failed`

### `.github/actions/open-bump-pr`

Generic commit-and-PR composite action. Independent of `bump-to-lkg` — works
with any working-tree changes. LKG-aware inputs (`lkg-commit`, `previous-pin`,
`source-run-url`) enrich the default PR title and body when provided.

**Outputs:** `pr-number`, `pr-url`, `action` (`"created"` / `"updated"` / `"noop"`)

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
      "first_known_bad_commit": null
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
subject: repo:leanprover-community/hopscotch-reports:ref:refs/heads/main
```

Required GitHub configuration:

| Type | Name | Value |
|------|------|-------|
| Secret | `AZURE_CLIENT_ID` | App registration `appId` |
| Secret | `AZURE_TENANT_ID` | Entra tenant ID |
| Secret | `AZURE_SUBSCRIPTION_ID` | Azure subscription ID |
| Variable | `AZURE_STORAGE_ACCOUNT` | `downstreamreports` |
| Variable | `AZURE_STORAGE_CONTAINER` | `$web` |

---

## Downstream integration

Minimal workflow for a downstream repo:

```yaml
name: Bump mathlib to LKG

on:
  schedule:
    - cron: "0 18 * * *"
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6

      - name: Bump to LKG
        id: bump
        uses: leanprover-community/hopscotch-reports/.github/actions/bump-to-lkg@v1
        with:
          downstream: my-project-name

      - name: Open PR
        if: steps.bump.outputs.updated == 'true'
        uses: leanprover-community/hopscotch-reports/.github/actions/open-bump-pr@v1
        with:
          lkg-commit: ${{ steps.bump.outputs.lkg-commit }}
          previous-pin: ${{ steps.bump.outputs.current-pin }}
```

The `downstream` input must match the `name` field of the entry in
`ci/inventory/downstreams.json`.
