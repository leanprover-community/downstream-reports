# Reusable workflows and composite actions

This repo publishes two reusable workflows and four composite actions for
downstream Lean projects to consume.

For the common case — a scheduled job that bumps a dependency and opens a PR
— use the `bump-dependency-to-latest` reusable workflow directly (see below).
For tracking active incompatibilities in a persistent issue **and** opening a
ready-to-work fix PR, use `track-incompatibility` (the recommended option). 
The composite actions (`bump-to-latest`,
`open-bump-pr`, `query-latest`, `track-incompatibility`) are the building blocks for custom workflows
that need more control.

> **Extensibility note.** Today the actions are used exclusively for the
> mathlib upstream. The intent is to keep them general enough to support other
> upstream/downstream pairs in the future. When adding features, avoid baking
> in mathlib-specific assumptions: anything that varies per-upstream (dependency
> name, repo) should be an explicit input with a sensible default rather than a
> hardcoded constant.

---

## Sub-daily cron cadence

The actions are idempotent on unchanged input, so a downstream's `update.yml`
can run on a sub-daily cron (e.g. `0 */2 * * *`) without producing PR / issue /
CI noise on ticks where the snapshot hasn't moved. Two layered short-circuits
make this safe:

- **`bump-to-latest`** probes its `branch` input (the bump-PR branch) before
  running hopscotch. If that branch's manifest already pins the dependency at
  the snapshot's target, the action exits with `skipped=true` and never spends
  a `lake build`. Auto-disabled for `query-type: first-known-bad` (FKB lives
  on per-FKB-SHA branches handled by `track-incompatibility`).
- **`open-bump-pr`** compares the local commit's tree against the remote bump
  branch's HEAD tree. If they're identical and an open PR already points at
  the branch, the force-push and the `gh pr edit` are both skipped (the
  action surfaces this via `action: up-to-date`).

Adoption (using the reusable wrapper):

```yaml
on:
  schedule:
    - cron: "0 */2 * * *"   # every two hours
jobs:
  bump:
    uses: leanprover-community/downstream-reports/.github/workflows/bump-dependency-to-latest.yml@main
```

The wrapper threads its `branch` input to both `bump-to-latest`'s probe and
`open-bump-pr`'s push, so a single source of truth for the bump-PR branch is
maintained automatically.

Adoption (composing the actions directly): pass the **same** `branch` value to
both `bump-to-latest` and `open-bump-pr`. A mismatch silently degrades to "no
optimization" — the probe 404s on the wrong branch and the bump runs every
tick (correct, just wasteful). Sourcing the value from one `env:` constant is
the recommended pattern.

---

## `bump-dependency-to-latest` reusable workflow

**Path:** `.github/workflows/bump-dependency-to-latest.yml`

Wraps `bump-to-latest` + `open-bump-pr` into a single callable unit. This is the
recommended starting point for downstreams that just want a scheduled bump PR
with no boilerplate.

### Minimal usage

```yaml
name: Bump mathlib to latest

on:
  schedule:
    - cron: "0 18 * * *"   # adjust to taste
  workflow_dispatch:

jobs:
  bump:
    uses: leanprover-community/downstream-reports/.github/workflows/bump-dependency-to-latest.yml@main
    permissions:
      contents: write
      pull-requests: write
```

The `downstream` lookup defaults to `github.repository` (matched as a repo
slug), so no inputs are required as long as the repo is registered in the
inventory.

### Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `branch` | `hopscotch/lkg-bump` | Branch name for the bump PR. Force-pushed on every run. |
| `base` | repo default branch | Base branch for the PR |
| `labels` | — | Comma-separated labels to apply to the PR |
| `dependency-name` | `mathlib` | Dependency name in the lakefile |
| `hopscotch-version` | `v1.4.1` | Hopscotch release tag to download |
| `query-type` | `last-known-good` | Which commit to bump to: `last-known-good` or `first-known-bad` |

### Outputs

| Output | Description |
|--------|-------------|
| `pr-number` | PR number (empty when `action=noop`; populated for `created` / `updated` / `up-to-date`) |
| `pr-url` | PR URL (empty when `action=noop`; populated for `created` / `updated` / `up-to-date`) |
| `action` | `"created"`, `"updated"`, `"up-to-date"` (remote branch already had the same tree with an open PR — push and edit skipped), or `"noop"` |
| `updated` | `"true"` if hopscotch successfully bumped the project |
| `skipped` | `"true"` if the project was already at the target commit |
| `build-failed` | `"true"` if hopscotch ran but the build failed |

### Customised example

```yaml
jobs:
  bump:
    uses: leanprover-community/downstream-reports/.github/workflows/bump-dependency-to-latest.yml@main
    permissions:
      contents: write
      pull-requests: write
    with:
      branch: automation/lkg-bump
      labels: dependencies
```

**With a GitHub App token** (to open PRs as a bot account rather than `github-actions[bot]`):

Use the composite actions directly so the token stays within one job — GitHub masks step outputs automatically, but job outputs are plaintext and cannot safely carry a token.

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

      - name: Open PR
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

---

## `bump-to-latest`

**Path:** `.github/actions/bump-to-latest`

Fetches the snapshot, reads the current mathlib pin from
`lake-manifest.json`, and (when a bump is needed) installs elan + hopscotch and
runs `hopscotch dep` to bump and build. On success the working tree contains
modified `lakefile` and `lake-manifest.json` ready to commit.

Also fetches human-readable commit metadata from the GitHub API to generate a
suggested PR title, body snippet, and git commit message — pass
`generate-description: 'false'` to skip these API calls.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | no | `${{ github.repository }}` | Downstream name key or repo slug (`owner/repo`). Auto-detected by presence of `/`. Defaults to the repository running this action. |
| `project-dir` | no | `.` | Path to the downstream project root |
| `dependency-name` | no | `mathlib` | Name of the dependency in the lakefile |
| `hopscotch-version` | no | `v1.4.1` | Hopscotch release tag to download |
| `generate-description` | no | `true` | Set to `false` to skip GitHub API calls; `pr-title`, `bump-description`, and `commit-message` will be empty |
| `query-type` | no | `last-known-good` | Which commit to bump to: `last-known-good`, `first-known-bad`, or `last-good-release` (semver tag, e.g. `v4.13.0`) |
| `branch` | no | `hopscotch/lkg-bump` | The bump-PR's branch — same notion as `open-bump-pr`'s `branch`. Used here as a probe target: the action reads the dependency pin from the manifest at `<github.repository>@<branch>` (via `GITHUB_TOKEN`) and, if it already matches the target, exits with `skipped=true` and never invokes hopscotch (so no `lake build`). Default matches `open-bump-pr`'s default, so the typical caller pairing the two actions gets sub-daily-cadence idempotency for free — the ~15-min lake build is skipped while the bump PR awaits merge. **Must match the value passed to `open-bump-pr`'s `branch`** — a mismatch silently degrades to "no optimization" (the probe 404s on the wrong branch and the bump runs every tick). The reusable wrapper at `.github/workflows/bump-dependency-to-latest.yml` threads its single `branch` input through to both actions for you; direct callers should source it from one place (e.g. an `env:` constant). **Auto-disabled when `query-type=first-known-bad`** (FKB targets live on per-FKB-SHA branches handled by `track-incompatibility`). A 404 / missing manifest / missing dependency is treated as "no match" so the bump runs; an unexpected API failure also defaults to no-match but emits a `::warning::` annotation so the failure shows up in the run summary. |

### Outputs

| Output | Description |
|--------|-------------|
| `rev` | The human-readable ref passed to hopscotch (tag name for `last-good-release`, SHA otherwise) |
| `commit` | The resolved commit SHA |
| `current-pin` | The commit the project was pinned to before this action ran |
| `updated` | `"true"` if hopscotch successfully bumped the project |
| `skipped` | `"true"` if the project was already at the target commit (or target is empty) |
| `build-failed` | `"true"` if hopscotch ran but the build failed |
| `pr-title` | Suggested PR title (empty when skipped or `generate-description: false`) |
| `bump-description` | Markdown paragraph describing the bump — new commit + previous pin, with subjects and dates. Pass to `open-bump-pr`'s `message` input. Empty when skipped or `generate-description: false`. |
| `commit-message` | Suggested git commit message (empty when skipped or `generate-description: false`) |

---

## `open-bump-pr`

**Path:** `.github/actions/open-bump-pr`

Generic commit-and-PR action. Independent of `bump-to-latest` — works with any
working-tree changes.

Commits all working-tree changes onto a dedicated branch (force-pushed on every
run to keep the PR to a single commit), then creates or updates an open PR. The
PR body is an optional `message` followed by an automated footer that links back
to the triggering run and records today's date.

If there are no working-tree changes (`git diff` is clean) the action exits with
`action=noop` and no PR is touched.

To stay quiet under sub-daily cadences, the action also short-circuits when the
remote branch already has a commit whose **tree** matches the one it would push
*and* an open PR already points at the branch. In that case both the force-push
and the PR title/body edit are skipped, and the action exits with
`action=up-to-date` (with the existing PR's `pr-number` / `pr-url` surfaced).
The fresh-SHA / same-tree commit that would otherwise trigger PR CI on every
tick of an awaiting-merge bump PR therefore never lands.

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

Opens or maintains a persistent tracking issue **and** (by default) a fix PR
with the lakefile bumped to the FKB commit, so downstream maintainers have a
ready starting point for investigation.  When the FKB advances, stale fix PRs
are closed automatically.  When the regression clears, both the issue and any
open fix PRs are closed with resolution comments.

Set `open-pr: false` to run in issue-only mode, where no PR is opened.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | no | `${{ github.repository }}` | Downstream name key or repo slug (`owner/repo`). |
| `upstream` | no | `leanprover-community/mathlib4` | Upstream repo slug for commit metadata lookups. |
| `label` | no | `dependency-incompatibility` | Label identifying the tracking issue. Created if missing. |
| `title` | no | auto | Full issue title; auto-generated when empty. |
| `close-on-resolve` | no | `true` | Close the tracking issue with a resolution comment when FKB clears. |
| `token` | no | `github.token` | Token for PR-side operations (push, fix-PR open/close). Needs `contents: write` and `pull-requests: write` when `open-pr: true`. Typically a GitHub App token, since pushes by the default `GITHUB_TOKEN` do not trigger downstream CI and PR creation can be disabled repo-wide. Ignored when `open-pr: false`. |
| `issue-token` | no | `github.token` | Token for issue-side operations (label, list, create, edit, comment, close). Defaults to `GITHUB_TOKEN`, which is reliable here because `issues: write` granted via the workflow's `permissions:` block isn't subject to repo-wide overrides. Override only to change the issue author. |
| `open-pr` | no | `true` | Master switch for the fix-PR side. Set `false` for issue-only mode. |
| `pr-label` | no | `dependency-incompatibility-fix` | Label applied to fix PRs; primary key for stale-PR detection. Created automatically. |
| `branch-prefix` | no | `bump-<dependency-name>/fix` | Prefix for the fix-PR branch. Final branch: `<prefix>-<fkb-short7>` (e.g. `bump-mathlib/fix-abc1234`). Stable per FKB SHA. |
| `project-dir` | no | `.` | Path to the downstream project root. Forwarded to `bump-to-latest`. |
| `dependency-name` | no | `mathlib` | Dependency name in the lakefile. Forwarded to `bump-to-latest`. |
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
| `action` | `"created"`, `"updated"`, `"closed"`, or `"noop"` — describes the **issue** lifecycle |
| `pr-number` | Fix PR number (empty when `pr-action` is non-creating) |
| `pr-url` | Fix PR URL |
| `pr-action` | `"created"`, `"noop-existing"`, `"noop-no-changes"`, `"noop-resolved"`, or `"disabled"` (when `open-pr: false`) |

### Reusable workflow

**Path:** `.github/workflows/track-incompatibility.yml`

Thin wrapper around the composite action.  Minimal usage (with PR side):

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

Accepts the same inputs as the composite action and forwards them through.

---

## `open-incompatibility-issue` _(deprecated — use `track-incompatibility`)_
**Path:** `.github/actions/open-incompatibility-issue`

---

## `query-latest`

**Path:** `.github/actions/query-latest`

Lightweight read-only action. Fetches the snapshot and returns the target
commit for a downstream — without cloning repos, installing elan, or running
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

---

## Typical workflows

**Simplest — reusable workflow (no boilerplate):**

```yaml
name: Bump mathlib to latest

on:
  schedule:
    - cron: "0 18 * * *"
  workflow_dispatch:

jobs:
  bump:
    uses: leanprover-community/downstream-reports/.github/workflows/bump-dependency-to-latest.yml@main
    permissions:
      contents: write
      pull-requests: write
```

**Custom — composite actions (when you need extra steps):**

```yaml
name: Bump mathlib to latest

on:
  schedule:
    - cron: "0 18 * * *"
  workflow_dispatch:

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
        # no inputs needed — defaults to github.repository matched by repo slug

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
        # no inputs needed — defaults to github.repository

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
# Bump to the latest compatible mathlib release tag (rather than a raw commit).
# The resulting lakefile pins `inputRev` to a human-readable tag like "v4.13.0".

jobs:
  bump:
    uses: leanprover-community/downstream-reports/.github/workflows/bump-dependency-to-latest.yml@main
    permissions:
      contents: write
      pull-requests: write
    with:
      query-type: last-good-release
      branch: automation/release-bump
```

Or using the composite actions directly for a read-only lookup:

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
- `commit` is the resolved SHA, for consumers that need byte-equality comparison against the `rev` field in `lake-manifest.json`.
- Both fields are empty when no semver release precedes the downstream's current LKG.
