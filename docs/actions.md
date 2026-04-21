# Reusable workflows and composite actions

This repo publishes two reusable workflows and four composite actions for
downstream Lean projects to consume.

For the common case — a scheduled job that bumps a dependency and opens a PR
— use the `bump-dependency-to-latest` reusable workflow directly (see below).
For tracking active incompatibilities in a persistent issue, use
`open-incompatibility-issue`. The composite actions (`bump-to-latest`, `open-bump-pr`,
`query-latest`, `open-incompatibility-issue`) are the building blocks for custom workflows
that need more control.

> **Extensibility note.** Today the actions are used exclusively for the
> mathlib upstream. The intent is to keep them general enough to support other
> upstream/downstream pairs in the future. When adding features, avoid baking
> in mathlib-specific assumptions: anything that varies per-upstream (dependency
> name, repo) should be an explicit input with a sensible default rather than a
> hardcoded constant.

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
| `hopscotch-version` | `v1.3.0` | Hopscotch release tag to download |
| `query-type` | `last-known-good` | Which commit to bump to: `last-known-good` or `first-known-bad` |

### Outputs

| Output | Description |
|--------|-------------|
| `pr-number` | PR number (empty when `action=noop`) |
| `pr-url` | PR URL (empty when `action=noop`) |
| `action` | `"created"`, `"updated"`, or `"noop"` |
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

      - uses: actions/checkout@v4
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
| `hopscotch-version` | no | `v1.3.0` | Hopscotch release tag to download |
| `generate-description` | no | `true` | Set to `false` to skip GitHub API calls; `pr-title`, `bump-description`, and `commit-message` will be empty |
| `query-type` | no | `last-known-good` | Which commit to bump to: `last-known-good`, `first-known-bad`, or `last-good-release` (semver tag, e.g. `v4.13.0`) |

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
| `pr-number` | PR number (empty when `action=noop`) |
| `pr-url` | PR URL (empty when `action=noop`) |
| `action` | `"created"`, `"updated"`, or `"noop"` |

---

## `open-incompatibility-issue`

**Path:** `.github/actions/open-incompatibility-issue`

Looks up this downstream's first-known-bad (FKB) commit in the snapshot and
opens or maintains a single persistent GitHub issue describing the current
incompatibility.  When the FKB clears (regression resolved), the action posts
a resolution comment and closes the issue.

Identity is tracked by a **label** (default `hopscotch-incompatibility`), not by
title — so a human can edit the title without breaking idempotency, and the
label can be searched or filtered independently.  The label is created
automatically if missing.

The issue body includes:
- the FKB upstream commit (linked SHA + title + author + date),
- the LKG upstream commit for contrast,
- the downstream commit the failure was observed against,
- links to the validation workflow run and the specific matrix job,
- a footer pointing back at the workflow run that last updated the issue.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | no | `${{ github.repository }}` | Downstream name key or repo slug (`owner/repo`). Auto-detected by presence of `/`. |
| `upstream` | no | `leanprover-community/mathlib4` | Upstream repo slug used to fetch commit metadata for the FKB/LKG commits. Must match the `upstream` field in the snapshot. |
| `label` | no | `hopscotch-incompatibility` | Label used to identify the persistent issue. Created if missing. |
| `title` | no | auto | Full issue title. When empty, auto-generated as `[regression] <dep-name> <fkb-short> breaks the build`. |
| `close-on-resolve` | no | `true` | When the snapshot clears the FKB, close the open tracking issue with a resolution comment. |
| `token` | no | `github.token` | Token used for `gh issue` and `gh api`. Needs `issues: write`. |

### Outputs

| Output | Description |
|--------|-------------|
| `issue-number` | Issue number (empty when `action=noop`) |
| `issue-url` | Issue URL (empty when `action=noop`) |
| `action` | `"created"`, `"updated"`, `"closed"`, or `"noop"` |

### Reusable workflow

**Path:** `.github/workflows/open-incompatibility-issue.yml`

Thin wrapper around the composite action.  Minimal usage:

```yaml
name: Track mathlib regression

on:
  schedule:
    - cron: "0 19 * * *"
  workflow_dispatch:

jobs:
  regression-issue:
    uses: leanprover-community/downstream-reports/.github/workflows/open-incompatibility-issue.yml@main
    permissions:
      issues: write
```

Accepts the same inputs as the composite action and forwards them through.

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
      - uses: actions/checkout@v4

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
