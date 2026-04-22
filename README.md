# Downstream reports

[`hopscotch`](https://github.com/leanprover-community/hopscotch) is a Lean tool for stepping a downstream project through a list of commits to find the first failing one. This repository contains a GitHub Actions harness that runs it automatically against a curated set of downstream projects and reports the results.

At the moment, this downstream validation is performed for the `leanprover-community/mathlib4` dependency.

## GitHub workflows

### `mathlib-downstream-report.yml`

Runs on a schedule against the latest mathlib commit. For each tracked downstream, it probes the downstream against that commit and — if it fails — bisects the mathlib history to identify the first bad commit. Results are persisted to a database and a summary is appended to the GitHub Actions job summary. Status changes (`NEW_FAILURE` / `RECOVERED`) trigger Zulip alerts.

### `mathlib-downstream-ondemand.yml`

Manually dispatchable. Tests one or more downstreams against the current HEAD of their configured mathlib bumping branch. Deduplicates automatically — a downstream is skipped if its branch HEAD has not changed since the last run. Reports every result to Zulip (compatibility, failure, or skipped), rather than only state-change transitions.

See [`docs/ondemand-workflow.md`](docs/ondemand-workflow.md) for a detailed description, including deduplication logic, stored state, and how it differs from the scheduled validation workflow.

### `mathlib-downstream-summary.yml`

Loads the latest per-downstream state from the database and sends a compact Markdown table to Zulip.

See [`docs/workflows.md`](docs/workflows.md) for a detailed description of the scheduled validation and summary workflows, including job structure, window selection algorithm, and Zulip configuration.

## Reusable actions and workflows for downstream repos

This repo publishes a reusable workflow and three composite GitHub Actions that
downstream Lean projects can use to consume the LKG data and automate mathlib
bumps:

| Name | Kind | Description |
|------|------|-------------|
| [`bump-dependency-to-latest`](.github/workflows/bump-dependency-to-latest.yml) | Reusable workflow | Zero-boilerplate scheduled bumping — the simplest option. |
| [`bump-to-latest`](.github/actions/bump-to-latest) | Composite action | Looks up the target commit (LKG or FKB), checks the current pin, and runs `hopscotch` to bump and build. |
| [`open-bump-pr`](.github/actions/open-bump-pr) | Composite action | Commits working-tree changes and creates or updates a PR. |
| [`query-latest`](.github/actions/query-latest) | Composite action | Lightweight read-only lookup — returns the target commit for a downstream without cloning or building. |

See [`docs/actions.md`](docs/actions.md) for the full input/output reference and
example workflows.

> **SHA pinning:** the reusable workflow (`bump-dependency-to-latest`) internally
> pins the composite actions to `@main`, so callers cannot override their
> version. If you need to pin to a specific SHA for security or stability, use
> the composite actions directly (Options 2–4 above) and specify the SHA in each
> `uses:` line, e.g. `uses: leanprover-community/downstream-reports/.github/actions/bump-to-latest@<sha>`.

## Keeping your downstream up to date with the public last-known-good (LKG) data

Once your project is [registered](#adding-a-downstream), the LKG data is
updated automatically after every validation run. The LKG (last-known-good) commit is the latest
mathlib commit known to build cleanly against your downstream's **main branch**.

Note that the LKG data reflects the state of your downstream at the time of the
last validation run. If your downstream has changed since then, the recorded LKG
commit may no longer build cleanly against its current state. This is why
`bump-to-latest` re-runs the build rather than blindly applying the recorded commit
— it verifies the bump still works before touching your working tree.

You can consume the LKG data from your own repo in several ways depending on how
much automation you want.

### Option 1 — Reusable workflow (simplest)

The `bump-dependency-to-latest` reusable workflow looks up your downstream's current
LKG commit, runs `hopscotch` to bump and verify the build, and opens (or updates)
a single PR with an auto-generated title and description. If the project is
already at the LKG commit the run is a no-op; if the build fails the run exits
without touching any PR.

**This is the right choice if:**
- You want a scheduled bump PR with no custom logic
- The defaults (one open PR, force-pushed branch, auto-generated message) suit you

**Use a different option if:**
- You need to run extra steps inside the bump job — e.g. post-bump checks, notifications on failure, or custom commit logic (use Option 2)
- You want to push the bump directly to a branch instead of opening a PR (use Option 3)

```yaml
name: Bump mathlib to latest

on:
  schedule:
    - cron: "0 18 * * *"   # run daily; adjust to taste
  workflow_dispatch:

jobs:
  bump:
    uses: leanprover-community/downstream-reports/.github/workflows/bump-dependency-to-latest.yml@main
    permissions:
      contents: write
      pull-requests: write
```

Optional inputs — pass any of these under `with:` if you need to customise:

| Input | Default | Description |
|-------|---------|-------------|
| `branch` | `hopscotch/lkg-bump` | Branch name for the bump PR |
| `base` | repo default branch | Base branch for the PR |
| `labels` | — | Comma-separated labels to apply (e.g. `"dependencies"`) |
| `dependency-name` | `mathlib` | Dependency name in the lakefile |
| `hopscotch-version` | `v1.3.0` | Hopscotch release tag |
| `commit-type` | `last-known-good` | Which commit to bump to: `last-known-good` or `first-known-bad` |

### Option 2 — Composite actions (custom workflow)

Use `bump-to-latest` followed by `open-bump-pr` directly when you need extra steps
— for example, running your own checks between the build and the PR, or
combining the bump with other automation. The action builds against the target
commit, and if successful it opens (or updates) a PR in your repo. The PR is
kept to a single commit and its description is updated on every run.

```yaml
name: Bump mathlib to latest

on:
  schedule:
    - cron: "0 18 * * *"   # run daily; adjust to taste
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6

      - name: Bump to latest
        id: bump
        uses: leanprover-community/downstream-reports/.github/actions/bump-to-latest@main
        with:
          downstream: MyProject   # must match the name in ci/inventory/downstreams.json

      - name: Open or update PR
        if: steps.bump.outputs.updated == 'true'
        uses: leanprover-community/downstream-reports/.github/actions/open-bump-pr@main
        with:
          title:          ${{ steps.bump.outputs.pr-title }}
          message:        ${{ steps.bump.outputs.bump-description }}
          commit-message: ${{ steps.bump.outputs.commit-message }}
```

### Option 3 — Bump and push directly

If you prefer to commit the bump straight to your default branch (no PR), use
`bump-to-latest` alone and then push:

```yaml
      - name: Bump to latest
        id: bump
        uses: leanprover-community/downstream-reports/.github/actions/bump-to-latest@main
        with:
          downstream: MyProject

      - name: Push bump
        if: steps.bump.outputs.updated == 'true'
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add -A
          git commit -m "${{ steps.bump.outputs.commit-message }}"
          git push
```

### Option 4 — Just fetch the target commit

Use `query-latest` to retrieve the current LKG or FKB commit SHA without cloning,
building, or touching your working tree. This is the right starting point for
custom workflows — for example, posting a notification, triggering a separate
CI job, or driving a bespoke update script. Keep in mind that this skips the
verification build, so if your downstream has changed since the LKG data was
last updated, the commit is not guaranteed to still be good.

```yaml
      - name: Get LKG commit
        id: latest
        uses: leanprover-community/downstream-reports/.github/actions/query-latest@main
        # defaults to github.repository — no inputs needed if the repo slug
        # matches the registered downstream's repo field

      - name: Do something with the LKG commit
        run: echo "LKG is ${{ steps.latest.outputs.commit }}"
```

Full input/output documentation is in [`docs/actions.md`](docs/actions.md).

## Inventory

`ci/inventory/downstreams.json` holds the curated set of downstream projects.
Each entry specifies at minimum `name`, `repo`, `default_branch`, and
`dependency_name`. An `enabled: false` field excludes the entry from the
validation workflow.

Do not depend on this file externally to this repository, as the configuration schema might change at any time.

## Adding a downstream

Add an entry to `ci/inventory/downstreams.json`. Required fields:

| Field | Description |
| --- | --- |
| `name` | Unique identifier used in job names, artifact names, and the database. |
| `repo` | GitHub repository in `owner/name` form. |
| `default_branch` | Branch to clone for validation (e.g. `main` or `master`). |
| `dependency_name` | Must match the `name` field of the mathlib `[[require]]` entry in the downstream's `lakefile.toml`. For mathlib dependents this is always `"mathlib"`. |
| `enabled` | Set to `false` to exclude the entry without deleting it. Defaults to `true`. |

Example entry:

```json
{
  "name": "MyProject",
  "repo": "owner/MyProject",
  "default_branch": "main",
  "dependency_name": "mathlib",
  "enabled": true
}
```

On the first run after adding an entry the workflow has no prior episode state
for it. A passing result is recorded as `passing`; a failing result opens
a `new_failure` episode immediately. See [`docs/operations.md`](docs/operations.md)
for how to interpret episode states and manage the database.
