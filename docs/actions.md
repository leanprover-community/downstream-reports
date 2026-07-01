# Composite actions

This repo publishes four composite actions for downstream Lean projects to
consume:

- `bump-to-latest` — read the snapshot's target commit and bump the dependency
  pin (with a `lake build` sanity check).
- `open-bump-pr` — commit working-tree changes onto a dedicated branch and
  create or update a PR.
- `query-latest` — lightweight read-only lookup of the snapshot.
- `track-incompatibility` — open and maintain a persistent GitHub issue (and
  optionally a fix PR) tracking the current `first-known-bad` regression.

For the common case — a scheduled job that bumps a dependency, opens a PR, and
tracks any incompatibility — compose `bump-to-latest` + `open-bump-pr` plus a
`track-incompatibility` job. See the [canonical example](#canonical-example)
below.

> **Extensibility note.** The actions support any upstream/downstream pair, not
> just mathlib. When adding features, avoid mathlib-specific assumptions: anything
> that varies per-upstream (dependency name, repo) should be an explicit input with
> a sensible default, not a hardcoded constant.

---

## Set up authentication

> [!NOTE]
> The composite actions push branches, create / update PRs, and (for
> `track-incompatibility`) open issues. They all need a token with the right
> scopes. There are two ways to provide one — the built-in `GITHUB_TOKEN` is
> the simplest, but a GitHub App is recommended when you want your
> downstream's own CI to run on the bump PRs without a per-PR approval click.

### Option A — Default `GITHUB_TOKEN` (simplest, with a CI caveat)

GitHub Actions injects a `GITHUB_TOKEN` into every workflow run; the composite
actions pick it up via the `token` input's default (`${{ github.token }}`).
Two configuration steps must be completed first:

1. **Grant permissions in the workflow.** Add a `permissions:` block at job
   (or workflow) scope. For `bump-to-latest` + `open-bump-pr`:

   ```yaml
   permissions:
     contents: write
     pull-requests: write
   ```

   When using `track-incompatibility` with the issue side enabled, also add
   `issues: write`.

2. **Enable PR creation at the repo level.** GitHub blocks Actions from
   opening pull requests by default. Toggle this on under **Settings →
   Actions → General → Workflow permissions** by ticking **"Allow GitHub
   Actions to create and approve pull requests"**. Without it, `open-bump-pr`
   (and `track-incompatibility`'s fix-PR side) fail with
   `GitHub Actions is not permitted to create or approve pull requests`. This
   setting is also org-level: an org-wide block overrides the repo toggle.

With those two boxes ticked, the [canonical example](#canonical-example) works
as-is.

> [!WARNING]
> **Bump PRs opened with `GITHUB_TOKEN` don't run your downstream's CI until a
> maintainer approves them.**
>
> To prevent runaway recursion, GitHub holds the workflow runs (`push`,
> `pull_request`, …) on PRs opened under the default `GITHUB_TOKEN`: the PR
> shows up authored by `github-actions[bot]` with its `pull_request` / `push`
> checks pending until a user with write access clicks **Approve and run
> workflows** on it.
>
> The bump and `lake build` are verified inside `bump-to-latest`'s own
> job, so the PR is safe to merge. If you rely on additional CI (lints,
> downstream-of-downstream tests, deploy previews, …) before merging, approve
> the run as above, or — to run CI automatically without a per-PR approval —
> open PRs under a GitHub App token
> ([Option B](#option-b--github-app-installation-token-recommended-for-ci-on-pr))
> instead.
>
> A commit pushed by a human (e.g. `git commit --allow-empty -m "run CI"`) also
> starts CI without an approval, and an empty commit is tree-identical, so
> `open-bump-pr`'s `tree_unchanged` short-circuit leaves it in place on the next
> run. On a `track-incompatibility` fix PR the fix commits you push already do
> this.

### Option B — GitHub App installation token (recommended for CI-on-PR)

A GitHub App acts as its own identity, so its pushes and PRs trigger
workflows the same way human commits would. One-time setup cost, but full CI
coverage on bump PRs and a stable bot identity.

1. **Create the App.** Settings → Developer settings → GitHub Apps → New
   GitHub App. Grant **repository** permissions:
   - `Contents: Read and write`
   - `Pull requests: Read and write`
   - `Issues: Read and write` — only if you use `track-incompatibility`'s
     issue side
   - `Metadata: Read-only` — auto-granted

   No webhooks or user-level permissions are needed.

2. **Install it** on the downstream repo (or on the owning org, scoped to
   the repo). From the App's settings page:

   1. Open the **Install App** tab in the left sidebar.
   2. Click **Install** next to the account that owns the downstream repo
      (your user account or the org).
   3. Choose **Only select repositories** and pick the downstream repo
      (recommended) or **All repositories** if the App will service multiple repos.
   4. Confirm. GitHub takes you to the installation's settings page, whose
      URL ends in `/installations/<installation-id>`. You don't need to copy
      this ID: `actions/create-github-app-token` resolves it from the App ID
      plus the target repo at runtime.

   GitHub's reference: [Installing your own GitHub App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app).

3. **Store the credentials.** Extract two pieces of data from the App's
   settings page:

   1. **App ID** — shown near the top of the **General** tab as a small
      numeric value. Save it as a repository (or org) **variable**
      named `MY_BOT_APP_ID` (Settings → Secrets and variables → Actions →
      Variables → New repository variable). Use a variable, not a secret: App
      IDs are not sensitive, and variables let you reference them via
      `${{ vars.MY_BOT_APP_ID }}` in logs without masking.
   2. **Private key** — scroll down to **Private keys** on the same General
      tab and click **Generate a private key**. A `.pem` file downloads
      once; GitHub does not retain it. Copy its full contents (including the
      `-----BEGIN/END RSA PRIVATE KEY-----` lines) and save it as a repository
      (or org) **secret** named `MY_BOT_PRIVATE_KEY` (Settings → Secrets and
      variables → Actions → Secrets → New repository secret). Then delete the
      local `.pem`. You can always generate a new key later if needed.

   GitHub's reference: [Managing private keys for GitHub Apps](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps).

4. **Create an installation token per run** with
   [`actions/create-github-app-token`](https://github.com/actions/create-github-app-token)
   and pass it to the composite actions via the `token` input:

   ```yaml
   jobs:
     bump:
       runs-on: ubuntu-latest
       permissions:
         contents: write
         pull-requests: write
       steps:
         - uses: actions/create-github-app-token@v1
           id: app-token
           with:
             app-id: ${{ vars.MY_BOT_APP_ID }}
             private-key: ${{ secrets.MY_BOT_PRIVATE_KEY }}

         - uses: actions/checkout@v6
           with:
             token: ${{ steps.app-token.outputs.token }}

         - name: Bump to latest
           id: bump
           uses: leanprover-community/downstream-reports/.github/actions/bump-to-latest@main

         - name: Open or update PR
           if: steps.bump.outputs.updated == 'true'
           uses: leanprover-community/downstream-reports/.github/actions/open-bump-pr@main
           with:
             title:           ${{ steps.bump.outputs.pr-title }}
             message:         ${{ steps.bump.outputs.bump-description }}
             commit-message:  ${{ steps.bump.outputs.commit-message }}
             token:           ${{ steps.app-token.outputs.token }}
             git-user-name:   my-bot[bot]
             git-user-email:  ${{ vars.MY_BOT_APP_ID }}+my-bot[bot]@users.noreply.github.com
   ```

   The same `token: ${{ steps.app-token.outputs.token }}` works on
   `track-incompatibility`. That action splits PR-side and issue-side
   credentials (`token` vs `issue-token`) — usually only `token` needs the
   App, since `issues: write` granted via the workflow `permissions:` block
   is not subject to the repo-wide PR-creation override.

The "Allow GitHub Actions to create and approve pull requests" repo toggle
is not required when using an App token — it only governs the default
`GITHUB_TOKEN`.

#### Personal access token (PAT)

A classic or fine-grained PAT can substitute for the App token: store
it as a secret and pass it via the same `token` input. Two trade-offs:

- It's bound to a human user, so that user authors the PRs and is shown as the
  actor triggering subsequent CI runs.
- It counts against that user's per-hour API rate limit, shared with all their
  other GitHub activity.

Apps are preferred for shared / organisational repos and for anything
long-lived; PATs are fine for personal sandboxes.

---

## Canonical example

A two-job workflow that opens an LKG-bump PR (so the project can advance to
the latest compatible dependency commit at any time) and, when a regression
is reported, a separate fix PR pinned at the FKB (so the breaking change
can be worked on in parallel). The two PRs live on different branches without
conflict. The LKG PR is mergeable immediately to keep the project moving
while the fix lands later.

```yaml
name: Update Dependencies

on:
  schedule:
    - cron: "0 */6 * * *"   # Check for updates every six hours 
  workflow_dispatch:

jobs:
  bump:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v6

      - name: Bump to latest mathlib
        id: bump
        uses: leanprover-community/downstream-reports/.github/actions/bump-to-latest@main

      - name: Open or update PR
        if: steps.bump.outputs.updated == 'true'
        uses: leanprover-community/downstream-reports/.github/actions/open-bump-pr@main
        with:
          title:          ${{ steps.bump.outputs.pr-title }}
          message:        ${{ steps.bump.outputs.bump-description }}
          commit-message: ${{ steps.bump.outputs.commit-message }}

  open-issue:
    runs-on: ubuntu-latest
    permissions:
      issues: write
      contents: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v6

      - name: Open or update incompatibility issue and PR
        uses: leanprover-community/downstream-reports/.github/actions/track-incompatibility@main
```

For just the LKG bump (no incompatibility tracking), drop the `open-issue`
job. To suppress the LKG PR while a regression is active (so the only open
PR is the FKB fix one), gate the `bump` job on `query-latest`'s
`first-known-bad` output and add a step that closes the LKG PR when
`track-incompatibility`'s `pr-number` output is non-empty.

---

## Sub-daily cron cadence

The actions are idempotent on unchanged input, so the workflow above can run on
a sub-daily cron (e.g. `"0 */2 * * *"`, every two hours) without producing
PR / issue / CI noise on ticks where the snapshot hasn't moved. Two layered
short-circuits prevent waste:

- **`bump-to-latest`** probes the LKG bump-PR branch (`hopscotch/lkg-bump`,
  matching `open-bump-pr`'s default) before running hopscotch. If that branch's
  manifest already pins the dependency at the snapshot's target, the action
  exits with `skipped=true` and skips `lake build`. Auto-disabled for
  `query-type: first-known-bad` (FKB lives on per-FKB-SHA branches handled
  by `track-incompatibility`).
- **`open-bump-pr`** compares the local commit's tree against the remote bump
  branch's HEAD tree. If they're identical and an open PR points at
  the branch, the force-push and `gh pr edit` are both skipped (surfaced via
  `action: up-to-date`).

A caller using a non-default branch on `open-bump-pr` must pass the **same**
value to `bump-to-latest`'s `branch` input. Otherwise the probe watches the
wrong branch and the bump rebuilds on every run while a PR is open.

---

## `bump-to-latest`

**Path:** `.github/actions/bump-to-latest`

Fetches the snapshot, reads the current mathlib pin from
`lake-manifest.json`, and when a bump is needed, installs elan + hopscotch and
runs `hopscotch dep` to bump and build. On success the working tree contains
modified `lakefile` and `lake-manifest.json` ready to commit.

Also fetches human-readable commit metadata from the GitHub API to generate a
suggested PR title, body snippet, and git commit message — pass
`generate-description: 'false'` to skip these API calls.

**Release-tag pinning.** When a bump lands exactly on a release tag (the target
commit equals the snapshot's `last_good_release_commit`), the action pins the
dependency to the tag name rather than the SHA: `lake-manifest.json`'s
`inputRev` becomes e.g. `v4.32.0` (the resolved `rev` stays the SHA, so the pin
is still reproducible), and the PR title/body name the release. `last-good-release`
bumps already pin a tag this way.

**Backwards-move guardrail.** Because the published snapshot can lag a
downstream's manifest, the recorded target commit may be *older* than the
project's current pin (the project already advanced past it). Before
building, the action queries the upstream repo's compare API and refuses to
build unless the target is a forward move (a descendant of the current pin).
A transient or inconclusive compare API call is a warning, not a hard stop.
This protects every consumer of `bump-to-latest`, including the fix-PR side of
`track-incompatibility`.

How a non-forward target (`behind`/`diverged`) is handled depends on
`query-type`, since the *meaning* differs:

- `last-known-good` / `first-known-bad` → **hard fail**. A snapshot older than
  the project's pin is a genuine anomaly worth surfacing.
- `last-good-release` → **clean skip** (`skipped=true`, `updated=false`, step
  succeeds). A release tag behind the pin means the project is already past the
  latest release — the normal state after a latest-commit bump has been merged.
  This lets a scheduled `last-good-release` bump run without a caller-side
  forward-move guard, skipping quietly until a newer release lands.

The upstream repo for the compare call and commit-description lookups is
taken from the snapshot's top-level `upstream` field — no configuration needed.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | no | `${{ github.repository }}` | Downstream name key or repo slug (`owner/repo`). Auto-detected by presence of `/`. Defaults to the repository running this action. |
| `project-dir` | no | `.` | Path to the downstream project root |
| `dependency-name` | no | `mathlib` | Name of the dependency in the lakefile |
| `hopscotch-version` | no | `v2.0.0-beta` | Hopscotch release tag to download. `v2.0.0-beta`+ is required for the `fix` subcommand (used by `apply-fixes`); older tags lack it and the apply step degrades to a rev-bump-only PR. |
| `skip-build` | no | `false` | Set to `true` to only run `lake update` (pin lakefile + manifest) and skip the build. `build-failed` is always `false`; the bump succeeds (`updated=true`) only when `lake update` succeeds. If `lake update` fails the step fails so callers don't commit a half-baked tree. Used by the FKB fix-PR path. |
| `generate-description` | no | `true` | Set to `false` to skip GitHub API calls; `pr-title`, `bump-description`, and `commit-message` will be empty |
| `query-type` | no | `last-known-good` | Which commit to bump to: `last-known-good`, `first-known-bad`, or `last-good-release` (semver tag, e.g. `v4.13.0`) |
| `branch` | no | `hopscotch/lkg-bump` | Bump-PR branch the Step 1.5 probe checks for an already-applied bump. Must match the `branch` passed to `open-bump-pr`, or the probe watches the wrong branch. Unused for `query-type: first-known-bad`. |
| `apply-fixes` | no | `false` | After the bump, run `hopscotch fix apply` so the PR carries the fixes hopscotch recorded, not just the rev bump. When enabled, applies everything hopscotch proposes — the failure-boundary fixes (for an FKB bump, overlaid from the regression probe's published wide-range bisection; an LKG bump is green so it has none) and the deprecation advisories; set `no-advisories` to restrict it to the boundary fixes. Runs on any `query-type`. **Best-effort:** if no fixes were recorded, the installed `hopscotch-version` lacks the `fix` subcommand, or the apply fails, the bump proceeds with the rev bump alone (validated by the PR's own CI). Off by default; the fixes publish to the snapshot regardless of this flag, so set it `true` per downstream to apply them. |
| `no-advisories` | no | `false` | Pass `--no-advisories` to `hopscotch fix apply`, restricting it to the failure-boundary fixes and skipping the deprecation advisories — changes that build today but break at the upstream cleanup (mirrors hopscotch's own flag, which applies them by default). Set this to keep an LKG bump "mergeable as-is": advisory changes aren't covered by the bump's green build. Advisories are read from the bump's **own** `results.json` (commit-specific: detected at the commit bumped to), so they're complete only when that bump built; `skip-build` finds only the statically-resolved subset; partial ones are skipped by hopscotch. No effect when `apply-fixes` is false. |

### Outputs

| Output | Description |
|--------|-------------|
| `rev` | The human-readable ref passed to hopscotch: a tag name for `last-good-release`, or for a `last-known-good` bump that lands exactly on a release tag; a SHA otherwise |
| `commit` | The resolved commit SHA |
| `current-pin` | The commit the project was pinned to before this action ran |
| `updated` | `"true"` if hopscotch produced a committable bump. The step **fails** instead of returning `updated=false` whenever hopscotch stops at a stage that leaves nothing committable, e.g. a `lake update` (bump-step) failure, which rewrites the lakefile but leaves `lake-manifest.json` stale. |
| `skipped` | `"true"` if the project was already at the target commit (or target is empty) |
| `build-failed` | `"true"` only for the expected case: a `first-known-bad` bump whose `lake build` (verify) stage failed after `lake update` succeeded (`failureStage = "lake build"` in hopscotch's `results.json`). Any other failure, including `lake update` failure, fails the step rather than returning here. |
| `pr-title` | Suggested PR title (empty when skipped or `generate-description: false`) |
| `bump-description` | Markdown paragraph describing the bump — new commit + previous pin, with subjects and dates. Pass to `open-bump-pr`'s `message` input. Empty when skipped or `generate-description: false`. |
| `commit-message` | Suggested git commit message (empty when skipped or `generate-description: false`) |
| `fix-summary` | hopscotch's own `fix apply` output, verbatim, when it changed files after the bump — empty when nothing was applied, the step was skipped, or no fixes were recorded. Pass to `open-bump-pr`'s `fix-summary` input to describe the applied fixes in the PR body using the tool's own words. |

---

## `open-bump-pr`

**Path:** `.github/actions/open-bump-pr`

Generic commit-and-PR action. Independent of `bump-to-latest` and works with
any working-tree changes.

Commits all working-tree changes onto a dedicated branch (force-pushed on every
run to keep the PR to a single commit), then creates or updates an open PR. The
auto-generated PR body is the optional `message` (the `bump-description` from
`bump-to-latest`), a `---` rule, a brief explanation that this is a verified
last-known-good bump that should be mergeable as-is, and a footer linking back to
the triggering run. When the PR is opened under the built-in `GITHUB_TOKEN` (no
App token), the body also includes a warning that downstream CI won't run until
a maintainer approves it, pointing at [Set up authentication](#set-up-authentication). Pass
`body` to override everything.

If there are no working-tree changes (`git diff` is clean) the action exits with
`action=noop` and no PR is touched.

To stay quiet under sub-daily cadences, the action also short-circuits when the
remote branch already has a commit whose **tree** matches the one it would push
and an open PR points at the branch. Both the force-push and PR title/body edit
are skipped, and the action exits with `action=up-to-date` (with the existing PR's
`pr-number` / `pr-url` surfaced). The fresh-SHA / same-tree commit that would
otherwise trigger PR CI on every tick never lands.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `branch` | no | `hopscotch/lkg-bump` | Branch for the PR. Force-pushed on every run — this keeps the PR to one commit and avoids accumulating bump history. |
| `base` | no | repo default branch | Base branch for the PR |
| `title` | no | `chore: dependency update` | PR title |
| `body` | no | `''` | Full PR body. When set, overrides the auto-generated message + footer entirely. |
| `commit-message` | no | `chore: dependency update` | Git commit message |
| `labels` | no | `''` | Comma-separated labels to apply to the PR |
| `message` | no | `''` | Content to place in the PR body above the automated footer. Pass the `bump-description` output from `bump-to-latest` here. |
| `fix-summary` | no | `''` | hopscotch's own `fix apply` output (the `fix-summary` output of `bump-to-latest`). When non-empty it is rendered verbatim in the body and the "mergeable as-is" framing is adjusted — the applied changes were *not* covered by the bump's green build, so CI should run before merge. Ignored when `body` is set. |
| `token` | no | `GITHUB_TOKEN` | Token used to push the branch and create/update the PR. Pass a GitHub App installation token to have PRs opened by a bot account instead of `github-actions[bot]`. |
| `git-user-name` | no | `github-actions[bot]` | `git user.name` for the bump commit. |
| `git-user-email` | no | `41898282+github-actions[bot]@users.noreply.github.com` | `git user.email` for the bump commit. |

### Outputs

| Output | Description |
|--------|-------------|
| `pr-number` | PR number (empty when `action=noop`; populated for `created` / `updated` / `up-to-date`) |
| `pr-url` | PR URL (empty when `action=noop`; populated for `created` / `updated` / `up-to-date`) |
| `action` | `"created"` (new PR opened), `"updated"` (existing PR's title/body edited after a force-push), `"up-to-date"` (remote branch already had an identical tree at HEAD with an open PR — no push, no edit), or `"noop"` (no working-tree changes vs HEAD) |

---

## `track-incompatibility`

**Path:** `.github/actions/track-incompatibility`

Opens or maintains a persistent tracking issue and (by default) a fix PR
with the lakefile bumped to the FKB commit, giving downstream maintainers a
ready starting point for investigation. When the FKB advances, stale fix PRs
are closed automatically. When the regression clears, both the issue and any
open fix PRs are closed with resolution comments.

Set `open-pr: false` to run in issue-only mode. Set `open-issue: false` for
PR-only mode, useful when you reserve issues for longer-lasting problems and
want incompatibilities surfaced only as fix PRs. Setting both to `false` puts
the action in read-only mode: it fetches and logs the current FKB/LKG and
last-run metadata but opens or closes nothing — handy for temporarily pausing
side effects without removing the job.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | no | `${{ github.repository }}` | Downstream name key or repo slug (`owner/repo`). |
| `label` | no | `dependency-incompatibility` | Label identifying the tracking issue. Created if missing. |
| `title` | no | auto | Full issue title; auto-generated when empty. |
| `close-on-resolve` | no | `true` | Close the tracking issue with a resolution comment when FKB clears. |
| `token` | no | `github.token` | Token for PR-side operations (push, fix-PR open/close). Needs `contents: write` and `pull-requests: write` when `open-pr: true`. Use a GitHub App token: PRs opened under the default `GITHUB_TOKEN` don't run downstream CI without a maintainer's per-PR approval, and PR creation can be disabled repo-wide. Ignored when `open-pr: false`. |
| `issue-token` | no | `github.token` | Token for issue-side operations (label, list, create, edit, comment, close). Defaults to `GITHUB_TOKEN`, which is reliable here because `issues: write` granted via the workflow's `permissions:` block isn't subject to repo-wide overrides. Override only to change the issue author. Ignored when `open-issue: false`.  |
| `open-issue` | no | `true` | Master switch for the tracking-issue side. Set `false` for PR-only mode (no issue is opened or closed). |
| `open-pr` | no | `true` | Master switch for the fix-PR side. Set `false` for issue-only mode. |
| `pr-label` | no | `dependency-incompatibility-fix` | Label applied to fix PRs; primary key for stale-PR detection. Created automatically. |
| `branch-prefix` | no | `bump-<dependency-name>/fix` | Prefix for the fix-PR branch. Final branch: `<prefix>-<fkb-short7>` (e.g. `bump-mathlib/fix-abc1234`). Stable per FKB SHA. |
| `project-dir` | no | `.` | Path to the downstream project root. Forwarded to `bump-to-latest`. |
| `dependency-name` | no | `mathlib` | Dependency name in the lakefile. Forwarded to `bump-to-latest`. |
| `apply-fixes` | no | `false` | Forwarded to `bump-to-latest`: when `true`, run `hopscotch fix apply` on the FKB bump so the fix PR carries hopscotch's fixes (plus advisories), not just the rev bump. Off by default; set it `true` per downstream to apply them. |
| `no-advisories` | no | `false` | Forwarded to `bump-to-latest`: with `apply-fixes` on, restrict it to the failure-boundary fixes (skip deprecation advisories). |
| `base` | no | repo default | Base branch for the fix PR. |
| `git-user-name` | no | `github-actions[bot]` | `git user.name` for the bump commit. |
| `git-user-email` | no | `41898282+github-actions[bot]@users.noreply.github.com` | `git user.email` for the bump commit. |
| `close-stale-prs` | no | `true` | Close fix PRs whose head branch no longer matches the current FKB branch. The branch is retained; WIP commits stay reachable. |
| `close-prs-on-resolve` | no | `true` | Close open fix PRs when the FKB clears. Independent of `close-on-resolve`. |

### Outputs

| Output | Description |
|--------|-------------|
| `issue-number` | Issue number (empty when `action=noop`) |
| `issue-url` | Issue URL |
| `action` | `"created"`, `"updated"`, `"closed"`, `"noop"`, or `"disabled"` (when `open-issue: false`) — describes the **issue** lifecycle |
| `pr-number` | Fix PR number (empty when `pr-action` is non-creating) |
| `pr-url` | Fix PR URL |
| `pr-action` | `"created"`, `"noop-existing"`, `"noop-no-changes"`, `"noop-resolved"`, or `"disabled"` (when `open-pr: false`) |

### Reusable workflow

**Path:** `.github/workflows/track-incompatibility.yml`

Thin wrapper around the composite action. Minimal usage (with PR side):

```yaml
name: Track mathlib regression

on:
  schedule:
    - cron: "0 19 * * *"
  workflow_dispatch:

jobs:
  track:
    uses: leanprover-community/downstream-reports/.github/workflows/track-incompatibility.yml@main
    permissions:
      contents: write
      issues: write
      pull-requests: write
```

Issue-only mode:

```yaml
jobs:
  track:
    uses: leanprover-community/downstream-reports/.github/workflows/track-incompatibility.yml@main
    permissions:
      issues: write
    with:
      open-pr: 'false'
```

PR-only mode (no tracking issue — reserve issues for longer-lasting problems):

```yaml
jobs:
  track:
    uses: leanprover-community/downstream-reports/.github/workflows/track-incompatibility.yml@main
    permissions:
      contents: write
      pull-requests: write
    with:
      open-issue: 'false'
```

Accepts the same inputs as the composite action and forwards them through.

---

## `open-incompatibility-issue` _(deprecated — use `track-incompatibility`)_
**Path:** `.github/actions/open-incompatibility-issue`

---

## `query-latest`

**Path:** `.github/actions/query-latest`

Lightweight read-only action. Fetches the snapshot and returns the target
commit for a downstream without cloning repos, installing elan, or running
hopscotch.

Accepts either a downstream **name key** (e.g. `physlib`) or a **repo slug**
(e.g. `leanprover-community/physlib`). Values containing `/` are treated as
repo slugs; all others as name keys. Defaults to `github.repository`, so a
downstream repo can use it with no inputs at all.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | no | `${{ github.repository }}` | Downstream name key or repo slug (`owner/repo`). Auto-detected by presence of `/`. |
| `query-type` | no | `last-known-good` | Which commit to return: `last-known-good`, `first-known-bad`, or `last-good-release` (empty when no active entry) |

### Outputs

| Output | Description |
|--------|-------------|
| `rev` | The human-readable ref (tag name for `last-good-release`, SHA for other query types) |
| `commit` | The resolved commit SHA (same as `rev` for non-release query types) |
| `downstream-name` | The downstream name key as registered in the snapshot |
| `repo` | GitHub repo slug (`owner/repo`) |
| `dependency-name` | The dependency name field from the snapshot entry |
| `upstream` | The upstream repo slug the snapshot tracks (top-level `upstream` field; empty on snapshots predating it) |

---

## Common patterns

The [canonical example](#canonical-example) above covers the recommended
two-job pattern. The snippets below show smaller pieces.

**Just the LKG bump (no incompatibility tracking):**

```yaml
permissions:
  contents: write
  pull-requests: write

jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6

      - name: Bump to latest
        id: bump
        uses: leanprover-community/downstream-reports/.github/actions/bump-to-latest@main

      - name: Open PR
        if: steps.bump.outputs.updated == 'true'
        uses: leanprover-community/downstream-reports/.github/actions/open-bump-pr@main
        with:
          title:          ${{ steps.bump.outputs.pr-title }}
          message:        ${{ steps.bump.outputs.bump-description }}
          commit-message: ${{ steps.bump.outputs.commit-message }}
```

**Read-only — just look up the target commit without building:**

```yaml
      - name: Get latest commit
        id: latest
        uses: leanprover-community/downstream-reports/.github/actions/query-latest@main

      - run: echo "LKG is ${{ steps.latest.outputs.commit }}"
```

**Check the first-known-bad commit during an active regression:**

```yaml
      - name: Get first-known-bad commit
        id: fkb
        uses: leanprover-community/downstream-reports/.github/actions/query-latest@main
        with:
          query-type: first-known-bad

      - run: echo "FKB is ${{ steps.fkb.outputs.commit }}"
```

**Bump to the latest compatible semver release tag (e.g. `v4.13.0`):**

```yaml
      - name: Look up latest compatible release
        id: release
        uses: leanprover-community/downstream-reports/.github/actions/query-latest@main
        with:
          query-type: last-good-release

      - run: |
          echo "Latest compatible mathlib release: ${{ steps.release.outputs.rev }}"
          echo "Resolves to commit: ${{ steps.release.outputs.commit }}"
```

When `query-type: last-good-release`:
- `rev` is a **tag name** (e.g. `v4.13.0`) that `hopscotch dep` and Lake's `inputRev` both accept directly.
- `commit` is the resolved SHA, useful for consumers needing byte-equality comparison against the `rev` field in `lake-manifest.json`.
- Both fields are empty when no semver release precedes the downstream's current LKG.

**With a GitHub App token** (so bump PRs trigger your downstream's CI and are
attributed to a bot account rather than `github-actions[bot]`) — see
[Authentication → Option B](#option-b--github-app-installation-token-recommended-for-ci-on-pr)
for setup details and an example.
