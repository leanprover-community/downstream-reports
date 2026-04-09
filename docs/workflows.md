# Workflow reference

## `mathlib-downstream-report.yml`

This workflow answers the question: *does each tracked downstream
project still build against a given `mathlib4` commit, and if not, which `mathlib4`
commit introduced the breakage?*

**Jobs:**

1. **`plan`** — reads `ci/inventory/downstreams.json` to build a job matrix

2. **`validate`** — for each downstream to validate:

   - *Window selection*:
     clones mathlib and the downstream, then runs a **head probe** — a single
     build attempt of the downstream against the target mathlib commit (with
     `--from` set to its parent so the tool fetches exactly that one commit).
     The probe exit code drives the rest of the step:

     - **Exit 0 (pass):** a passing head-only result is written immediately;
       no bisect is needed.
     - **Exit ≠ 0 and ≠ 1 (error):** a non-bisectable error result is written;
       transient runner problems fall here.
     - **Exit 1 (fail):** window construction is attempted (see below).

     **Lower-bound selection.** The script picks the lower bound of the bisect
     window from two candidates:

     1. *Pinned commit* — the mathlib revision currently recorded in the
        downstream's `lake-manifest.json` (resolved to a full SHA against the
        local mathlib clone). This is the commit the downstream is known to pin
        to, so it is a natural "last known good" starting point.
     2. *Stored last-known-good* — the `last_known_good` field from the
        `downstream_status` table in the database, if present.

     If the stored last-known-good is *strictly newer* than the pinned commit
     (i.e. it is a descendant of it in mathlib history), it is re-verified: the
     script runs a second build attempt of the downstream against that commit.
     If it passes, the stored last-known-good becomes the lower bound (shrinking
     the window); if it fails, the pinned commit is used instead. When no pinned
     commit is available, the stored last-known-good is verified in the same way
     and used if it passes, otherwise no lower bound is available.

     **Window construction.** Given a lower bound, the window is
     `git rev-list --reverse <target> ^<base>` — every commit reachable from
     the target but not from the base, in chronological order. If this list
     exceeds `--max-commits` (default 100 000), it is truncated to
     `max_commits − 1` commits from the oldest end plus the target commit, and
     `truncated = true` is recorded. If the base is absent, equals the target,
     or is not a strict ancestor of it, the window collapses to `[target]`.

     **Bisect vs head-only decision.** A bisect probe is queued
     (`needs_probe = true`, `search_mode = "bisect"`) only when the head probe
     exited with code 1 *and* the window contains more than one commit. The
     `selection.json` artifact records the `probe_from_ref` (parent of the
     oldest window commit) and `probe_to_ref` (target commit) so the probe step
     can invoke `hopscotch` without a local mathlib clone. In all other cases a
     head-only result is written directly.

   - *Probe*: runs `hopscotch` in bisect mode over the
     pre-selected window to find the first bad commit via binary search.

   - Uploads a `result-<name>` artifact containing `result.json` and supporting
     logs.

3. **`report`** — downloads all `result-*` artifacts, updates the
   database, renders a Markdown report appended to the job summary, and
   uploads an `alert-payload` artifact for the alert job.

4. **`alert`** — downloads the alert payload and sends Zulip messages for
   status changes (`NEW_FAILURE` / `RECOVERED`) to the
   `Hopscotch > Downstream alerts` topic. Steady states (`PASSING`,
   `FAILING`, `ERROR`) do not trigger alerts.

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

## `mathlib-downstream-summary.yml`

Manually dispatchable workflow that loads the latest per-downstream state from
the database and sends a compact Markdown table to Zulip.

## Zulip configuration

Both workflows send messages to `mathlib-initiative.zulipchat.com` via the
`downstream-bot` bot. Required GitHub configuration:

| Type | Name | Value |
| --- | --- | --- |
| Variable | `ZULIP_EMAIL` | `downstream-bot@mathlib-initiative.zulipchat.com` |
| Secret | `ZULIP_API_KEY` | Bot API key (from Zulip bot settings) |
