# Mathlib cache warming

Builds mathlib at the LKG / FKB SHAs reported for every downstream
that does not opt out via `warm_cache: false`, and pushes the
resulting oleans to the R2 bucket behind `cache.mathlib.org`: the
store mathlib's own CI writes and every consumer reads.
External consumers of `lkg/latest.json` (e.g. the `bump-to-latest`
action) then hit a warm cache instead of rebuilding mathlib from
scratch. The snapshot reports the warmth this workflow verifies: each
published commit carries a `*_warm` flag. A cold commit is still
published, and a consumer that bumps onto it gets a warning on the PR it
opens.

## Why

mathlib's master branch advances through batched bors merges. mathlib's
CI (`mathlib4/.github/workflows/build_template.yml`, `upload_cache` job)
pushes oleans for the SHAs it built: the bors merge commits, not every
commit on master.

The regression workflow records a `last_known_good_commit` (LKG) and a
`first_known_bad_commit` (FKB) per downstream. These are usually mathlib
master SHAs, but a release-pinned downstream lands them on a release-tag
commit (e.g. `v4.29.1`) that diverged from master. When an LKG or FKB is
a commit that CI did not build, its cache is empty, and a consumer that
fetches `lkg/latest.json` and bumps to it rebuilds mathlib from scratch.

This workflow builds and pushes those SHAs for the downstreams in the
inventory.

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
    │                     ├─ upload_cache       ubuntu-latest, has cache creds
    │                     │   shallow master → build cache → install curl
    │                     │   → mint + put-staged to R2
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

**Where the warmth shows up.** `export_lkg_snapshot.py` publishes
`last_known_good_commit_warm` and `first_known_bad_commit_warm` beside
each commit, each true only when that SHA's `cache_warmth` row is
verified warm (`already_warm` / `warmed`).

The snapshot reports warmth and leaves the choice to the consumer.
`last_known_good_commit` and `first_known_bad_commit` keep their
meaning as the compatibility boundary, so the LKG/FKB adjacency
invariant holds. A cold SHA is published like any other;
`bump-to-latest` bumps to it and puts a warning in the PR body that
names the cost and where to ask for warming. A project that repeatedly
pays for a from-source mathlib build then has a concrete thing to point
at, and the fix is to enable warming for it.

Warmth is a fact about a SHA in mathlib's cache, shared by every
downstream on it, so the snapshot publishes it per commit.

**Why publish-lkg / generate-pages chain off warming.** A freshly
reported LKG gets its warming attempt before the snapshot refresh, so
in the common case the cycle that reported it also publishes it warm.
If warming is skipped or fails outright, the snapshot and the status
page keep the previous cycle's content.

**Eventual consistency.** The cron schedule gives the chain a recurring
entry point, so a missed `workflow_run` event, a cancelled report run,
or a transient warm failure self-heals within one tick. See Trigger.

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

## Rebuilding the table

The `cache_warmth` table is a rebuildable cache of warming state, not
a system of record. When its rows stop matching reality — for example
after the cache bucket is cleared — drop and re-create it:

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

The rebuild is cheap. The planner consults only the rows of SHAs that
are currently some downstream's LKG or FKB, so the live set is small,
and the next scheduled tick re-probes it. A warm SHA takes the
`already_warm` path (cache get plus the `--no-build` check, no build).
Until that tick completes, the snapshot reports the re-probed SHAs
cold.

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
- **`workflow_dispatch`** with an optional `shas` input
  (comma-separated 40-char hex SHAs). When `shas` is non-empty, the
  planner skips the inventory, the DB, and the `cache_warmth` filter,
  so a forced re-warm runs even for a SHA the table marks warm.

## Opt-out

`DownstreamConfig.warm_cache: bool = True`. Warming is on by default,
so a newly added downstream's SHAs are warm by the time anything bumps
to them.

Set `"warm_cache": false` in `ci/inventory/downstreams.json` for a
downstream that does not consume hopscotch bumps. Its commits are
still published, and the snapshot reports them cold. If such a project
later starts to bump, its first bump PR carries the cold-cache warning,
which is the signal to flip the flag.

Deduplication by SHA keeps the default's cost low: passing downstreams
share master push tips, which mathlib's own CI caches, so the SHAs that
need a build are the bisect-boundary commits inside bors batches.

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

- no `cache_warmth` row → **include** (not yet attempted);
- verified warm (`already_warm` / `warmed`) → **skip** as
  `cache_warmth_hit`. Mathlib's olean cache is content-hashed and
  immutable per SHA, so a verified SHA stays verified;
- failed attempt with backoff still pending → **skip** as
  `retry_backoff`;
- failed attempt past its backoff → **include** (retry);
- failed `MAX_WARM_ATTEMPTS` (5) times → **skip** as
  `retry_exhausted`; the finalize summary warns.

The backoff doubles per recorded attempt: 6h (one scheduled tick),
12h, 24h, 48h. Mathlib master always builds, so a failed warming
attempt, `build_failed` included, is infrastructure trouble and not a
property of the SHA. The schedule keeps retrying without letting a
flapping failure occupy the self-hosted runner every tick. An
exhausted SHA needs operator attention, or a `--manual-shas` backfill,
which bypasses the filter.

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

Three jobs. Each has its own status, and the job that produces a
terminal status uploads `warm-result-<sha>` itself, so the finalize job
has exactly one final result per SHA. `build_and_stage` and `verify`
run the target SHA's own in-tree cache tool (`lake exe cache`).
`upload_cache` builds its cache tool from a shallow `master` checkout,
as mathlib4's own upload job does, so the credentials reach only a
binary this job built.

### `build_and_stage`

- `runs-on: [self-hosted, pr]`
- No cache token in scope.

Steps:

1. Install jq (the `pr` runner doesn't ship it).
2. Install elan (no default toolchain — lake reads `lean-toolchain`
   from the checkout).
3. Clone mathlib at the target SHA (`mathlib4/`, full commit graph,
   `--filter=blob:none` for size).
4. **Verify SHA is published** — accepts the SHA if it is reachable
   from `origin/master` (`git merge-base --is-ancestor`) or from a
   published release tag (`git tag --contains`). The job exits 1 with
   an error otherwise. This guards against a typo in the dispatch
   input and admits release-pinned downstreams (see "Published-only"
   under Trade-offs).
5. Checkout the SHA.
6. Clone mathlib master shallow into `mathlib4-tools/`, the leantar
   backfill source.
7. **Backfill leantar** into the target toolchain's sysroot when the
   pinned toolchain predates leantar bundling (nightly-2026-03-09):
   the cache tool resolves `leantar` from the sysroot alone.
8. **Probe:** `lake exe cache get` then
   `lake build --no-build -v Mathlib` (both in `mathlib4/`). This
   runs the target SHA's own in-tree cache tool, so the probe looks
   up the file names a consumer at that SHA computes. It reads the
   runner's local cache first; see "Probe measures the runner's view"
   under Trade-offs. The step always exits 0 and reports `warm` or
   `cold` through `outputs.status`, so a cold cache, the normal reason
   the workflow runs, leaves no `##[error]` annotation, and red in
   this job marks a real failure. If the probe reports `warm`, the
   status becomes `already_warm` and the chain ends.
9. `lake build Mathlib` (in `mathlib4/`, only when the probe reports
   `cold`). The cache is content-hashed, so the build reuses every
   file `cache get` pulled and rebuilds only the files the cache
   lacked.
10. `lake exe cache stage --staging-dir=../cache-staging`. Staging
    must use the in-tree tool: cache file names embed the tool's
    hash generation (`Cache/IO.lean` `rootHashGeneration`), and only
    the tool at the target SHA names files the way `cache get` at
    that SHA looks them up.
11. Upload the `stage-<sha>` artifact (`.ltar` files only, no binary).
12. Write result, upload as `warm-result-<sha>` (terminal:
    `already_warm`/`build_failed`) or `intermediate-<sha>`
    (non-terminal: `staged`).

### `upload_cache`

- `runs-on: ubuntu-latest`
- `needs: build_and_stage`, `if: needs.build_and_stage.outputs.status == 'staged'` —
  skipped entirely on `already_warm` / `build_failed`.
- `environment: cache-warming-token` — gates the mint, `main` only.
  See [Authorization](#authorization).

Steps:

1. Download `intermediate-<sha>` and `stage-<sha>`.
2. Install elan.
3. **Install curl.** The cache tool uploads with curl 8.20 or later
   (mathlib4#44940) and `ubuntu-latest` ships 8.5. The step fetches
   the static `v8.23.0` build from `leanprover-community/static-curl`,
   checks its pinned sha256, and prepends it to `PATH`. The release
   and the checksum match mathlib4's own upload job.
4. Clone mathlib master shallow into `mathlib4-tools/`.
5. `lake build cache` in `mathlib4-tools/`, which builds the cache
   binary this job runs.
6. **Mint R2 credentials** via mathlib-ci's `mint-s3-credentials`
   action, which exchanges the job's OIDC token at the mathlib cache
   broker for short-lived S3 credentials. A failed mint fails the
   step, and the result still records `push_failed`.
7. **Push to R2** via `lake env .lake/build/bin/cache put-staged
   --backend=s3 --staging-dir=../cache-staging
   --repo=leanprover-community/mathlib4` (run from
   `mathlib4-tools/`), with one retry. `MATHLIB_CACHE_PUT_URL` (the
   secret `MATHLIB_CACHE_R2_PUT_URL`) names one flat endpoint and turns
   the container policy off, so `--repo` alone sets the prefix and
   `leanprover-community/mathlib4` writes the flat `f/<hash>.ltar`
   names a mathlib4 consumer reads. `put-staged` uploads the staged
   `.ltar` files under the names staging gave them and computes no
   hashes, so the master-built tool is safe here across hash
   generations.
8. Write result, upload as `warm-result-<sha>` (terminal:
   `push_failed`) or `pushed-<sha>` (non-terminal: `pushed`).
9. Exit 1 on push failure, so the job shows red.

The R2 bucket is the only store this workflow writes, and the one
`verify` reads through `cache.mathlib.org`, so a push that succeeds and
a verify that passes describe the same files.

### `verify`

- `runs-on: ubuntu-latest`, no token.
- `if: needs.upload_cache.outputs.status == 'pushed'` — skipped
  entirely otherwise.

Steps:

1. Download `pushed-<sha>`.
2. Install elan.
3. Fresh clone of mathlib at the same SHA.
4. `lake exe cache get` then `lake build --no-build --rehash -v
   Mathlib`. This mirrors mathlib's own `post_steps` verification. The
   fresh clone is what makes the check read the bucket: the build
   runner's working directory holds the oleans locally.
5. Roll the status to `warmed` or `verify_failed`.
6. Upload `warm-result-<sha>`.
7. Exit 1 if the check failed.

## Status flow

| Status | Where set | Terminal | Surfaces as |
|---|---|---|---|
| `already_warm` | build_and_stage (probe found the cache complete) | yes | green job |
| `build_failed` | build_and_stage (`lake build Mathlib` failed) | yes | green job, recorded in summary |
| `staged` | build_and_stage (build + stage succeeded) | no — hands off to upload_cache | green job |
| `push_failed` | upload_cache (cache push errored) | yes | red job (allowed failure — run stays green) |
| `pushed` | upload_cache (push succeeded) | no — hands off to verify | green job |
| `warmed` | verify (post-push check passed) | yes | green job |
| `verify_failed` | verify (post-push check failed) | yes | red job (allowed failure — run stays green) |
| `no_result` | finalize (synthesised) | n/a | red finalize job |

"Allowed failure" means the job carries job-level
`continue-on-error: true`. It shows a red X in the run's job list and
emits an `::error::` annotation, but the workflow_run conclusion stays
`success`, so the publish-lkg and generate-pages chain still fires and
the snapshot refresh that serves every downstream goes ahead. The
failed SHA is published with its `*_warm` flag false, so a consumer
that bumps onto it gets a warning.

All three `*_failed` statuses are failures of the warming *attempt*,
not of the SHA: mathlib master always builds, so `build_failed` is
runner trouble (OOM, disk, toolchain download) just as `push_failed`
and `verify_failed` are broker or bucket trouble. The planner re-attempts each of
them on the backoff schedule described under "Plan job".

The finalize job synthesises `no_result` for any SHA that was in the
plan but uploaded no `warm-result-<sha>` artifact, typically after a
runner crash or a job timeout that stopped the worker before its
terminal upload step. It surfaces as a red finalize job, so a dropped
SHA is visible.

## Orchestrator finalize job

The `finalize` job downloads all `warm-result-<sha>` artifacts (each
into its own subdirectory under `results/`, without `merge-multiple`:
every terminal stage uploads the same in-artifact filename, and a
merge would let one SHA's row overwrite another's), combines them
with synthetic `no_result` entries for any planned SHA that did not
report back, and renders to the run's job summary:

- A total count of SHAs processed.
- A status-breakdown table with one row per non-zero status.
- A per-SHA table with short SHA, tag, status, and the list of
  downstreams that benefit.

The finalize job exits 1 if any SHA reports `no_result`; per-SHA
`push_failed` / `verify_failed` already surfaced as red
allowed-failure jobs in the matrix.

After it renders the summary, the job runs
`scripts/record_warm_shas.py --backend sql ... --summary summary.json`,
which upserts an `(upstream, sha)` row into `cache_warmth` for every
entry with a terminal status (`already_warm`, `warmed`,
`build_failed`, `push_failed`, `verify_failed`). The row stores the
status verbatim, the attempt time, and an incremented attempt counter.
The planner skips a verified-warm row in every future plan, because
the olean cache is content-hashed and immutable per SHA, and a failed
row re-enters the plan on the backoff schedule. The script records
terminal statuses only: `no_result` and the planner's skip statuses
describe SHAs whose attempt did not run. The recording step has
`if: always()`, so a partial failure still persists the SHAs that did
report.

`record_warm_shas.py` takes the write-capable `POSTGRES_DSN`, so
`finalize` runs in the main-only `publish` GitHub Environment, and the
job's `if:` also checks `github.ref == 'refs/heads/main'`, so a branch
`workflow_dispatch` skips it instead of failing at the environment
gate. **The `publish` environment must stay branch-policy-only.** A
required-reviewer or wait-timer rule would stall this unattended
scheduled chain, and the report and on-demand `publish` jobs that share
the environment, until someone approves.

## Throttling

- **Workflow-level concurrency.** `concurrency: warm-mathlib-cache`
  with `cancel-in-progress: false`. At most one warming run executes
  and one queues at a time across the repo. A third dispatch replaces
  the queued run, so the latest plan is the one that runs.
- **Matrix throttle.** `max-parallel: 1` on the orchestrator's
  `warm-sha` matrix caps the per-SHA chains in flight. The
  `build_and_stage` job runs on the shared self-hosted `pr` pool, and
  the cap keeps cache warming from contending with the regression
  probe job. Raise it if the plan grows large and the pool has
  headroom.

## Authorization

The credentials come through one GitHub Environment on this repo,
`cache-warming-token`, whose branch policy admits `main` alone. That is
the trust boundary for the mint.

The `mint-s3-credentials` action sends the job's OIDC token to the
mathlib cache broker, which answers S3 credentials that expire after
900 seconds. The broker's policy decides the grant from the token's
claims: the row `downstream-reports-warm-publish` matches this job's
subject and grants `object-read-write` on the `mathlib4/` prefix of the
`mathlib4-cache` bucket. That prefix holds the master set. The broker
lives in `mathlib-initiative/cache-infrastructure` under
`cache-broker/`.

### One-time prerequisites

1. **GitHub Environment** named `cache-warming-token` on this repo
   (Settings → Environments), with a deployment branch policy that
   lists `main`. The environment scopes the OIDC subject claim, and the
   branch policy is what keeps the mint main-only.
2. **A broker grant row** whose claims match this job's subject.
   Without one the broker answers 403 and the mint step fails.
3. **Repo variable** `MATHLIB_CACHE_BROKER_URL` — the broker's
   credential endpoint. The mint fails while it is unset.
4. **Environment secret** `MATHLIB_CACHE_R2_PUT_URL` on
   `cache-warming-token` — the flat destination,
   `https://<account>.r2.cloudflarestorage.com/mathlib4-cache/mathlib4`.
   While it is unset, the push step fails and the SHA records
   `push_failed`.

The variable and secret names match mathlib4's own for its
`upload_cache` job.

## Token isolation

The mint runs in `upload_cache`, a separate job from the build, so the
elaboration-time code in `build_and_stage` runs without the
credentials. `verify` runs on a third runner with no credential.

The R2 credentials stay out of `$GITHUB_ENV`: the action masks them and
returns them as step outputs, and only the push step maps them into its
own `env:`.

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

GitHub registers `workflow_dispatch` from the workflow file on the
default branch and runs the dispatched ref's copy of both files
(`./_warm-one-sha.yml` resolves at the caller's ref):

```bash
gh workflow run warm-mathlib-cache.yml \
  --ref <branch> \
  -f shas=<40-hex>
```

`build_and_stage` runs, and `upload_cache` stops at the
`cache-warming-token` gate, whose branch policy admits `main` alone. A
feature-branch dispatch exercises the build and stage path and uploads
nothing.

### End-to-end check

After a successful run, on a *different* machine:

```bash
git clone https://github.com/leanprover-community/mathlib4
cd mathlib4
git checkout <warmed-sha>
lake exe cache get
lake build --no-build -v Mathlib    # should succeed without rebuilds
```

A fresh-machine check catches a CDN edge that serves stale data,
which the in-job verify cannot see.

## Failure modes

All three per-SHA failure statuses are retried on the backoff
schedule; a SHA that exhausts its retry budget surfaces as
`retry_exhausted` in the finalize summary with a `::warning::`
annotation.

- **`build_failed`** — mathlib did not build at the SHA. Master always
  builds, so this is runner trouble (OOM, disk pressure, a toolchain
  download flake). The workflow stays green.
- **`push_failed`** — the mint or the put-staged call errored. Look
  at the mint and push step logs, the broker grant, and the bucket.
  The upload_cache job goes red (allowed failure); the run stays
  green. A rejected command line, a curl older than the tool accepts,
  or a prefix the credential does not cover, fails every SHA in the
  tick rather than one.
- **`verify_failed`** — the push reported success, but `cache get`
  plus `lake build --no-build` found missing oleans. Either the push
  uploaded an incomplete set, or the read lagged the write. The verify
  job goes red (allowed failure); the run stays green.
- **Mint failed** — the broker refused the token, and the SHA records
  `push_failed`. Check the grant row and the `MATHLIB_CACHE_BROKER_URL`
  variable.

## Trade-offs

- **Three-job split mirrors mathlib4 CI.** Build runs on a self-hosted
  runner with no credential; push runs on ubuntu-latest with the R2
  credentials; verify runs on a fresh ubuntu without them. The upload
  job builds its cache binary from a shallow `master` checkout, so the
  binary that holds the credentials came from this job. This is the
  posture of the `build` → `upload_cache` → `post_steps` chain in
  `mathlib4/.github/workflows/build_template.yml`.
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
  (`git tag --contains`), and refuses everything else. A downstream
  that pins a stable mathlib release (e.g. `v4.29.1`) lands its
  LKG/FKB on a tag that diverged from master, and `lake exe cache get`
  at that tag reads the warmed oleans. mathlib's own release CI caches
  the same content-addressed blobs, so the push is idempotent. The
  check refuses arbitrary branch tips and typo SHAs.
- **Probe measures the runner's view.** `cache get` downloads only the
  files absent from the runner's local cache, and `lake build
  --no-build -v Mathlib` then checks that cache. The `pr` pool keeps
  the cache across jobs, and every mathlib build on the pool
  (mathlib4's own CI and this `build_and_stage`) packs its oleans into
  it before its upload runs, so the probe can report `warm` for a file
  the bucket lacks. A false `cold` costs one build. `verify` on a fresh
  runner is the check that reads what consumers read; the probe routes
  between the fast path and a build.
