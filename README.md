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

## Reusable actions for downstream repos

This repo publishes three composite GitHub Actions that downstream Lean projects
can use to consume the LKG data and automate mathlib bumps:

| Action | Description |
|--------|-------------|
| [`bump-to-lkg`](.github/actions/bump-to-lkg) | Fetches the LKG snapshot, checks the current pin, and runs `hopscotch` to bump and build. |
| [`open-bump-pr`](.github/actions/open-bump-pr) | Commits working-tree changes and creates or updates a PR. |
| [`query-lkg`](.github/actions/query-lkg) | Lightweight read-only lookup — returns the LKG commit for a downstream without cloning or building. |

See [`docs/actions.md`](docs/actions.md) for the full input/output reference and
an example workflow.

## Keeping your downstream up to date with the public last-known-good (LKG) data

Once your project is [registered](#adding-a-downstream), the LKG snapshot is
updated automatically after every validation run. The LKG (last-known-good) commit is the latest
mathlib commit known to build cleanly against your downstream's **main branch**.

Note that the snapshot reflects the state of your downstream at the time of the
last validation run. If your downstream has changed since then, the recorded LKG
commit may no longer build cleanly against its current state. This is why
`bump-to-lkg` re-runs the build rather than blindly applying the snapshot commit
— it verifies the bump still works before touching your working tree.

You can consume the LKG data from your own repo in several ways depending on how
much automation you want.

### Option 1 — Automated PR (recommended)

Use `bump-to-lkg` followed by `open-bump-pr` to fully automate the bump. The
action builds against the LKG commit, and if successful it opens (or updates) a
PR in your repo. The PR is kept to a single commit and its description is
updated on every run, so you always have exactly one open bump PR to review and
merge.

```yaml
name: Bump mathlib to LKG

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
      - uses: actions/checkout@v4

      - name: Bump to LKG
        id: bump
        uses: leanprover-community/hopscotch-reports/.github/actions/bump-to-lkg@main
        with:
          downstream: MyProject   # must match the name in ci/inventory/downstreams.json

      - name: Open or update PR
        if: steps.bump.outputs.updated == 'true'
        uses: leanprover-community/hopscotch-reports/.github/actions/open-bump-pr@main
        with:
          title:          ${{ steps.bump.outputs.pr-title }}
          message:        ${{ steps.bump.outputs.bump-description }}
          commit-message: ${{ steps.bump.outputs.commit-message }}
```

### Option 2 — Bump and push directly

If you prefer to commit the bump straight to your default branch (no PR), use
`bump-to-lkg` alone and then push:

```yaml
      - name: Bump to LKG
        id: bump
        uses: leanprover-community/hopscotch-reports/.github/actions/bump-to-lkg@main
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

### Option 3 — Just fetch the LKG

Use `query-lkg` to retrieve the current LKG commit SHA without cloning,
building, or touching your working tree. This is the right starting point for
custom workflows — for example, posting a notification, triggering a separate
CI job, or driving a bespoke update script. Keep in mind that this skips the
verification build, so if your downstream has changed since the snapshot was
generated, the commit is not guaranteed to still be good.

```yaml
      - name: Get LKG commit
        id: lkg
        uses: leanprover-community/hopscotch-reports/.github/actions/query-lkg@main
        # defaults to github.repository — no inputs needed if the repo slug
        # matches the registered downstream's repo field

      - name: Do something with the LKG commit
        run: echo "LKG is ${{ steps.lkg.outputs.lkg-commit }}"
```

Full input/output documentation for all three actions is in [`docs/actions.md`](docs/actions.md).

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
