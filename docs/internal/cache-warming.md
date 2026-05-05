# Mathlib cache warming

Builds mathlib at the LKG / FKB SHAs reported for opted-in downstreams
and pushes the resulting oleans to mathlib's shared Azure cache, so
external consumers of `lkg/latest.json` (e.g. the `bump-to-latest`
action) hit a warm cache instead of having to rebuild mathlib from
scratch.

## Why

mathlib's master branch advances via batched bors merges. mathlib's CI
(`mathlib4/.github/workflows/build_template.yml`, `upload_cache` job)
only pushes oleans for the SHAs CI actually built — typically the bors
merge commits, not every commit on master.

The downstream-reports daily regression workflow records, per
downstream, a `last_known_good_commit` (LKG) and `first_known_bad_commit`
(FKB) — both are mathlib master SHAs. When those land on master commits
that didn't go through CI, their cache is empty. A consumer that
fetches our `lkg/latest.json` and asks for the LKG ends up rebuilding
mathlib from scratch.

This workflow closes that gap by performing the build and push
ourselves for a curated set of downstreams.

## Topology

```
mathlib-downstream-report (on main, success)
    │
    ▼  workflow_run trigger
warm-mathlib-cache.yml         (orchestrator)
    │
    ├─ plan job        → reads inventory + DB, emits matrix of unique SHAs
    │
    └─ warm-sha matrix → calls _warm-one-sha.yml once per SHA (max-parallel: 4)
                          │
                          ├─ build_and_stage    self-hosted, NO token
                          │   clone target SHA → probe → build → stage
                          │
                          ├─ upload_cache       ubuntu-latest, has cache token
                          │   shallow master → build cache → mint → put-staged
                          │
                          └─ verify             ubuntu-latest, NO token
                              fresh clone → cache get → assert all oleans present
```

## Files

| File | Purpose |
|---|---|
| `.github/workflows/warm-mathlib-cache.yml` | Orchestrator: plan, matrix dispatch, summary. |
| `.github/workflows/_warm-one-sha.yml` | Reusable per-SHA worker (`workflow_call`). |
| `scripts/plan_cache_warm_jobs.py` | Builds the matrix from inventory + DB or from a manual SHA list. |
| `scripts/test_plan_cache_warm_jobs.py` | Unit tests for the planner. |
| `scripts/models.py` | `DownstreamConfig.warm_cache: bool = False` opt-in flag. |

## Trigger

- **`workflow_run`** on completion of `mathlib-downstream-report` —
  filtered to `branches: [main]` and `conclusion == 'success'`.
  Production runs only fire from main.
- **`workflow_dispatch`** with optional `shas` input
  (comma-separated 40-char hex SHAs). When `shas` is non-empty the
  inventory + DB are bypassed and only those SHAs are processed.

## Opt-in

`DownstreamConfig.warm_cache: bool = False`. Set
`"warm_cache": true` on selected entries in
`ci/inventory/downstreams.json`. Default is false so existing entries
are unaffected.

```json
{
  "name": "FLT",
  "repo": "ImperialCollegeLondon/FLT",
  "default_branch": "main",
  "dependency_name": "mathlib",
  "skip_known_bad_bisect": false,
  "enabled": true,
  "warm_cache": true
}
```

## Plan job

`scripts/plan_cache_warm_jobs.py` runs in two modes:

- **Manual** (`--manual-shas a,b,c`): validates each SHA is 40-char
  lowercase hex, dedups, and emits one matrix entry per SHA with
  `tag: "manual"` and `downstreams: []`. Skips DB / inventory.
- **DB + inventory** (default): loads enabled inventory entries with
  `warm_cache=True`, reads `downstream_status` (workflow=`regression`)
  via `SqlBackend.load_all_statuses`, collects every non-null LKG /
  FKB, deduplicates by SHA, and tags each entry `lkg`, `fkb`, or
  `both` based on the union of roles across downstreams.

Output JSON:

```json
{
  "include": [
    {"sha": "<40-hex>",
     "tag": "lkg|fkb|both|manual",
     "downstreams": ["physlib", "FLT"]}
  ]
}
```

The orchestrator's `plan` job reads this, sets `matrix` and
`has_jobs` outputs, and the matrix job is skipped when the plan is
empty.

## Per-SHA chain (`_warm-one-sha.yml`)

Three jobs. Each has its own status; a terminal status uploads
`warm-result-<sha>` directly so the orchestrator's summary always has
exactly one final result per SHA. The cache tool is built fresh in
both `build_and_stage` and `upload_cache` from a shallow `master`
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
4. **Verify SHA is on master** — `git merge-base --is-ancestor
   "$SHA" origin/master`. The job exits 1 with a clear error
   otherwise. Guards against typos in the dispatch input.
5. Checkout the SHA.
6. Clone mathlib master shallow into `mathlib4-tools/`.
7. `lake build cache` in `mathlib4-tools/`.
8. **Probe:** `../mathlib4-tools/.lake/build/bin/cache get` (run from
   `mathlib4/`, no `lake env` needed for `get`) then
   `lake build --no-build -v Mathlib`. If both succeed, status
   becomes `already_warm` and the chain ends.
9. `lake build Mathlib` (in `mathlib4/`, only runs when the probe
   failed). The cache is content-hashed, so anything `cache get`
   already pulled is reused; only files whose hashes weren't in the
   cache get rebuilt.
10. `lake env ../mathlib4-tools/.lake/build/bin/cache stage
    --staging-dir=../cache-staging`. `lake env` is required for
    `stage` (and `put-staged`) so `leantar` is found on PATH.
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
   `mathlib4-tools/`).
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
| `push_failed` | upload_cache (cache push errored) | yes | red job (final step exits 1) |
| `pushed` | upload_cache (push succeeded) | no — hands off to verify | green job |
| `warmed` | verify (post-push check passed) | yes | green job |
| `verify_failed` | verify (post-push check failed) | yes | red job (final step exits 1) |
| `no_result` | summary (synthesised) | n/a | red summary job |

`build_failed` is recorded but not loud, because mathlib was
occasionally non-buildable on master in the past — the FKB of a
downstream is a mathlib commit that broke that downstream, not
necessarily mathlib itself, but rare exceptions exist.

`no_result` is synthesised by the summary job for any SHA that was
in the plan but didn't upload a `warm-result-<sha>` artifact —
typically a runner crash or a job timeout that killed the worker
before its terminal upload step ran. It surfaces as a red summary
job so we don't silently drop those.

## Orchestrator summary

The `summary` job downloads all `warm-result-<sha>` artifacts (each
into its own subdirectory under `results/` — *not* `merge-multiple`,
because every terminal stage uploads under the same in-artifact
filename and merging would silently overwrite earlier rows),
combines them with synthetic `no_result` entries for any planned
SHA that didn't report back, and renders to the run's job summary:

- A total count of SHAs processed.
- A status-breakdown table with one row per non-zero status.
- A per-SHA table with short SHA, tag, status, and the list of
  downstreams that benefit.

The summary job exits 1 if any SHA reports `push_failed`,
`verify_failed`, or `no_result`.

## Throttling

- **Workflow-level concurrency.** `concurrency: warm-mathlib-cache`
  with `cancel-in-progress: false`. At most one warming run executes
  or queues at a time across the repo; if a third dispatch arrives
  while one is running and one queued, the previously-queued one is
  dropped (the latest plan is always the one to honour).
- **Matrix throttle.** `max-parallel: 4` on the orchestrator's
  `warm-sha` matrix caps in-flight per-SHA chains. The
  `build_and_stage` job runs on the shared self-hosted `pr` runner;
  the runner pool itself enforces serialisation, but `max-parallel`
  is the explicit ceiling.

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
cat /tmp/plan.json   # → {"include": []} if no warm_cache=true entries
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

- **`build_failed`** — mathlib didn't build at the SHA. Probably a
  legitimately broken master commit (rare). Workflow does not fail.
- **`push_failed`** — mint succeeded but the put-staged call errored.
  Look at the push step's logs and Azure storage account health.
  Workflow fails.
- **`verify_failed`** — push reported success but the post-push
  cache get + `lake build --no-build` showed missing oleans.
  Indicates either the push didn't upload everything, or there's a
  read-after-write consistency issue. Workflow fails.
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
- **Master-only.** `build_and_stage` refuses non-master SHAs via an
  explicit ancestor check. Cache pushes for branch / tag SHAs would
  pollute the canonical cache namespace and aren't meaningful for
  our consumers.
- **Probe is best-effort.** `lake build --no-build -v Mathlib` after
  `cache get` is the canonical way to check completeness. False
  negatives (cache present but probe failed) waste a build but are
  harmless. False positives are not really possible — if lake says
  every olean is present and valid, they are.
