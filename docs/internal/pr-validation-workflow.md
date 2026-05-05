# PR-triggered downstream validation

Implementation spec for testing a `mathlib4` PR's would-be-merged tree against a
named downstream, triggered by a PR comment.

## Goal

Answer "Will this PR break downstream `D`?" before merge, on demand, via
`/check-downstream <name>` in a `mathlib4` PR comment.

## Non-goals (v1)

- No master baseline. The reader is assumed to have checked downstream health
  before dispatching. The result comment links to the latest downstream report
  and notes the caveat.
- No commit-status check. The result is an informational comment, not a merge
  gate.
- No DB persistence. PR runs are ephemeral; the regression-tracking schema in
  hopscotch-reports stays untouched.
- No artifact reuse across PR runs.
 
## Topology

Two workflows, two repos, one new GitHub App.

```
mathlib4 PR comment "/check-downstream FLT"
    │
    ▼
mathlib4/.github/workflows/pr_check_downstream.yml          (thin)
    │  - auth check (author_association)
    │  - parse comment, resolve merge SHA
    │  - validate downstream names against inventory
    │  - mint App token, post ack comment, dispatch
    ▼
hopscotch-reports/.github/workflows/mathlib-pr-validation.yml  (heavy)
    │  - matrix job per downstream on [self-hosted, pr]
    │  - clone mathlib4 @ merge SHA + lake exe cache get
    │  - clone downstream, lakedit set --path, lake update, lake build
    │  - capture log, classify outcome
    ▼
mathlib4 PR comment (edited in place)
    "✅ FLT builds against this PR"  /  "❌ FLT fails — log: ..."
```

## GitHub App setup

We create a dedicated App rather than reusing any existing one — the scopes
needed here are narrow and shouldn't be bundled with broader bots.

### 1. Create the App

GitHub → Settings → Developer settings → GitHub Apps → New GitHub App.

- **Name:** `mathlib-pr-downstream-validator` (or similar; must be globally
  unique).
- **Homepage URL:** the hopscotch-reports repo URL.
- **Webhook:** disable (we don't receive events; we only mint tokens).
- **Repository permissions:**
  | Scope          | Access       | Why                                                  |
  |----------------|--------------|------------------------------------------------------|
  | Metadata       | Read         | Required by GitHub for any App.                      |
  | Contents       | Read         | Read inventory, fetch merge ref info.                |
  | Pull requests  | Read & write | Post/edit the result comment on the mathlib4 PR.     |
  | Issues         | Read & write | PR comments use the Issues API for create/edit/list. |
  | Actions        | Read & write | `workflow_dispatch` against hopscotch-reports.       |
- **Account permissions:** none.
- **Where can this App be installed?** Only on this account
  (`leanprover-community`).

After creation:
1. Generate a private key (`.pem`). Store it securely — it is shown once.
2. Note the **App ID**.
3. Install the App on **both** repos:
   - `leanprover-community/mathlib4`
   - `leanprover-community/hopscotch-reports`
   Restrict the installation to those two repos in the install dialog.
4. Note the **Installation IDs** (one per repo) — visible in the install URL or
   via `GET /app/installations`. We don't strictly need them at runtime
   (`actions/create-github-app-token` discovers them) but it's useful to record
   them in a sealed place for debugging.

### 2. Store the credentials

For v1, use plain GitHub repository secrets on both sides. The existing
`mathlib-ci/.github/actions/azure-create-github-app-token` flow keeps the
mathlib bot key in Azure Key Vault; we are intentionally *not* coupling this
new App to Azure — it can move there later if needed without changing
workflow logic, since `actions/create-github-app-token` is a drop-in.

In `leanprover-community/mathlib4` repo settings → Secrets and variables →
Actions:
- `DOWNSTREAM_VALIDATOR_APP_ID` (App ID, plain text, can be a `var` not secret).
- `DOWNSTREAM_VALIDATOR_PRIVATE_KEY` (full `.pem` contents).

In `leanprover-community/hopscotch-reports`:
- `DOWNSTREAM_VALIDATOR_APP_ID`
- `DOWNSTREAM_VALIDATOR_PRIVATE_KEY`

(Same App, so identical values. We store both because the dispatching workflow
on mathlib4 mints a token to call hopscotch-reports' `workflow_dispatch`, and
the hopscotch-reports workflow mints a separate token to post comments back on
the mathlib4 PR — each side needs its own token-minting capability.)

### 3. Token minting in workflows

Use `actions/create-github-app-token@v2`. Each side requests a token scoped to
the *other* repo:

```yaml
# In mathlib4 workflow:
- uses: actions/create-github-app-token@v2
  id: app_token
  with:
    app-id: ${{ vars.DOWNSTREAM_VALIDATOR_APP_ID }}
    private-key: ${{ secrets.DOWNSTREAM_VALIDATOR_PRIVATE_KEY }}
    owner: leanprover-community
    repositories: hopscotch-reports
```

```yaml
# In hopscotch-reports workflow:
- uses: actions/create-github-app-token@v2
  id: app_token
  with:
    app-id: ${{ vars.DOWNSTREAM_VALIDATOR_APP_ID }}
    private-key: ${{ secrets.DOWNSTREAM_VALIDATOR_PRIVATE_KEY }}
    owner: leanprover-community
    repositories: mathlib4
```

This narrows each token to a single repo even though the App is installed on
both — least-privilege for each leg of the round trip.

## Mathlib4 side

### File: `.github/workflows/pr_check_downstream.yml`

```yaml
name: PR check downstream

on:
  issue_comment:
    types: [created]
  workflow_dispatch:
    inputs:
      pr_number:
        required: true
        type: string
      downstreams:
        required: true
        type: string  # comma-separated

permissions:
  contents: read
  pull-requests: write   # for the ack comment
  issues: write          # PR comments use issues API

concurrency:
  group: pr-check-downstream-${{ github.event.issue.number || inputs.pr_number }}
  cancel-in-progress: false   # do not cancel a running build on a new comment;
                              # the dispatch is cheap and we want both runs.

jobs:
  trigger:
    if: |
      github.event_name == 'workflow_dispatch' ||
      (github.event.issue.pull_request &&
       startsWith(github.event.comment.body, '/check-downstream'))
    runs-on: ubuntu-latest
    steps:
      - name: Authorize commenter
        if: github.event_name == 'issue_comment'
        env:
          ASSOC: ${{ github.event.comment.author_association }}
        run: |
          case "$ASSOC" in
            OWNER|MEMBER|COLLABORATOR) echo "ok" ;;
            *) echo "::error::author_association=$ASSOC not allowed"; exit 1 ;;
          esac

      - name: Parse comment
        id: parse
        if: github.event_name == 'issue_comment'
        env:
          BODY: ${{ github.event.comment.body }}
        run: |
          # First line only; trim leading slash; remainder is comma-separated.
          line="$(printf '%s' "$BODY" | head -n1 | tr -d '\r')"
          rest="${line#/check-downstream}"
          rest="$(echo "$rest" | sed -E 's/^[[:space:]]+//;s/[[:space:]]+$//')"
          if [ -z "$rest" ]; then
            echo "::error::usage: /check-downstream <name>[, <name>...] | all"
            exit 1
          fi
          echo "downstreams=$rest" >> "$GITHUB_OUTPUT"

      - name: Get mathlib-ci
        uses: ./workflow-actions/.github/actions/get-mathlib-ci

      - name: Validate downstreams against inventory
        id: validate
        env:
          DS: ${{ steps.parse.outputs.downstreams || inputs.downstreams }}
          ASSOC: ${{ github.event.comment.author_association }}
        run: |
          "$CI_SCRIPTS_DIR/pr_check_downstream/validate_names.sh" \
            --names "$DS" \
            --author-association "$ASSOC" \
            --output "$GITHUB_OUTPUT"
          # Sets: resolved_names (final comma list), head_repo, head_sha,
          #       merge_sha (resolved from refs/pull/N/merge).

      - name: Mint App token (for hopscotch-reports)
        id: app_token
        uses: actions/create-github-app-token@v2
        with:
          app-id: ${{ vars.DOWNSTREAM_VALIDATOR_APP_ID }}
          private-key: ${{ secrets.DOWNSTREAM_VALIDATOR_PRIVATE_KEY }}
          owner: leanprover-community
          repositories: hopscotch-reports

      - name: Dispatch hopscotch-reports workflow
        env:
          GH_TOKEN: ${{ steps.app_token.outputs.token }}
          PR_NUMBER: ${{ github.event.issue.number || inputs.pr_number }}
          HEAD_REPO: ${{ steps.validate.outputs.head_repo }}
          MERGE_SHA: ${{ steps.validate.outputs.merge_sha }}
          DOWNSTREAMS: ${{ steps.validate.outputs.resolved_names }}
        run: |
          gh workflow run mathlib-pr-validation.yml \
            -R leanprover-community/hopscotch-reports \
            -f pr_number="$PR_NUMBER" \
            -f head_repo="$HEAD_REPO" \
            -f merge_sha="$MERGE_SHA" \
            -f downstreams="$DOWNSTREAMS"

      - name: Post / update ack comment
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          PR_NUMBER: ${{ github.event.issue.number || inputs.pr_number }}
          DOWNSTREAMS: ${{ steps.validate.outputs.resolved_names }}
          MERGE_SHA: ${{ steps.validate.outputs.merge_sha }}
        run: |
          "$CI_SCRIPTS_DIR/pr_check_downstream/post_ack_comment.sh"
```

The default `secrets.GITHUB_TOKEN` is sufficient for posting the ack comment on
the same repo — we only need the App token for the cross-repo dispatch.

### Mathlib-ci scripts

New directory `mathlib-ci/scripts/pr_check_downstream/`:

- `validate_names.sh` — fetches the inventory from
  `https://raw.githubusercontent.com/leanprover-community/hopscotch-reports/main/ci/inventory/downstreams.json`,
  validates each name, expands `all` to the enabled set (gated to `OWNER` /
  `MEMBER` only — `COLLABORATOR` cannot use `all`), resolves `refs/pull/N/merge`
  via `gh api`, emits outputs.
- `post_ack_comment.sh` — finds an existing comment by hidden marker
  `<!-- pr-check-downstream:ack -->` (using `gh api` to list comments) and
  edits it; otherwise creates one. Body shape:

  > **Downstream validation triggered**
  > Testing this PR (merge ref `<short-sha>`) against: `FLT`, `Toric`.
  > Run: <link>
  > Results will be posted as a separate comment per downstream.
  > <!-- pr-check-downstream:ack -->

These scripts live in `mathlib-ci` (not `mathlib4`) because they touch tokens
and post comments; that is the documented split (see the `PR_summary.yml`
reminder language in mathlib4).

## Hopscotch-reports side

### File: `.github/workflows/mathlib-pr-validation.yml`

```yaml
name: mathlib-pr-validation

on:
  workflow_dispatch:
    inputs:
      pr_number:
        required: true
        type: string
      head_repo:
        description: "owner/repo that the PR head lives in (supports forks)"
        required: true
        type: string
      merge_sha:
        description: "Resolved SHA of refs/pull/N/merge."
        required: true
        type: string
      downstreams:
        description: "Comma-separated downstream names."
        required: true
        type: string

permissions:
  contents: read
  actions: read

concurrency:
  group: mathlib-pr-validation-${{ inputs.pr_number }}-${{ inputs.merge_sha }}
  cancel-in-progress: true   # if a new push lands and a fresh dispatch comes
                             # for the same PR, drop the older one

jobs:
  plan:
    runs-on: ubuntu-latest
    outputs:
      matrix: ${{ steps.plan.outputs.matrix }}
    steps:
      - uses: actions/checkout@v6
      - id: plan
        env:
          INPUT_DOWNSTREAMS: ${{ inputs.downstreams }}
        run: |
          python3 scripts/pr_validation/build_matrix.py \
            --inventory ci/inventory/downstreams.json \
            --names "$INPUT_DOWNSTREAMS" \
            --output matrix.json
          echo "matrix=$(tr -d '\n' < matrix.json)" >> "$GITHUB_OUTPUT"

  validate:
    needs: plan
    name: "validate: ${{ matrix.name }}"
    runs-on: [self-hosted, pr]
    strategy:
      fail-fast: false
      max-parallel: 2
      matrix: ${{ fromJson(needs.plan.outputs.matrix) }}
    timeout-minutes: ${{ vars.PR_VALIDATION_TIMEOUT_MINUTES || 90 }}
    steps:
      - uses: actions/checkout@v6   # hopscotch-reports itself, for scripts
      - name: Set up Python
        uses: actions/setup-python@v6.2.0
        with:
          python-version: '3.x'
      - run: pip install -r scripts/requirements.txt

      - name: Install Lean (elan)
        run: |
          curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf \
            | sh -s -- -y --default-toolchain none
          echo "$HOME/.elan/bin" >> "$GITHUB_PATH"

      - name: Build / fetch lakedit
        env:
          HOPSCOTCH_REF: ${{ vars.HOPSCOTCH_REF || 'v1.4.1' }}
          TOOL_BIN: ${{ runner.temp }}/tool-bin
        run: bash scripts/pr_validation/install_lakedit.sh

      - name: Run validation
        id: run
        env:
          PR_NUMBER:    ${{ inputs.pr_number }}
          HEAD_REPO:    ${{ inputs.head_repo }}
          MERGE_SHA:    ${{ inputs.merge_sha }}
          DOWNSTREAM:   ${{ matrix.name }}
          DOWNSTREAM_REPO: ${{ matrix.repo }}
          DEFAULT_BRANCH: ${{ matrix.default_branch }}
          DEPENDENCY_NAME: ${{ matrix.dependency_name }}
          WORKDIR:      ${{ runner.temp }}/pr-validation
          OUTPUT_DIR:   ${{ runner.temp }}/artifacts/${{ matrix.name }}
          TOOL_BIN:     ${{ runner.temp }}/tool-bin
        run: bash scripts/pr_validation/validate.sh

      - name: Upload result
        if: always()
        uses: actions/upload-artifact@v7.0.0
        with:
          name: result-${{ matrix.name }}
          path: ${{ runner.temp }}/artifacts/${{ matrix.name }}
          if-no-files-found: error

  report:
    needs: [plan, validate]
    if: always()
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: actions/download-artifact@v8.0.1
        with:
          path: results
          pattern: result-*

      - name: Mint App token (for mathlib4)
        id: app_token
        uses: actions/create-github-app-token@v2
        with:
          app-id: ${{ vars.DOWNSTREAM_VALIDATOR_APP_ID }}
          private-key: ${{ secrets.DOWNSTREAM_VALIDATOR_PRIVATE_KEY }}
          owner: leanprover-community
          repositories: mathlib4

      - name: Post / update result comments
        env:
          GH_TOKEN:   ${{ steps.app_token.outputs.token }}
          PR_NUMBER:  ${{ inputs.pr_number }}
          MERGE_SHA:  ${{ inputs.merge_sha }}
          RUN_URL:    ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: python3 scripts/pr_validation/post_results.py --results-dir results
```

### Hopscotch-reports scripts

New directory `hopscotch-reports/scripts/pr_validation/`:

- `build_matrix.py` — load inventory, intersect with requested names, emit
  `{ "include": [ {name, repo, default_branch, dependency_name}, ... ] }`.
  Errors if any requested name is unknown (defense in depth — mathlib4 already
  validated, but the dispatch is untrusted input).
- `install_lakedit.sh` — clones `leanprover-community/hopscotch` at the pinned
  ref into `$WORKDIR/hopscotch`, runs `lake build lakedit`, copies the binary
  to `$TOOL_BIN/lakedit`. Caches by hopscotch SHA in a stable path so reruns
  on the same self-hosted runner are fast.
- `validate.sh` — see next section.
- `post_results.py` — reads each `result.json`, finds existing per-downstream
  comments on the PR (marker `<!-- pr-check-downstream:result:<name> -->`),
  edits in place or creates new. Uploads `build.log` artifacts and links them.

### `validate.sh` body

```bash
set -euo pipefail

mkdir -p "$WORKDIR" "$OUTPUT_DIR"
RESULT="$OUTPUT_DIR/result.json"
LOG="$OUTPUT_DIR/build.log"

emit() {  # status, stage, message
  python3 -c '
import json, os, sys
json.dump({"status": sys.argv[1], "stage": sys.argv[2], "message": sys.argv[3],
           "downstream": os.environ["DOWNSTREAM"],
           "merge_sha": os.environ["MERGE_SHA"]},
          open(os.environ["RESULT"], "w"))
' "$1" "$2" "$3"
}

# ---- 1. Clone mathlib4 at the merge SHA --------------------------------------
ML="$WORKDIR/mathlib4"
rm -rf "$ML"
git clone --no-checkout "https://github.com/$HEAD_REPO.git" "$ML" \
  || { emit infra_failure clone "could not clone $HEAD_REPO"; exit 1; }
git -C "$ML" fetch origin "$MERGE_SHA" \
  || { emit infra_failure fetch "merge SHA $MERGE_SHA not fetchable; PR may have conflicts"; exit 1; }
git -C "$ML" checkout --detach "$MERGE_SHA"

# ---- 2. Warm the olean cache --------------------------------------------------
( cd "$ML" && lake exe cache get ) >> "$LOG" 2>&1 || true   # best-effort

# ---- 3. Clone the downstream --------------------------------------------------
DS="$WORKDIR/downstream"
rm -rf "$DS"
git clone --depth=1 --branch "$DEFAULT_BRANCH" \
  "https://github.com/$DOWNSTREAM_REPO.git" "$DS" \
  || { emit infra_failure clone_downstream "could not clone $DOWNSTREAM_REPO"; exit 1; }

# ---- 4. lakedit set <dep> --path ---------------------------------------------
"$TOOL_BIN/lakedit" set "$DEPENDENCY_NAME" --path "$ML" --project-dir "$DS" \
  >> "$LOG" 2>&1 \
  || { emit infra_failure lakedit "lakedit failed; see log"; exit 1; }

# ---- 5. lake update + lake build ---------------------------------------------
( cd "$DS" && lake update "$DEPENDENCY_NAME" ) >> "$LOG" 2>&1 \
  || { emit infra_failure lake_update "lake update failed; see log"; exit 1; }

if ( cd "$DS" && lake build ) >> "$LOG" 2>&1; then
  emit pass build "downstream builds against PR merge ref"
else
  emit fail build "lake build failed; see log"
  # exit 0 — the failure is the meaningful result, not an infra error.
fi
```

`status` is one of `pass`, `fail`, `infra_failure`. The reporter renders these
distinctly so users can tell "your PR breaks FLT" from "the runner couldn't
clone FLT."

## Result comment shape

One comment per downstream, edited in place by marker:

```
### ✅ FLT — builds against this PR

Tested merge ref `abcd123` against `FLT@main`.
[full build log](artifact link) · [run](actions link)

> ⚠️ This run did not baseline against master. If master is currently broken
> for this downstream, the failure may not be attributable to this PR. See the
> [latest downstream report](…) for downstream health.
> *(TODO: auto-include master baseline.)*

<!-- pr-check-downstream:result:FLT -->
```

Failure variant leads with `❌ FLT — fails against this PR` and a 30-line tail
of the lake build log inlined in a `<details>` block.

Infra-failure variant leads with `⚠️ FLT — could not validate (infra)` and the
stage that broke (clone / fetch / lakedit / lake update). These do not imply
anything about the PR.

## Parameters / extension points

Wire these from day one, defaulting to what we want today:

| Surface          | Variable                       | Default                                 | Where    |
|------------------|--------------------------------|-----------------------------------------|----------|
| Inventory URL    | `INVENTORY_URL`                | hopscotch-reports `main` raw URL        | mathlib4 |
| Hopscotch ref    | `HOPSCOTCH_REF`                | `v1.4.1` (a tag)                        | hopscotch-reports |
| Runner label     | matrix `runs-on`               | `[self-hosted, pr]`                     | hopscotch-reports |
| Build timeout    | `PR_VALIDATION_TIMEOUT_MINUTES`| `90`                                    | hopscotch-reports |
| Comment marker   | `comment_marker_prefix`        | `pr-check-downstream`                   | both     |
| Allowed authors  | hardcoded list in auth step    | `OWNER`/`MEMBER`/`COLLABORATOR`         | mathlib4 |
| `all`-allowed    | hardcoded list in auth step    | `OWNER`/`MEMBER`                        | mathlib4 |

Use `vars.*` (repo variables) for everything that's not a secret; `secrets.*`
only for the App private key.

## Comment grammar

Single line, first line of the comment:

```
/check-downstream <name>[, <name>...]
/check-downstream all
```

- Names are matched case-sensitively against `inventory.downstreams[*].name`.
- `all` expands to all `enabled: true` entries; gated to OWNER/MEMBER.
- Empty argument list errors with usage hint.
- Anything else on the comment is ignored — the comment can carry context
  text after the directive line.

## Rollout plan

1. Create the App, install on both repos, store secrets/vars.
2. Land `scripts/pr_validation/` in hopscotch-reports + the
   `mathlib-pr-validation.yml` workflow on a feature branch. Smoke-test by
   running `gh workflow run mathlib-pr-validation.yml -f ...` from a personal
   token (no mathlib4 changes yet) against a known-good downstream.
3. Land `scripts/pr_check_downstream/` in mathlib-ci.
4. Land `pr_check_downstream.yml` in mathlib4 on a feature branch. Smoke-test
   by self-commenting on a draft PR.
5. Once green on a single downstream, allow `all` for OWNER/MEMBER.
6. Document the comment grammar in mathlib4's CONTRIBUTING (separate PR).

## Future work / TODOs

- **Master baseline.** Run a parallel build of the same downstream against
  current master and surface a clear "this PR is responsible" / "master is
  also broken" verdict in the comment. Largest open item.
- **Hopscotch / lakedit binary release.** Replace `install_lakedit.sh`'s
  build-from-source path with a release-asset download, mirroring how the
  hopscotch binary is fetched in the existing workflows.
- **Cache reuse.** Persist `$WORKDIR/mathlib4/.lake` across runs on the same
  self-hosted runner to amortize the lake-build cost when the merge SHA's
  oleans are partially cache-hits.
- **Status check.** Optionally surface results as a commit-status check, gated
  by a label so it's opt-in per-PR.
- **DB persistence.** Record PR runs in the hopscotch-reports DB so trends
  ("this PR broke X downstreams") are queryable. Requires a new schema branch.
- **Comment cleanup on close.** Collapse / strike through stale result
  comments when a PR is merged or force-pushed.

## A note on directories
Always recall that the local ~/hopscotch-reports is actually pointing to leanprover-community/downstream-reports. The name of the repository in strings should always be "downstrream-reports"