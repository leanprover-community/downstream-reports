# Downstream reports

[`hopscotch`](https://github.com/leanprover-community/hopscotch) is a Lean tool for stepping a downstream project through a list of commits to find the first failing one. This repository contains a GitHub Actions harness that runs it automatically against a curated set of downstream projects and reports the results.

At the moment, this downstream validation is performed for the `leanprover-community/mathlib4` dependency.

## GitHub workflows

### `mathlib-downstream-report.yml`

Runs on a schedule against the latest mathlib commit. For each tracked downstream, it probes the downstream against that commit and — if it fails — bisects the mathlib history to identify the first bad commit. Results are persisted to a database and a summary is appended to the GitHub Actions job summary. Status changes (`NEW_FAILURE` / `RECOVERED`) trigger Zulip alerts.

### `mathlib-downstream-ondemand.yml`

Manually dispatchable. Tests one or more downstreams against the current HEAD of their configured mathlib bumping branch. Deduplicates automatically — a downstream is skipped if its branch HEAD has not changed since the last run. Reports every result to Zulip (compatibility, failure, or skipped), rather than only state-change transitions.

See [`docs/ondemand-workflow.md`](docs/ondemand-workflow.md) for a detailed description, including deduplication logic, stored state, and how it differs from the scheduled regression workflow.

### `mathlib-downstream-summary.yml`

Loads the latest per-downstream state from the database and sends a compact Markdown table to Zulip.

See [`docs/workflows.md`](docs/workflows.md) for a detailed description of the scheduled regression and summary workflows, including job structure, window selection algorithm, and Zulip configuration.

## Inventory

`ci/inventory/downstreams.json` holds the curated set of downstream projects.
Each entry specifies at minimum `name`, `repo`, `default_branch`, and
`dependency_name`. An `enabled: false` field excludes the entry from the
regression workflow.

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
