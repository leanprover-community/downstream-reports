# Reusable composite actions

This repo publishes three composite actions for downstream Lean projects to
consume. They are designed to be composed: `query-lkg` is the lightweight
read-only half; `bump-to-lkg` is the full bump-and-build half; `open-bump-pr`
is the generic "commit + PR" step that follows either of them.

The shared snapshot-fetch logic lives in
[`.github/scripts/fetch-lkg.sh`](../.github/scripts/fetch-lkg.sh) and is
called by both `query-lkg` and `bump-to-lkg`.

> **Extensibility note.** Today the actions are used exclusively for the
> mathlib upstream. The intent is to keep them general enough to support other
> upstream/downstream pairs in the future. When adding features, avoid baking
> in mathlib-specific assumptions: anything that varies per-upstream (dependency
> name, repo, snapshot URL) should be an explicit input with a sensible default
> rather than a hardcoded constant.

---

## `bump-to-lkg`

**Path:** `.github/actions/bump-to-lkg`

Fetches the LKG snapshot, reads the current mathlib pin from
`lake-manifest.json`, and (when a bump is needed) installs elan + hopscotch and
runs `hopscotch dep` to bump and build. On success the working tree contains
modified `lakefile` and `lake-manifest.json` ready to commit.

Also fetches human-readable commit metadata from the GitHub API to generate a
suggested PR title, body snippet, and git commit message — pass
`generate-description: 'false'` to skip these API calls.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | yes | — | Name as registered in the hopscotch-reports inventory |
| `project-dir` | no | `.` | Path to the downstream project root |
| `dependency-name` | no | `mathlib` | Name of the dependency in the lakefile |
| `hopscotch-version` | no | `v1.3.0` | Hopscotch release tag to download |
| `snapshot-url` | no | production URL | Override to test against a staging blob |
| `dependency-repo` | no | `leanprover-community/mathlib4` | GitHub repo of the dependency (`owner/repo`) — used to fetch commit descriptions |
| `generate-description` | no | `true` | Set to `false` to skip GitHub API calls; `pr-title`, `bump-description`, and `commit-message` will be empty |

### Outputs

| Output | Description |
|--------|-------------|
| `lkg-commit` | The LKG commit SHA from the snapshot |
| `current-pin` | The commit the project was pinned to before this action ran |
| `updated` | `"true"` if hopscotch successfully bumped the project |
| `skipped` | `"true"` if the project was already at the LKG commit |
| `build-failed` | `"true"` if hopscotch ran but the build failed |
| `pr-title` | Suggested PR title (empty when skipped or `generate-description: false`) |
| `bump-description` | Markdown paragraph describing the bump — new commit + previous pin, with subjects and dates. Pass to `open-bump-pr`'s `message` input. Empty when skipped or `generate-description: false`. |
| `commit-message` | Suggested git commit message (empty when skipped or `generate-description: false`) |

---

## `open-bump-pr`

**Path:** `.github/actions/open-bump-pr`

Generic commit-and-PR action. Independent of `bump-to-lkg` — works with any
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
| `message` | no | `''` | Content to place in the PR body above the automated footer. Pass the `bump-description` output from `bump-to-lkg` here. |

### Outputs

| Output | Description |
|--------|-------------|
| `pr-number` | PR number (empty when `action=noop`) |
| `pr-url` | PR URL (empty when `action=noop`) |
| `action` | `"created"`, `"updated"`, or `"noop"` |

---

## `query-lkg`

**Path:** `.github/actions/query-lkg`

Lightweight read-only action. Fetches the LKG snapshot and returns the
last-known-good commit for a downstream — without cloning repos, installing
elan, or running hopscotch.

Accepts either a downstream **name key** (e.g. `physlib`) or a **repo slug**
(e.g. `leanprover-community/physlib`). Values containing `/` are treated as
repo slugs; all others as name keys. Defaults to `github.repository`, so a
downstream repo can use it with no inputs at all.

### Inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `downstream` | no | `${{ github.repository }}` | Downstream name key or repo slug (`owner/repo`). Auto-detected by presence of `/`. |
| `snapshot-url` | no | production URL | Override to test against a staging blob |

### Outputs

| Output | Description |
|--------|-------------|
| `lkg-commit` | The last-known-good commit SHA |
| `downstream-name` | The downstream name key as registered in the snapshot |
| `repo` | GitHub repo slug (`owner/repo`) |
| `dependency-name` | The dependency name field from the snapshot entry |

---

## Typical workflow

```yaml
name: Bump mathlib to LKG

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

      - name: Bump to LKG
        id: bump
        uses: leanprover-community/hopscotch-reports/.github/actions/bump-to-lkg@main
        with:
          downstream: my-project-name   # must match ci/inventory/downstreams.json

      - name: Open PR
        if: steps.bump.outputs.updated == 'true'
        uses: leanprover-community/hopscotch-reports/.github/actions/open-bump-pr@main
        with:
          title:          ${{ steps.bump.outputs.pr-title }}
          message:        ${{ steps.bump.outputs.bump-description }}
          commit-message: ${{ steps.bump.outputs.commit-message }}
```

To just look up the LKG commit without building:

```yaml
      - name: Get LKG commit
        id: lkg
        uses: leanprover-community/hopscotch-reports/.github/actions/query-lkg@main
        # no inputs needed — defaults to github.repository

      - run: echo "LKG is ${{ steps.lkg.outputs.lkg-commit }}"
```
