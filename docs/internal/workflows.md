# Workflow reference

## `mathlib-downstream-report.yml`

This workflow answers the question: *does each tracked downstream
project still build against a given `mathlib4` commit, and if not, which `mathlib4`
commit introduced the breakage?*

**Jobs:**

1. **`plan`** — reads `ci/inventory/downstreams.json` to build a job matrix.

2. **`select`** (per downstream, `ubuntu-latest`, **has secrets**) — runs
   `select_downstream_regression_window.py`. Clones mathlib and the downstream,
   reads the database for prior episode state, computes a candidate bisect
   window, and writes `selection.json`. **Never invokes hopscotch.**

   The select step may short-circuit via `try_skip_already_good`: if the
   target commit and downstream HEAD are identical to the last verified-passing
   run, a synthetic passing result is embedded directly in `selection.json` as
   `pre_resolved_result` and the probe step writes it without running hopscotch.

   **Lower-bound selection.** The select step picks a conservative lower bound
   for the bisect window from the *pinned commit* (the mathlib revision in
   `lake-manifest.json`). The stored last-known-good commit is also forwarded
   to the probe step, which performs LKG verification (see below) if a bisect
   turns out to be needed.

   **Window construction.** Given a lower bound, the window is
   `git rev-list --reverse <target> ^<base>` — every commit reachable from
   the target but not from the base, in chronological order. If this list
   exceeds `--max-commits` (default 100 000), it is truncated to
   `max_commits − 1` commits from the oldest end plus the target commit, and
   `truncated = true` is recorded. If the base is absent, equals the target,
   or is not a strict ancestor of it, the window collapses to `[target]`.

   Uploads a `selection-<name>` artifact.

3. **`probe`** (per downstream, self-hosted, **no secrets**) — runs
   `probe_downstream_regression_window.py`. Downloads `selection-<name>` and:

   - If `pre_resolved_result` is set, writes it as `result.json` and exits.
   - Otherwise runs a **HEAD probe** — a single hopscotch build of the
     downstream against the target commit.
   - If the HEAD probe passes or errors, writes a head-only `result.json`.
   - If the HEAD probe fails (exit code 1):
     1. Tries `try_skip_known_bad_bisect` — if the stored first-known-bad
        is an ancestor of the target and the downstream is unchanged, skips
        the bisect and runs a `run_culprit_probe` instead to capture fresh
        failure logs.
     2. Otherwise attempts **LKG verification**: if a stored last-known-good
        is newer than the pinned commit, runs hopscotch against it. If it
        passes, re-derives the bisect window with the wider lower bound. If
        it fails, falls back to the pinned-commit-based window.
     3. Runs `hopscotch` in bisect mode over the final window.

   All hopscotch invocations go through `cache_env()`, which strips CI secrets
   as a defence-in-depth measure. Uploads a `result-<name>` artifact.

4. **`report`** — downloads all `result-*` artifacts, updates the
   database, renders a Markdown report appended to the job summary, and
   uploads an `alert-payload` artifact for the alert job.

5. **`alert`** — downloads the alert payload and sends Zulip messages for
   status changes (`NEW_FAILURE` / `RECOVERED`) to the
   `Hopscotch > Downstream alerts` topic. Steady states (`PASSING`,
   `FAILING`, `ERROR`) do not trigger alerts.

**Security invariant:** any job that runs hopscotch has no secrets in its
`env:` blocks. The `probe` job runs on ephemeral self-hosted runners; the
`select` job holds `GITHUB_TOKEN` and `POSTGRES_DSN` but never invokes
hopscotch or lake.

**Job summary report.** The aggregation script renders a Markdown report that is
appended directly to the GitHub Actions job summary. It contains:

- A **summary table** with one row per downstream showing the outcome
  (`passed` / `failed` / `error`), the episode-level state transition
  (`PASSING`, `NEW_FAILURE`, `FAILING`, `RECOVERED`), the target
  mathlib commit, the last known-good commit, the first known-bad commit, and
  brief notes (failure stage, search mode).
- A collapsible **`<details>` block per downstream** with the search mode
  (head-only or bisect), the head-probe result, the bisect window bounds and
  position, the current episode state, a filtered snippet of the culprit build
  log (capped at 200 lines / 40 KB), and the full tool summary.

## `mathlib-downstream-ondemand.yml`

See [`docs/ondemand-workflow.md`](ondemand-workflow.md) for a full description.
This workflow is manually triggered to test one or more downstreams against the
current HEAD of their configured mathlib bumping branch. It deduplicates runs
automatically (skips if the branch HEAD is unchanged) and reports every result
— compatibility, failure, or skipped — to the `Hopscotch > On demand runs`
Zulip topic.

## `mathlib-downstream-summary.yml`

Manually dispatchable workflow that loads the latest per-downstream state from
the database and sends a compact Markdown table to Zulip.

## `warm-mathlib-cache.yml`

See [`docs/internal/cache-warming.md`](cache-warming.md) for a full description.

After each successful regression report on main, this workflow builds mathlib
at the LKG / FKB SHAs reported for opted-in downstreams and pushes the oleans
to mathlib's shared Azure cache. External consumers of `lkg/latest.json`
(e.g. the `bump-to-latest` action) hit a warm cache instead of having to
rebuild mathlib from scratch when our reported SHAs land between bors merges.

Per-downstream opt-in via `DownstreamConfig.warm_cache` in the inventory.
`workflow_dispatch` accepts an optional comma-separated `shas` input that
bypasses the inventory + DB lookup, useful for one-off backfills and for
testing on a feature branch. The build_stage_push job refuses SHAs that
aren't reachable from `origin/master`.

## Zulip configuration

Both workflows send messages to `mathlib-initiative.zulipchat.com` via the
`downstream-bot` bot. Required GitHub configuration:

| Type | Name | Value |
| --- | --- | --- |
| Variable | `ZULIP_EMAIL` | `downstream-bot@mathlib-initiative.zulipchat.com` |
| Secret | `ZULIP_API_KEY` | Bot API key (from Zulip bot settings) |
