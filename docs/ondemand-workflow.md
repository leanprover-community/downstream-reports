# On-demand workflow reference

## Purpose

`mathlib-downstream-ondemand.yml` answers the question: *is a specific downstream
project compatible with the current HEAD of its mathlib bumping branch?*

Unlike the scheduled regression workflow — which tests every tracked downstream
against a fixed mathlib commit and only alerts on episode state changes — the
on-demand workflow is designed for repeated, targeted compatibility checks against
a moving branch. It reports the result of **every** run to Zulip, and it
**deduplicates** automatically: if the branch HEAD has not changed since the last
run, the downstream is skipped rather than re-tested.

---

## Trigger and inputs

The workflow is manually dispatched via GitHub's `workflow_dispatch` event.

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `downstream` | string | *(empty)* | Single downstream name to test. If empty, all downstreams that have a `bumping_branch` in the inventory are included. |
| `branch` | string | *(empty)* | Explicit branch to test against; requires `downstream`. Overrides the downstream's configured `bumping_branch`. |
| `force` | boolean | `false` | Skip the deduplication check — re-test even if the branch HEAD is unchanged. |
| `quiet` | boolean | `true` | Pass `--quiet` to the hopscotch tool. |
| `dry_run` | boolean | `false` | Use the dry-run backend: no database reads or writes, no Zulip messages. |

---

## Job structure

```
plan ──► select (×N, ubuntu-latest) ──► probe (×N, self-hosted, no secrets) ──► report ──► alert
```

### `plan`

Runs on `ubuntu-latest`. Determines which downstreams to include in the matrix.

1. Loads `ci/inventory/downstreams.json` and filters to enabled entries.
2. If `downstream` is specified, restricts to that single entry; if `branch` is
   also set, overrides that entry's `bumping_branch`.
3. Otherwise keeps every entry that has a `bumping_branch` configured.
4. Resolves each candidate's branch HEAD SHA via the GitHub API.
5. **Deduplication:** queries the storage backend for the set of `(name, head_sha)`
   pairs that were already tested under the `ondemand` workflow. Any pair already
   present is moved to the *skipped* list unless `--force` is set.
6. Emits two JSON files:
   - `matrix.json` — included in the job matrix passed to `select`.
   - `skipped.json` — uploaded as the `skipped-downstreams` artifact; consumed by
     the `report` job so skipped entries appear in the summary and alert payload.

The `plan` job passes `dry_run` as a job output so every subsequent job can select
the right backend without re-evaluating the condition.

### `select`

Runs on `ubuntu-latest` (hosted runner with access to secrets). One job per
downstream in the matrix, up to four in parallel.

Runs `select_ondemand_window.py`, which clones mathlib and the downstream, reads
the database for prior episode state, and computes a candidate bisect window.
**Never invokes hopscotch.** Writes `selection.json` and uploads it as a
`selection-<name>` artifact.

The script also records `downstream_commit` — the HEAD SHA of the downstream
repository itself — so the skip heuristics described in CLAUDE.md can guard
against stale culprit attribution if the downstream has changed between runs.

Prior episode state (`previous_first_known_bad_commit`,
`previous_downstream_commit`, `previous_last_known_good_commit`) and
per-downstream skip flags (`skip_known_bad_bisect`) are embedded in
`selection.json` so the probe step can apply heuristics without a database
connection.

### `probe`

Runs on the self-hosted `pr` runner pool with **no secrets in its `env:` blocks**,
at most two jobs in parallel. Downloads the `selection-<name>` artifact and runs
`probe_downstream_regression_window.py` — the same script used by the regression
workflow.

The probe step runs the HEAD probe and, if it fails, optionally bisects. See
the regression workflow description in `docs/workflows.md` for the full
probe-step logic (LKG verification, skip-known-bad-bisect, culprit re-probe).

Uploads a `result-<name>` artifact containing `selection.json`, `result.json`,
and all tool logs. Also prints a human-readable summary for the job log.

### `report`

Runs on `ubuntu-latest` after all probe jobs complete (including when some
fail, as long as `plan` succeeded and at least one downstream was selected or
skipped).

1. Downloads all `result-*` artifacts into `downloaded-results/`.
2. Downloads the `skipped-downstreams` artifact.
3. Collects probe job URLs via the GitHub API (`job_urls.json`) for linking in
   the report.
4. Runs `aggregate_results.py --workflow ondemand`:
   - For each `result.json`, applies the episode state machine to derive the new
     `EpisodeState` and updated `DownstreamStatusRecord`.
   - Persists all results and updated statuses to the storage backend.
   - Renders a Markdown report and appends it to the GitHub step summary.
   - Writes `alert-payload.json` (see [Alert payload](#alert-payload) below).

### `alert`

Runs after `report` succeeds (or is skipped without cancellation). Downloads
`alert-payload.json` and runs `send_alerts.py --workflow ondemand`.

Unlike the regression workflow (which only alerts on `NEW_FAILURE` and
`RECOVERED`), the on-demand alert job reports **every result**:

| Result type | Message format |
|-------------|----------------|
| `failing` or `new_failure` episode | `format_ondemand_failure_message` |
| `passing` or `recovered` episode | `format_ondemand_compatible_message` |
| Skipped (not re-tested) | `format_ondemand_skipped_message` |
| `error` | Not alerted |

Messages are sent to the `Hopscotch > On demand runs` topic on Zulip.

---

## State stored

The on-demand workflow reads and writes the same storage tables as the regression
workflow, using the same `StorageBackend` protocol. The two workflows are
**fully isolated**: `workflow` is part of the primary key on every relevant table,
and all reads filter by it. Episode state accumulated under `"regression"` is never
visible to `"ondemand"` and vice versa. Each downstream starts with a blank slate
under `"ondemand"` until the on-demand workflow has run at least once.

> **Future optimization:** the regression workflow's `last_known_good_commit` is
> kept up to date by daily runs and would be a reliable lower bound for on-demand
> bisect windows on first use. Bootstrapping from it would shrink the window
> significantly on the first on-demand run. Requires care because the two
> workflows may be testing different target commits, so the LKG from regression is
> not guaranteed to be a strict ancestor of the on-demand target.

### Per-downstream episode state (`downstream_status` table)

Keyed by `(workflow, upstream, downstream_name)`. The on-demand workflow writes
under `workflow="ondemand"`. Fields:

| Field | Description |
|-------|-------------|
| `last_known_good_commit` | Most recent mathlib commit for which the downstream built successfully. Used as the lower bound of future bisect windows. |
| `first_known_bad_commit` | Mathlib commit that first caused a failure in the current open episode. Preserved across subsequent failing runs; only cleared on recovery. |
| `pinned_commit` | The mathlib revision from `lake-manifest.json` at the time of the last run. |
| `downstream_commit` | HEAD SHA of the downstream repository at the time of the last run. Guards against stale culprit attribution when the downstream itself changes. |

### Deduplication cache (`tested_downstream_commits`)

The plan job queries this set (keyed by `workflow="ondemand"`) and the report job
populates it. Each entry is a `(downstream_name, head_sha)` pair where `head_sha`
is the mathlib branch HEAD that was tested. The next plan job uses this set to
skip downstreams whose branch HEAD has not advanced.

### Alert payload

`alert-payload.json`, written by `aggregate_results.py` and consumed by
`send_alerts.py`:

```json
{
  "run_id": "...",
  "run_url": "...",
  "upstream_ref": "ondemand",
  "results": [ /* RunResultRecord per tested downstream */ ],
  "skipped": [ /* skipped downstream metadata from skipped.json */ ]
}
```

The `"skipped"` field is present only in on-demand payloads. Each skipped entry
carries the downstream name, repo, HEAD SHA tested previously, prior outcome and
episode state, and a link to the previous probe job.

---

## Comparison with the regression workflow

| Aspect | `mathlib-downstream-report.yml` | `mathlib-downstream-ondemand.yml` |
|--------|---------------------------------|-----------------------------------|
| **Trigger** | Daily cron + manual dispatch | Manual dispatch only |
| **Job structure** | plan → select → probe → report → alert | plan → select → probe → report → alert |
| **Downstream scope** | All enabled downstreams (or a comma-separated subset) | All downstreams with `bumping_branch`, or a single named one |
| **Target commit** | A fixed mathlib ref (default: `master` HEAD at plan time) | HEAD of each downstream's configured `bumping_branch` |
| **Deduplication** | Never; always re-runs | Automatic: skips if branch HEAD is unchanged (disable with `force`) |
| **Window selection script** | `select_downstream_regression_window.py` | `select_ondemand_window.py` |
| **Probe script** | `probe_downstream_regression_window.py` (shared) | `probe_downstream_regression_window.py` (shared) |
| **DB workflow key** | `"regression"` | `"ondemand"` |
| **Alert scope** | `NEW_FAILURE` and `RECOVERED` transitions only | All results (failure, compatible, skipped) |
| **Alert topic** | `Downstream alerts` | `On demand runs` |
| **dry_run auto-enabled** | Yes, on non-`main` branches | No; opt-in via `dry_run` input |
| **`skipped.json`** | Not produced | Produced by plan job; included in report and alert payload |

The two workflows share the same episode state machine, storage schema, probe
script (`probe_downstream_regression_window.py`), and select/probe security
split. Results from one workflow do **not** affect the episode state seen by
the other (they are stored under separate `workflow` keys).

---

## Inventory requirements

To appear in an on-demand run a downstream entry must have:

- `"enabled": true` (or the field absent, which defaults to `true`)
- `"bumping_branch": "<branch-name>"` — the branch on mathlib to track

Entries without `bumping_branch` are silently excluded unless a specific
`downstream` + `branch` is provided via workflow inputs.

---

## Dry-run mode

When `dry_run=true`:

- `BACKEND` is set to `dry-run` in every job.
- `POSTGRES_DSN` is cleared from the environment (never present when not needed).
- All storage reads return empty state; writes are logged but not persisted.
- Zulip alerts are replaced by log output (`DryRunSender`).

This makes it safe to test workflow changes on non-`main` branches without
polluting the production database or sending spurious Zulip messages. Note: unlike
the regression workflow, dry-run is **not** automatically enabled on non-`main`
branches — it must be explicitly requested.
