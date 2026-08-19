# Mathlib cache warming

Builds mathlib at the LKG / FKB SHAs reported for every downstream
that does not opt out via `warm_cache: false`, and pushes the
resulting oleans to mathlib's shared Azure cache, so external
consumers of `lkg/latest.json` (e.g. the `bump-to-latest` action) hit
a warm cache instead of having to rebuild mathlib from scratch. The
snapshot reports the warmth this workflow verifies: each published
commit carries a `*_warm` flag. A cold commit is still published, and
a consumer that bumps onto it gets a warning on the PR it opens.

## Why

mathlib's master branch advances via batched bors merges. mathlib's CI
(`mathlib4/.github/workflows/build_template.yml`, `upload_cache` job)
only pushes oleans for the SHAs CI actually built — typically the bors
merge commits, not every commit on master.

The downstream-reports daily regression workflow records, per
downstream, a `last_known_good_commit` (LKG) and `first_known_bad_commit`
(FKB) — usually mathlib master SHAs, but a release-pinned downstream
lands them on a release-tag commit (e.g. `v4.29.1`) that diverged from
master. When those land on commits that didn't go through CI, their
cache is empty. A consumer that
fetches our `lkg/latest.json` and asks for the LKG ends up rebuilding
mathlib from scratch.

This workflow closes that gap by performing the build and push
ourselves for a curated set of downstreams.

## Topology

```
mathlib-downstream-report (on main, success)
    │
    ▼  workflow_run trigger
warm-mathlib-cache.yml         (orchestrator; also: cron every 6h, dispatch)
    │
    ├─ plan job        → reads inventory + DB, consults cache_warmth
    │                    (skip verified-warm, pace failed via backoff),
    │                    emits matrix of unique SHAs to attempt
    │
    ├─ warm-sha matrix → calls _warm-one-sha.yml once per SHA (max-parallel: 1)
    │                     │
    │                     ├─ build_and_stage    self-hosted, NO token
    │                     │   clone target SHA → probe → build → stage
    │                     │
    │                     ├─ upload_cache       ubuntu-latest, has cache token
    │                     │   shallow master → build cache → mint → put-staged
    │                     │
    │                     └─ verify             ubuntu-latest, NO token
    │                         fresh clone → cache get → assert all oleans present
    │
    └─ finalize job    → renders breakdown, upserts every attempted
                         SHA's terminal status into cache_warmth
                         (incrementing its attempt counter)
                  │
                  ▼  workflow_run trigger (success on main)
        publish-lkg.yml + generate-pages.yml
        (refresh lkg/latest.json, runs/latest.json, and the Pages site)
```

**Where the warmth shows up.** In the snapshot, beside each commit:
`export_lkg_snapshot.py` publishes `last_known_good_commit_warm` and
`first_known_bad_commit_warm`, each true only when that SHA's
`cache_warmth` row is verified warm (`already_warm` / `warmed`).

The snapshot reports warmth; it never acts on it. There is no second
"which commit" field to keep in sync, and `last_known_good_commit` /
`first_known_bad_commit` keep meaning exactly the compatibility
boundary — redefining them would break the LKG/FKB adjacency
invariant. A cold SHA is published like any other, and the consumer
decides: `bump-to-latest` bumps to it and puts a warning in the PR
body naming the cost and where to ask for warming. That keeps the
feedback where someone can act on it. A project that repeatedly pays
for a from-source mathlib build has a concrete thing to point at, and
the fix is to enable warming for it rather than to have the snapshot
quietly withhold its target.

Warmth is published per commit rather than per downstream because it
is a fact about a SHA in mathlib's cache, shared by every downstream
that happens to sit on it.

**Why publish-lkg / generate-pages chain off warming, not off the
report directly.** Ordering: a freshly-reported LKG gets its warming
attempt before the snapshot refresh, so in the common case the same
cycle that reported it also publishes it warm. If warming is skipped or
fails outright, the snapshot and the rendered status page do not
refresh — consumers continue to see the previous cycle.

**Eventual consistency.** The cron schedule (`0,6,12,18 UTC`) gives
the chain a recurring entry point so a missed `workflow_run` event,
a cancelled report run, or a transient warm failure can self-heal.
Once `cache_warmth` filters every planned SHA, scheduled ticks
finish in ~30s on `ubuntu-latest`, and their success still chains
into `publish-lkg` + `generate-pages`, which re-read the current
DB state and republish.

## Files

| File | Purpose |
|---|---|
| `.github/workflows/warm-mathlib-cache.yml` | Orchestrator: plan, matrix dispatch, finalize. |
| `.github/workflows/_warm-one-sha.yml` | Reusable per-SHA worker (`workflow_call`). |
| `scripts/plan_cache_warm_jobs.py` | Builds the matrix from inventory + DB or from a manual SHA list. Skips verified-warm SHAs and paces failed SHAs through the backoff retry schedule. |
| `scripts/test_plan_cache_warm_jobs.py` | Unit tests for the planner. |
| `scripts/record_warm_shas.py` | CLI used by the finalize job: reads `summary.json`, upserts `(upstream, sha)` rows into `cache_warmth` with each attempted SHA's terminal status. |
| `scripts/test_record_warm_shas.py` | Unit tests for the warmth-recording filter. |
| `scripts/export_lkg_snapshot.py` | Publishes the `last_known_good_commit_warm` / `first_known_bad_commit_warm` flags beside each commit. |
| `.github/scripts/fetch-latest.sh` | Reads the flag for the requested query type and exposes it as the `cache_warm` output. |
| `.github/actions/bump-to-latest/action.yml` | Turns a `cache_warm=false` target into a step warning and a warning block in `bump-description`. |
| `scripts/models.py` | `DownstreamConfig.warm_cache: bool = True` opt-out flag. |
| `scripts/storage.py` | `cache_warmth` table (`status`, `attempts`, `last_attempt_at`) + `load_cache_warmth` / `record_warmth_results` on the storage backends. |

## Schema migration (status column)

`cache_warmth` carries `status`, `attempts`, and `last_attempt_at`
(formerly just `warmed_at`). Rows written before the status column
existed cannot be classified — membership used to conflate "verified
warm" with "gave up" — so the migration is drop-and-rebuild rather
than backfill:

```sql
DROP TABLE cache_warmth;
-- then re-create via scripts/storage.py create_schema, or:
CREATE TABLE cache_warmth (
  upstream        TEXT NOT NULL,
  sha             TEXT NOT NULL,
  status          TEXT NOT NULL,
  attempts        INTEGER NOT NULL,
  last_attempt_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (upstream, sha)
);
```

This is cheap: the planner only consults rows for SHAs that are
*currently* someone's LKG/FKB, so the live set is small and the next
scheduled tick re-probes it — a genuinely warm SHA takes the
`already_warm` fast path (cache get + `--no-build` check, no build).
Historical rows were dead weight. The same rebuild applies if the
Azure olean container is ever cleared.

## Trigger

- **`workflow_run`** on completion of `mathlib-downstream-report` —
  filtered to `branches: [main]` and `conclusion == 'success'`.
  Production runs only fire from main.
- **`schedule`** at `0,6,12,18 UTC` — eventual-consistency catch-up.
  Most ticks find every planned SHA already recorded in
  `cache_warmth`, emit an empty matrix, and finish in ~30s; they
  still chain into `publish-lkg` and `generate-pages` so a snapshot
  / page refresh that was missed by a previous broken chain
  self-heals within ~6h.
- **`workflow_dispatch`** with optional `shas` input
  (comma-separated 40-char hex SHAs). When `shas` is non-empty the
  inventory + DB *and* the `cache_warmth` filter are bypassed —
  operators forcing a re-warm should not be silently no-op'd.

## Opt-out

`DownstreamConfig.warm_cache: bool = True`. Warming is on by default
so a newly-added downstream's SHAs are warm by the time anything bumps
to them — TauCeti's cold pins in issue #77 came from consuming bumps
without being warmed.

Set `"warm_cache": false` in `ci/inventory/downstreams.json` for
downstreams that don't consume hopscotch bumps: warming them is wasted
compute. Their commits are still published; the snapshot simply reports
them cold. If such a project later starts bumping, its first bump PR
carries the cold-cache warning, which is the signal to flip the flag.

Deduplication by SHA keeps the default's marginal cost low: passing
downstreams share master push tips (which mathlib's own CI already
caches), so the SHAs that actually need building are the
bisect-boundary commits inside bors batches.

## Plan job

`scripts/plan_cache_warm_jobs.py` runs in two modes:

- **Manual** (`--manual-shas a,b,c`): validates each SHA is 40-char
  lowercase hex, dedups, and emits one matrix entry per SHA with
  `tag: "manual"` and `downstreams: []`. Skips DB / inventory and the
  `cache_warmth` filter entirely.
- **DB + inventory** (default): loads enabled inventory entries except
  those with `warm_cache: false`, reads `downstream_status`
  (workflow=`regression`)
  via `SqlBackend.load_all_statuses`, collects every non-null LKG / FKB,
  deduplicates by SHA, classifies each SHA against its `cache_warmth`
  record (via `SqlBackend.load_cache_warmth`), and tags each entry
  `lkg`, `fkb`, or `both` based on the union of roles across
  downstreams.

Classification per SHA:

- no `cache_warmth` row → **include** (never attempted);
- verified warm (`already_warm` / `warmed`) → **skip** as
  `cache_warmth_hit`. Mathlib's olean cache is content-hashed and
  immutable per SHA, so a verified SHA never needs re-probing;
- failed attempt with backoff still pending → **skip** as
  `retry_backoff`;
- failed attempt past its backoff → **include** (retry);
- failed `MAX_WARM_ATTEMPTS` (5) times → **skip** as
  `retry_exhausted`; the finalize summary warns.

The backoff doubles per recorded attempt: 6h (one scheduled tick),
12h, 24h, 48h. Mathlib master always builds, so a failed warming
attempt — `build_failed` included — is infra trouble, not a property
of the SHA; the schedule keeps re-attempting without letting a
flapping failure spin the self-hosted runner every tick. An exhausted
SHA needs operator attention (or a `--manual-shas` backfill, which
bypasses the filter).

Output JSON:

```json
{
  "include": [
    {"sha": "<40-hex>",
     "tag": "lkg|fkb|both|manual",
     "downstreams": ["physlib", "FLT"]}
  ],
  "skipped": [
    {"sha": "<40-hex>",
     "tag": "lkg|fkb|both",
     "downstreams": ["physlib"],
     "status": "cache_warmth_hit|retry_backoff|retry_exhausted",
     "detail": "<human-readable reason>"}
  ]
}
```

The orchestrator's `plan` job reads this, sets `matrix`, `has_jobs`,
`skipped`, and `has_skipped` outputs, and the matrix job is skipped
when the plan is empty.

## Per-SHA chain (`_warm-one-sha.yml`)

Three jobs. Each has its own status; a terminal status uploads
`warm-result-<sha>` directly so the orchestrator's finalize job always has
exactly one final result per SHA. `build_and_stage` and `verify` run
the target SHA's own in-tree cache tool (`lake exe cache`);
`upload_cache` builds its cache tool fresh from a shallow `master`
checkout, mirroring mathlib4's own tools-branch idiom — the cache
binary that sees the bearer token never came off the build runner.

### `build_and_stage`

- `runs-on: [self-hosted, pr]`
- No cache token in scope.

Steps:

1. Install jq (the `pr` runner doesn't ship it).
2. Install elan (no default toolchain — lake reads `lean-toolchain`
   from the checkout).
3. Clone mathlib at the target SHA (`mathlib4/`, full commit graph,
   `--filter=blob:none` for size).
4. **Verify SHA is published** — accepts the SHA if it's reachable
   from `origin/master` (`git merge-base --is-ancestor`) or from a
   published release tag (`git tag --contains`). The job exits 1 with
   a clear error otherwise. Guards against typos in the dispatch input
   while allowing release-pinned downstreams (see "Published-only"
   under Trade-offs).
5. Checkout the SHA.
6. Clone mathlib master shallow into `mathlib4-tools/` — kept solely
   as the leantar-backfill source.
7. **Backfill leantar** into the target toolchain's sysroot when the
   pinned toolchain predates leantar bundling
   (nightly-2026-03-09): the cache tool resolves `leantar` strictly
   from the sysroot, never from PATH.
8. **Probe:** `lake exe cache get` then
   `lake build --no-build -v Mathlib` (both in `mathlib4/`). This
   runs the target SHA's own in-tree cache tool, so completeness is
   measured exactly as a consumer at that SHA sees it. If both
   succeed, status becomes `already_warm` and the chain ends.
9. `lake build Mathlib` (in `mathlib4/`, only runs when the probe
   failed). The cache is content-hashed, so anything `cache get`
   already pulled is reused; only files whose hashes weren't in the
   cache get rebuilt.
10. `lake exe cache stage --staging-dir=../cache-staging`. Staging
    must use the in-tree tool: cache file names embed the tool's
    hash generation (`Cache/IO.lean` `rootHashGeneration`), and only
    the tool at the target SHA names files the way `cache get` at
    that SHA will look them up.
11. Upload `stage-<sha>` artifact (just `.ltar` files — no binary).
12. Write result, upload as `warm-result-<sha>` (terminal:
    `already_warm`/`build_failed`) or `intermediate-<sha>`
    (non-terminal: `staged`).

### `upload_cache`

- `runs-on: ubuntu-latest`
- `needs: build_and_stage`, `if: needs.build_and_stage.outputs.status == 'staged'` —
  skipped entirely on `already_warm` / `build_failed`.
- `environment: cache-warming-token` — binds the OIDC subject so the
  federated credential accepts dispatch from any branch.

Steps:

1. Download `intermediate-<sha>` and `stage-<sha>`.
2. Install elan.
3. Clone mathlib master shallow into `mathlib4-tools/`.
4. `lake build cache` in `mathlib4-tools/` — a fresh, trusted cache
   binary that never came off the build runner.
5. **Mint Azure bearer** via an inline OIDC ↔ Entra exchange (curl +
   jq). We do this manually rather than via mathlib's
   `azure-create-cache-token` action because that action shells out
   to `az`; the inline mint keeps the workflow self-contained.
6. **Push** via `lake env .lake/build/bin/cache put-staged
   --staging-dir=../cache-staging
   --repo=leanprover-community/mathlib4` (run from
   `mathlib4-tools/`). `put-staged` uploads the staged `.ltar` files
   under the names staging gave them and computes no hashes, so the
   master-built tool is safe here across hash generations.
7. **Clear** `MATHLIB_CACHE_AZURE_BEARER_TOKEN` from `$GITHUB_ENV`
   so the result-writing and artifact-upload steps that follow
   don't see it.
8. Write result, upload as `warm-result-<sha>` (terminal:
   `push_failed`) or `pushed-<sha>` (non-terminal: `pushed`).
9. Exit 1 on push failure, surfacing as a red job.

### `verify`

- `runs-on: ubuntu-latest`, no token.
- `if: needs.upload_cache.outputs.status == 'pushed'` — skipped
  entirely otherwise.

Steps:

1. Download `pushed-<sha>`.
2. Install elan.
3. **Fresh** clone of mathlib at the same SHA.
4. `lake exe cache get` then `lake build --no-build --rehash -v
   Mathlib`. Mirrors mathlib's own `post_steps` verification. The
   fresh clone is essential — reusing the build runner's working
   directory would let local oleans satisfy the check even if
   nothing was uploaded.
5. Roll status to `warmed` or `verify_failed`.
6. Upload `warm-result-<sha>`.
7. Exit 1 if the verify lake check failed.

## Status flow

| Status | Where set | Terminal | Surfaces as |
|---|---|---|---|
| `already_warm` | build_and_stage (probe succeeded) | yes | green job |
| `build_failed` | build_and_stage (`lake build Mathlib` failed) | yes | green job, recorded in summary |
| `staged` | build_and_stage (build + stage succeeded) | no — hands off to upload_cache | green job |
| `push_failed` | upload_cache (cache push errored) | yes | red job (allowed failure — run stays green) |
| `pushed` | upload_cache (push succeeded) | no — hands off to verify | green job |
| `warmed` | verify (post-push check passed) | yes | green job |
| `verify_failed` | verify (post-push check failed) | yes | red job (allowed failure — run stays green) |
| `no_result` | finalize (synthesised) | n/a | red finalize job |

"Allowed failure" means the job carries job-level
`continue-on-error: true`: it shows a red X in the run's job list
and emits an `::error::` annotation, but the workflow_run conclusion
stays `success`, so the publish-lkg + generate-pages chain still
fires and a failed SHA never blocks the snapshot refresh that serves
every downstream. (The failed SHA is published with its `*_warm` flag
false, so a consumer that bumps onto it is warned rather than
surprised.)

All three `*_failed` statuses are failures of the warming *attempt*,
not of the SHA: mathlib master always builds, so `build_failed` is
runner trouble (OOM, disk, toolchain download) just as `push_failed`
and `verify_failed` are Azure trouble. The planner re-attempts each of
them on the backoff schedule described under "Plan job".

`no_result` is synthesised by the finalize job for any SHA that was
in the plan but didn't upload a `warm-result-<sha>` artifact —
typically a runner crash or a job timeout that killed the worker
before its terminal upload step ran. It surfaces as a red finalize
job so we don't silently drop those.

## Orchestrator finalize job

The `finalize` job downloads all `warm-result-<sha>` artifacts (each
into its own subdirectory under `results/` — *not* `merge-multiple`,
because every terminal stage uploads under the same in-artifact
filename and merging would silently overwrite earlier rows),
combines them with synthetic `no_result` entries for any planned
SHA that didn't report back, and renders to the run's job summary:

- A total count of SHAs processed.
- A status-breakdown table with one row per non-zero status.
- A per-SHA table with short SHA, tag, status, and the list of
  downstreams that benefit.

The finalize job exits 1 if any SHA reports `no_result`; per-SHA
`push_failed` / `verify_failed` already surfaced as red
allowed-failure jobs in the matrix.

After rendering the summary, the job runs
`scripts/record_warm_shas.py --backend sql ... --summary summary.json`
which upserts `(upstream, sha)` rows into `cache_warmth` for every
entry whose status is terminal (`already_warm`, `warmed`,
`build_failed`, `push_failed`, `verify_failed`), storing the status
verbatim, stamping the attempt time, and incrementing the row's
attempt counter. A verified-warm row is dropped from future plans
indefinitely (the olean cache is content-hashed and immutable per
SHA); a failed row re-enters the plan on the backoff schedule.
`no_result` entries and the planner's own skip statuses are never
recorded — neither represents an attempt that ran. The recording step
has `if: always()` so partial failures still persist the SHAs that
did report.

`record_warm_shas.py` takes the write-capable `POSTGRES_DSN`, so `finalize`
runs in the main-only `publish` GitHub Environment and the whole job is gated
to `main` (`github.ref == 'refs/heads/main'`) — a branch `workflow_dispatch`
backfill still warms and uploads oleans, but `finalize` cleanly skips rather
than failing at the environment gate. **The `publish` environment must stay
branch-policy-only:** adding a required-reviewer or wait-timer rule would stall
this unattended scheduled chain — and the report/on-demand `publish` jobs that
share the environment — waiting on manual approval.

## Throttling

- **Workflow-level concurrency.** `concurrency: warm-mathlib-cache`
  with `cancel-in-progress: false`. At most one warming run executes
  or queues at a time across the repo; if a third dispatch arrives
  while one is running and one queued, the previously-queued one is
  dropped (the latest plan is always the one to honour).
- **Matrix throttle.** `max-parallel: 1` on the orchestrator's
  `warm-sha` matrix caps in-flight per-SHA chains. The
  `build_and_stage` job runs on the shared self-hosted `pr` runner;
  this keeps cache-warming from contending with the regression
  probe job. Bump up later if the daily plan grows large and there's
  headroom on the `pr` runner pool.

Tune up if the warming plan grows large and there's headroom on the
`pr` runner pool.

## Authorization (Azure infra)

The cache push targets the same Azure storage account mathlib's own
CI uses, with a federated credential bound to a GitHub Environment
on this repo. Distinct from the `azure/login`-based path
`publish-lkg.yml` uses (different Azure app, different blob
container). We perform the OIDC ↔ Entra token exchange inline rather
than via mathlib's `azure-create-cache-token` composite action: that
action requires `az` on PATH, which the self-hosted `pr` runner
doesn't have. The exchange itself is the standard OAuth2 flow at
`login.microsoftonline.com`.

### One-time prerequisites

1. **GitHub Environment** named `cache-warming-token` on this repo
   (Settings → Environments). Used purely to scope the OIDC subject
   claim. No protection rules required during testing; can be
   restricted to specific deployment branches once main-only execution
   is desired.
2. **Entra federated credential** on mathlib's cache-writer Azure app
   with subject

   ```
   repo:leanprover-community/downstream-reports:environment:cache-warming-token
   ```

   This binds the credential to the environment, not a branch — same
   pattern as the PR validation workflow's `pr-validation-token`
   environment.
3. Repo secret `MATHLIB_CACHE_WRITER_CLIENT_ID` (the cache-writer
   Azure app's client ID).
4. Repo secret `LPC_AZ_TENANT_ID` (shared tenant ID, already used by
   other mathlib infra).

Until 1–4 are in place, the mint step fails with a clear Azure auth
error.

## Token isolation

The mint step is ordered to run only after `lake build Mathlib` has
completed, so the bearer token is never in the environment of the
elaboration-time code. The `azure-create-cache-token` action writes
`MATHLIB_CACHE_AZURE_BEARER_TOKEN` into `$GITHUB_ENV` (mathlib's CI
relies on that), so we explicitly clear it after the push to keep the
token out of subsequent steps' environments. The verify job runs on a
separate ubuntu runner without the token.

## Testing

### Smoke-test the planner

```bash
source scripts/.venv/bin/activate
python3 scripts/plan_cache_warm_jobs.py \
  --backend dry-run \
  --inventory ci/inventory/downstreams.json \
  --output /tmp/plan.json
cat /tmp/plan.json   # → {"include": [], "skipped": []} off SQL (dry-run reads no statuses)
```

With manual SHAs:

```bash
python3 scripts/plan_cache_warm_jobs.py \
  --backend dry-run \
  --inventory ci/inventory/downstreams.json \
  --manual-shas "<40-hex>,<40-hex>" \
  --output /tmp/plan.json
```

### Dispatch from a feature branch

The orchestrator's name (`warm-mathlib-cache`) must exist on the
default branch for `workflow_dispatch` to be registered. A shim on
main provides that. Once landed:

```bash
gh workflow run warm-mathlib-cache.yml \
  --ref WarmCacheWorkflow \
  -f shas=<40-hex>
```

GitHub locates the workflow via the shim on main but executes the
feature branch's version of both files (`./_warm-one-sha.yml` resolves
at the caller's ref). The federated credential is bound to the
environment, not the branch, so token minting works from any ref.

### End-to-end check

After a successful run, on a *different* machine:

```bash
git clone https://github.com/leanprover-community/mathlib4
cd mathlib4
git checkout <warmed-sha>
lake exe cache get
lake build --no-build -v Mathlib    # should succeed without rebuilds
```

A fresh-machine check catches edge cases (CDN edge serving stale
data) the in-job verify can't.

## Failure modes

All three per-SHA failure statuses are retried on the backoff
schedule; a SHA that exhausts its retry budget surfaces as
`retry_exhausted` in the finalize summary with a `::warning::`
annotation.

- **`build_failed`** — mathlib didn't build at the SHA. Master always
  builds, so this is runner trouble (OOM, disk pressure, toolchain
  download flake). Workflow does not fail.
- **`push_failed`** — mint succeeded but the put-staged call errored.
  Look at the push step's logs and Azure storage account health.
  The upload_cache job goes red (allowed failure); the run stays
  green.
- **`verify_failed`** — push reported success but the post-push
  cache get + `lake build --no-build` showed missing oleans.
  Indicates either the push didn't upload everything, or there's a
  read-after-write consistency issue. The verify job goes red
  (allowed failure); the run stays green.
- **Mint failed** — Azure auth misconfigured. Check the federated
  credential and the `MATHLIB_CACHE_WRITER_CLIENT_ID` secret.

## Trade-offs

- **Three-job split mirrors mathlib4 CI.** Build runs on a self-hosted
  runner with no token; push runs on ubuntu-latest with the bearer;
  verify runs on a fresh ubuntu without the bearer. The cache binary
  the upload job uses is built fresh from a shallow `master`
  checkout, so it's never a binary that came off the build runner.
  This is the same posture as
  `mathlib4/.github/workflows/build_template.yml`'s
  `build` → `upload_cache` → `post_steps` chain.
- **Probe/stage run the target SHA's cache tool.** Cache file names
  embed the tool's hash generation (`Cache/IO.lean`
  `rootHashGeneration`), which moves with mathlib master. Probing and
  staging with the in-tree tool (`lake exe cache`) keeps the warm
  check and the staged names on the exact scheme consumers'
  `lake exe cache get` at that SHA computes. A master-built tool on a
  newer generation reports a warm SHA as cold (wasted rebuild) and
  stages names nobody at that SHA can fetch. The published-SHA check
  makes the in-tree tool source trusted.
- **Published-only.** `build_and_stage` accepts SHAs reachable from
  `origin/master` and SHAs reachable from a published release tag
  (`git tag --contains`); everything else is refused via an explicit
  check. Downstreams that pin a stable mathlib release (e.g.
  `v4.29.1`) land their LKG/FKB on a tag that diverged from master,
  and warming those oleans is exactly what `lake exe cache get` at
  that tag wants — mathlib's own release CI already caches the same
  content-addressed blobs, so this is idempotent rather than
  namespace pollution. Arbitrary branch tips and typo SHAs are still
  refused.
- **Probe is best-effort.** `lake build --no-build -v Mathlib` after
  `cache get` is the canonical way to check completeness. False
  negatives (cache present but probe failed) waste a build but are
  harmless. False positives are not really possible — if lake says
  every olean is present and valid, they are.
