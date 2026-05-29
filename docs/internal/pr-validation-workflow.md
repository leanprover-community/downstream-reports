# PR validation workflow reference

`mathlib-pr-validation.yml` answers the question: *will this `mathlib4` PR
break a named downstream?* — on demand, before merge, via a comment directive.

Unlike the scheduled regression workflow (which tests every tracked downstream
against a fixed `mathlib4` commit and only alerts on episode state changes),
PR validation is fired ad-hoc by a reviewer on a specific PR, runs against
the PR's would-be-merged tree (or against a known-good baseline with the PR
rebased onto it), and posts a single Markdown comment back on the PR with the
verdicts. It does not touch the regression database; PR runs are ephemeral.

---

## Round-trip topology

```
mathlib4 PR comment "!downstream-check FLT, Toric --merge-branch"
    │
    ▼
mathlib4/.github/workflows/pr_check_downstream.yml          (thin, in mathlib4)
    │  - author-association gate
    │  - parse comment, resolve refs/pull/N/merge to a SHA
    │  - validate entry grammar
    │  - mint App token, post ack comment, workflow_dispatch
    ▼
downstream-reports/.github/workflows/mathlib-pr-validation.yml  (this repo)
    │  - matrix job per entry on [self-hosted, pr]
    │  - clone mathlib4, resolve tree (LKG-rebase or merge SHA), build downstream
    │  - assemble one dispatch-level comment, post via App token
    ▼
mathlib4 PR comment — summary table + one section per entry
```

The mathlib4 side (the directive handler, the ack comment, and the grammar
validator) lives in `leanprover-community/mathlib4` and
`leanprover-community/mathlib-ci`. Everything documented below lives in this
repo and is what runs once the dispatch lands.

A dedicated GitHub App (`downstream-reports-automation`) is installed on both
mathlib4 and downstream-reports. Each side mints a token scoped to the *other*
repo: mathlib4 to dispatch into us, this workflow's `report` job to post the
result comment back. The App's private key sits in Azure Key Vault and is
fetched at run time via OIDC federation to an Entra app pinned to the
`pr-validation-token` environment subject — manual dispatches from a feature
branch authenticate identically to production runs from `main`.

---

## The two modes: LKG vs merge

Each entry in the directive runs in one of two modes. LKG is the default;
`--merge-branch` opts a single entry into merge mode.

**LKG mode** (default) takes the downstream's `last_known_good_commit` from
the published [`lkg/latest.json`](https://downstreamreports.z13.web.core.windows.net/lkg/latest.json)
snapshot, cherry-picks the PR's commits onto it, sanity-builds `Mathlib`, and
only then builds the downstream. The verdict is independent of current master
health: even when `mathlib4` master is already broken for that downstream (the
case this whole repo exists to track), an LKG-mode pass tells you the PR did
*not* introduce that break.

**Merge mode** (`--merge-branch`) clones `mathlib4` at the PR's
`merge_commit_sha` directly — current master + the PR applied, as GitHub
computes it — and builds the downstream against that. Cheaper because the
upstream olean cache hits cleanly, but a failure may be master's fault rather
than the PR's.

LKG mode is the more actionable signal in steady state. Cost-wise,
`lake exe cache get` is content-keyed so files the PR did not touch still
hit cache after the rebase; only PR-modified files (and their dependents)
plus the `Mathlib` library sanity-build add real time over the merge-mode
path. In practice that's minutes, not the hour-plus a full rebuild would
take.

---

## Comment grammar

Single line, first line of the triggering comment:

```
!downstream-check <name-or-slug>[@<rev>] [--merge-branch][, <name-or-slug>[@<rev>] [--merge-branch] ...]
```

Per comma-separated entry:

| Token | Required? | Meaning |
|---|---|---|
| `<name-or-slug>` | yes | Inventory short name (case-sensitive match against `inventory.downstreams[*].name`) or GitHub `owner/repo` slug (case-insensitive match against `inventory.downstreams[*].repo`). Either form resolves to the same canonical row; the user-typed literal is preserved as `requested_name` for display. |
| `@<rev>` | no | Any git refspec — branch, tag, or commit SHA — for the downstream checkout. Defaults to the inventory's `default_branch`. |
| `--merge-branch` | no | Flips this one entry from LKG mode (the default) to merge mode. Per-entry, so `FLT --merge-branch, Toric` runs the two in different modes. |

Authorisation is gated on the mathlib4 side to `OWNER` / `MEMBER` /
`COLLABORATOR`. There is no `all` shorthand — entries are enumerated.

---

## Job structure

```
plan (ubuntu-latest) ──► validate (×N, self-hosted, no secrets) ──► report (ubuntu-latest)
```

Concurrency group is `mathlib-pr-validation-${pr_number}` with
`cancel-in-progress: false`: a new push (which produces a new merge SHA) and
a re-dispatched directive both queue behind the running build rather than
cancel it. Each individual run takes long enough on the self-hosted `pr` pool
that finishing what we started is almost always cheaper than discarding it;
the dispatcher can still cancel manually if a stale build stops being
interesting.

### `plan`

Runs `scripts/pr_validation/build_matrix.py` to turn the comma-separated
`downstreams` input into a GitHub Actions matrix `include` list.

1. Loads `ci/inventory/downstreams.json` and indexes by both short name and
   `owner/repo` slug.
2. Parses each entry into `(bare-token, rev, mode)`. Unknown flags or empty
   tokens fail the job. Unknown names produce a single
   `error: unknown downstream(s): …` line and fail the job.
3. Fetches the published LKG snapshot. LKG-mode entries need
   `last_known_good_commit` from it (hard requirement — a downstream with no
   recorded LKG must fall back to `--merge-branch`). Both modes optionally
   pull `first_known_bad_commit` so the result comment can frame master
   health definitively. Snapshot unreachable + all-merge-mode dispatch
   proceeds without FKB enrichment.
4. Deduplicates on `(canonical name, rev, mode)`; the user's first-typed form
   wins as `requested_name`.
5. Emits `matrix.json` with one row per surviving entry.

The `has_downstreams` job output gates the rest of the workflow — an empty
matrix (only possible with stale dispatches) cleanly skips both `validate`
and `report`.

### `validate` (matrix)

Runs on the `[self-hosted, pr]` pool, up to 2 jobs in parallel, with a
configurable timeout (`PR_VALIDATION_TIMEOUT_MINUTES`, default 90 min). One
job per matrix row. The matrix display name encodes `(name, rev_slug, mode)`
so two entries that differ only in rev or mode show up as distinct rows in
the UI.

Each job installs `lakedit` (the manifest-rewriting tool) by cloning
`leanprover-community/hopscotch@$HOPSCOTCH_REF` and running `lake build
lakedit`, then runs `scripts/pr_validation/validate.py` through a pipeline
of stage functions:

| Stage | What it does | `stage` label on infra failure |
|---|---|---|
| 1. clone | `git clone --no-checkout` upstream `mathlib4` | `clone` |
| 2. resolve mathlib tree | Fetch the merge SHA (and LKG, in LKG mode); resolve `PR_BASE = merge^1` and `PR_HEAD = merge^2`; check out the merge SHA (merge mode) or check out LKG and `git cherry-pick PR_BASE..PR_HEAD` (LKG mode) | `fetch` / `checkout` / `rev_parse` / `rebase_conflict` |
| 3. warm cache | `lake exe cache get` — best-effort, failure is logged but not fatal | — |
| 4. sanity-build Mathlib | LKG mode only: `lake build Mathlib` to disambiguate "PR depends on post-LKG mathlib changes" from a genuine downstream break before that signal bleeds into a downstream build error | `mathlib_build_at_lkg` |
| 5. clone downstream | Clone + `fetch origin <rev>` + `checkout FETCH_HEAD` (works for branches, tags, and reachable SHAs alike) | `clone_downstream` / `fetch_downstream` / `checkout_downstream` |
| 6. lakedit set | `lakedit set <dep> --path <local mathlib4>` rewrites the downstream's `lake-manifest.json` | `lakedit` |
| 7. lake update + build | `lake update <dep>` then `lake build` on the downstream | `lake_update` (update phase) / `build` (final verdict) |

A single `Log` class is the chokepoint for live streaming: every subprocess
line and every script-emitted line lands in both the live workflow log and
`build.log`. `post_results.py` and `summarize.py` consume the same filtered
transcript.

Each job uploads a `result-<name>-<rev_slug>-<mode>` artifact containing
`result.json` and `build.log`, and runs `summarize.py` to append a
per-downstream block to `$GITHUB_STEP_SUMMARY`.

### `report`

Skipped when `post_results=false` so the workflow can be exercised without
the App / token plumbing (the per-entry artifacts are still available for
manual review). Otherwise runs `scripts/pr_validation/post_results.py`,
which:

1. Walks every `result-*/result.json` and reads the corresponding filtered
   `build.log` tail.
2. Looks each downstream up in the local inventory for `repo` and
   `default_branch` metadata.
3. Sorts entries by `(name, rev, mode)` so related rows sit together.
4. Pre-trims each log to the per-entry budget, then assembles the body and
   progressively halves the longest inlined log until the body fits under
   GitHub's 65,536-char comment limit.
5. POSTs **one** comment per dispatch via the App token. Re-dispatches
   produce additional comments (no edit-in-place, no hidden markers), so the
   audit trail of what was triggered survives.

---

## Security boundary

The `validate` job runs on `[self-hosted, pr]` and is deliberately kept off
the main regression-runner pool: it builds arbitrary `mathlib4` trees (the
PR's would-be-merged commit, or the PR's commits cherry-picked onto a
downstream's LKG) and runs `lake build` on a downstream, so it must never
see `POSTGRES_DSN` or any other regression-side secret. The `pr` runner
label gates the separation. The `report` job, which holds the App token,
runs on `ubuntu-latest` and never invokes `lake` or `hopscotch`.

This mirrors the regression workflow's split: `plan` and `report` can
talk to the outside world; `validate` cannot.

---

## `result.json` shape

Written by `validate.py` to each artifact root. Optional fields are only
included when they carry information beyond their default, so the comment
renderer's `result.get(...)` falsy-defaults are meaningful.

```json
{
  "status":         "pass" | "fail" | "infra_failure",
  "stage":          "build" | "clone" | "fetch" | …,
  "message":        "human-readable one-liner",
  "downstream":     "<canonical inventory name>",
  "merge_sha":      "<PR merge SHA>",
  "downstream_sha": "<resolved downstream HEAD>",
  "mode":           "lkg" | "merge",

  "lkg_commit":       "...",     // LKG mode only
  "requested_name":   "...",     // omitted when user typed the canonical name
  "downstream_rev":   "...",     // omitted when user used the default branch
  "fkb_commit":       "...",     // present when snapshot recorded a master regression
  "pr_base_sha":      "...",     // merge^1
  "pr_head_sha":      "...",     // merge^2; omitted for fast-forward merges
  "commits_replayed": 3,         // |PR_BASE..PR_HEAD|
  "replayed_tree_sha":"..."      // LKG mode only — post-cherry-pick HEAD
}
```

`validate.py` exits 0 in every case except a script-level crash: a build
failure or an infra failure is the meaningful answer for this workflow, not
a workflow error.

---

## Result comment shape

One Markdown comment per dispatch, opening with an optional `_Requested by
@<user>._` mention, then a bold dispatch title with the merge SHA and run
link, then a summary table (when there are at least two entries), then one
bold-headed section per entry separated by `---` rules. Headings are
intentionally bold-inline rather than `##` so the comment stays visually
quiet on the PR page.

```
_Requested by @marcelolynch._

**Downstream validation against PR merge [`abc1234`](commit-url)** · [run](run-url)

| Entry | Verdict |
|---|---|
| `FLT` | ✅ builds (rebased onto LKG) |
| `Toric --merge-branch` | ❌ fails (master incompatibility at [`fff7777`](commit-url)) |

---

**✅ FLT builds against this PR rebased onto LKG**

**Tested:** 3 PR commit(s) ([`abc1234..def5678`](compare-url)) cherry-picked
onto FLT's last-known-good mathlib commit [`257086b`](commit-url), built
against [`leanprover-community/FLT@13206c9`](commit-url).

---

**❌ Toric --merge-branch fails against this PR**

> mathlib master is currently incompatible with Toric — the regression
> was first observed at [`fff7777`](commit-url). This failure may
> reflect that existing incompatibility rather than the PR itself.
> Drop `--merge-branch` to re-run against Toric's last-known-good
> mathlib instead.

**Tested:** the PR's merge tree [`abc1234`](commit-url) (head …, 3 commit(s)
over base …), built against [`leanprover-community/Toric@…`](commit-url).

<details><summary>failure log</summary>
…tail of build.log…
</details>
```

Per-entry header variants:

- `**✅ <entry> builds against this PR[ rebased onto LKG]**` — pass.
- `**❌ <entry> fails against this PR[ rebased onto LKG]**` — fail; inlines
  the filtered tail of `build.log` in a `<details>` block.
- `**⚠️ <entry>: could not validate (PR conflicts with LKG)**` — LKG mode,
  `stage=rebase_conflict`.
- `**⚠️ <entry>: could not validate (mathlib build failed at LKG)**` — LKG
  mode, `stage=mathlib_build_at_lkg`.
- `**⚠️ <entry>: could not validate (infra: <stage>)**` — every other infra
  failure (clone / fetch / lakedit / lake update / …).

A blockquote subtitle frames every verdict except a clean LKG pass with no
recorded master regression, where the section header and recipe already say
everything.

The body caps at ~60K chars to stay under GitHub's 65,536-char limit.
Per-entry log budgets scale to the number of failing entries; pathologically
large logs are progressively halved until the body fits, with the
run-artifact link in the title as the always-available full-log fallback.

---

## LKG-mode-specific failure modes

| `stage` | What it means | What the PR author should do |
|---|---|---|
| `rebase_conflict` | Cherry-pick of the PR's commits onto LKG produced a conflict. | The PR likely depends on post-LKG mathlib changes; re-run with `--merge-branch` (and live with the master noise) or wait for a fresh LKG. |
| `mathlib_build_at_lkg` | Cherry-pick succeeded but `lake build Mathlib` failed at LKG. | Same conclusion as `rebase_conflict`. |
| `lkg_missing` (raised in `build_matrix.py`, no per-entry artifact) | The downstream has no recorded LKG yet (e.g. recently enabled). | Use `--merge-branch`; LKG mode requires a successful regression run on record. |

These render as warnings (not errors) in the workflow log and as `⚠️
could not validate` sections in the comment — the underlying signal is "the
PR cannot be tested in isolation against an older mathlib", not "infra
broke".

---

## Inventory and configuration

A downstream is eligible for PR validation as long as it appears in
`ci/inventory/downstreams.json`. There is no per-downstream opt-in flag and
no `enabled` gate — the directive lists what to test.

The `dependency_name` inventory field (default `mathlib`) is what
`lakedit set` rewrites, so downstreams whose manifest dependency on
`mathlib4` is recorded under a non-default name continue to work without
special-casing.

Tunable surfaces:

| Surface | Where | Default |
|---|---|---|
| Inventory URL (mathlib4 side) | `vars.INVENTORY_URL` | downstream-reports `main` raw URL |
| Hopscotch ref (used by `install_lakedit.sh`) | `vars.HOPSCOTCH_REF` | `v1.5.0` |
| Runner pool | matrix `runs-on` | `[self-hosted, pr]` |
| Build timeout | `vars.PR_VALIDATION_TIMEOUT_MINUTES` | `90` |
| LKG snapshot URL | `build_matrix.py --lkg-snapshot-url` | published `lkg/latest.json` on Azure |

Repository-level secrets (`DOWNSTREAM_REPORTS_AUTOMATION_APP_ID`,
`LPC_AZ_TENANT_ID`) and variables (`MATHLIB_AZ_KEY_VAULT_NAME`,
`GH_APP_AZURE_CLIENT_ID_DOWNSTREAM_REPORTS_AUTOMATION`) drive the Azure-Key-Vault
token mint in the `report` job.

---

## Known limitations

- **No master baseline.** PR validation does not run the downstream against
  current master in parallel. LKG mode answers "PR's effect in isolation";
  merge mode answers "PR + current master combined". Neither alone gives a
  conclusive "this is the PR's fault" / "master is also broken" verdict —
  the reader is expected to check the latest downstream report alongside.
- **No commit-status check.** The result is an informational comment, not a
  merge gate.
- **No DB persistence.** PR runs are ephemeral; regression-tracking state
  in this repo is untouched.
- **No artifact reuse.** Each run rebuilds `lakedit`, re-clones mathlib4 and
  the downstream, and re-fetches the olean cache. Persisting `.lake/` across
  runs on the same self-hosted runner is the obvious cost-reducer once usage
  justifies it.
