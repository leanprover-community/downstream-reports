# Downstream reports

This repository hosts a CI service that watches compatibility between
`mathlib4` and a curated set of downstream Lean projects that depend on it.
Twice a day it builds every registered project against the newest mathlib
commit. When a build fails, it bisects the mathlib history and records the first
commit that breaks the project. It reports the results on a public status page,
and it publishes GitHub Actions that your own repository can call to act on
them.

The builds run [`hopscotch`](https://github.com/leanprover-community/hopscotch),
a Lean CLI tool that steps a project through a list of upstream commits to find
the first failure. This repository is the harness around that tool: the
schedule, the runners, and the reports.

Status page: <https://leanprover-community.github.io/downstream-reports/>

At the moment the service validates the mathlib dependency only.

## Why register your project

**An updated health signal.** Daily runs reports whether your project still
builds against the head of mathlib `master`.

**The exact commit that breaks your project.** After a failure the service bisects
mathlib and records two commits: the last known good (LKG) commit and the first
known bad (FKB) commit. The two are adjacent, so the FKB commit is the precise
cause. You get one commit to look at instead of a range to search by hand.

**A safe bump target.** The LKG commit is the newest mathlib commit that builds
with your project. The composite actions in this repository move your dependency
pin to that commit, build it to confirm, and open a pull request. Your project
stays close to master, and it never lands on a mathlib commit that is known to
break it.

**A check before a mathlib PR merges.** A mathlib reviewer can comment
`!downstream-check MyProject` on a mathlib4 pull request. The service builds your
project against that pull request and posts the verdict back as a comment. The
break is found before it reaches master.

**Visibility with mathlib maintainers.** The state of your project appears on the
public status page and in the Zulip alerts that the mathlib community reads.

**Optional issue tracking in your own repository.** The `track-incompatibility`
action opens and maintains an issue while a regression is active, and closes the
issue when the regression clears.

## Register your project

> [!IMPORTANT]
> **A registered project is expected to keep up with mathlib.** The service
> validates your project against recent mathlib commits. A project whose pin
> falls far behind produces a report that helps nobody: the search window grows,
> each run costs more machine time, and the commit it reports comes from old
> history. Keep your pin close to mathlib master — the actions in
> [Keep your project current](#keep-your-project-current) do this for you. The
> maintainers of this repository can disable an entry that stops keeping pace,
> and can enable it again when the project catches up.

Add one entry to `ci/inventory/downstreams.json` and open a pull request. These
fields are required:

| Field | Description |
| --- | --- |
| `name` | Unique identifier for your project. The actions accept it as the `downstream` input. |
| `repo` | GitHub repository in `owner/name` form. |
| `default_branch` | Branch to clone for validation, for example `main` or `master`. |
| `dependency_name` | Must match the `name` field of the mathlib `[[require]]` entry in your `lakefile.toml`. For mathlib dependents this is always `"mathlib"`. |

```json
{
  "name": "MyProject",
  "repo": "owner/MyProject",
  "default_branch": "main",
  "dependency_name": "mathlib",
  "enabled": true
}
```

These optional fields change what a run does:

| Field | Description |
| --- | --- |
| `enabled` | Set `false` to hold the entry in the file but exclude it from every run. The default is `true`. |
| `bumping_branch` | A branch in your repository where your mathlib bumps land. It lets maintainers test that branch on demand against the head of mathlib master. |
| `run_test`, `run_lint` | Also run `lake test` or `lake lint` in each validation build. Both default to `false`. |
| `build_args`, `test_args`, `lint_args` | Extra arguments for the matching `lake` step. |
| `watch_manifest` | Dispatch a fresh run as soon as your pin moves past the recorded FKB commit. |

The other fields in the file control cost and search heuristics. The maintainers
of this repository set them. Do not read this file from outside this repository:
the schema can change at any time. Use the composite actions instead.

The first run has no prior state for your project, so it reports either a pass or
a new failure. Your project appears on the status page after that run, and the
actions can find it from then on.

## Keep your project current

The service refreshes the LKG data after every run. You can consume that data in
three ways, from full automation to a plain lookup.

One limit applies to all three. The LKG data describes your project as it was at
the last validation run. If your project changed since then, the recorded commit
can fail against the new state. For this reason `bump-to-latest` repeats the
build and reports success only when the bump still works.

### Option 1 — Bump and open a pull request (recommended)

Compose `bump-to-latest` and `open-bump-pr` to move the pin, build it to
confirm, and open or update a single pull request. A project already at the LKG
commit gets a no-op run. A failed build ends the run and touches no pull request.
Both actions are idempotent on unchanged input, so the workflow is safe on a
sub-daily cron.

```yaml
name: Bump mathlib to latest

on:
  schedule:
    - cron: "0 18 * * *"   # daily; adjust to taste
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
        with:
          downstream: MyProject   # registered name or repo slug; defaults to this repo

      - name: Open or update PR
        if: steps.bump.outputs.updated == 'true'
        uses: leanprover-community/downstream-reports/.github/actions/open-bump-pr@main
        with:
          title:          ${{ steps.bump.outputs.pr-title }}
          message:        ${{ steps.bump.outputs.bump-description }}
          commit-message: ${{ steps.bump.outputs.commit-message }}
```

The default `GITHUB_TOKEN` is enough for this workflow. Use a GitHub App token
instead if you want your own CI to run on the bump pull requests without a
per-run approval click. See
[authentication setup](docs/actions.md#set-up-authentication).

### Option 2 — Bump and push directly

To commit the bump straight to your default branch, use `bump-to-latest` alone
and push the result:

```yaml
      - name: Bump to latest
        id: bump
        uses: leanprover-community/downstream-reports/.github/actions/bump-to-latest@main
        with:
          downstream: MyProject

      - name: Push bump
        if: steps.bump.outputs.updated == 'true'
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add -A
          git commit -m "${{ steps.bump.outputs.commit-message }}"
          git push
```

### Option 3 — Read the commit only

Use `query-latest` to read the current LKG or FKB commit without a clone, a
build, or a change to your working tree. This is the right start for a custom
workflow, for example a notification, a dispatch to another CI job, or your own
update script. This option skips the verification build, so the commit is not
guaranteed to still be good for a project that changed since the last run.

```yaml
      - name: Get LKG commit
        id: latest
        uses: leanprover-community/downstream-reports/.github/actions/query-latest@main
        # defaults to github.repository — no inputs needed when the repo slug
        # matches the registered downstream's repo field

      - name: Do something with the LKG commit
        run: echo "LKG is ${{ steps.latest.outputs.commit }}"
```

## Composite actions

Downstream projects can call these four composite actions:

| Name | Description |
| --- | --- |
| [`bump-to-latest`](.github/actions/bump-to-latest) | Reads the target commit (LKG, FKB, or last good release), checks the current pin, then bumps and builds. |
| [`open-bump-pr`](.github/actions/open-bump-pr) | Commits working-tree changes and creates or updates a pull request. |
| [`query-latest`](.github/actions/query-latest) | Read-only lookup. Returns the target commit without a clone or a build. |
| [`track-incompatibility`](.github/actions/track-incompatibility) | Opens and maintains an issue, and optionally a fix pull request, while an FKB regression is active. Closes both when the regression clears. |

[`docs/actions.md`](docs/actions.md) holds the full input and output reference,
the [authentication setup](docs/actions.md#set-up-authentication), a canonical
example that combines all four actions, and notes on a sub-daily cron cadence.

## How the service works

Four kinds of run produce the data that the actions consume.

**Scheduled validation.** Twice a day the service builds every registered
project against the newest mathlib commit. On a failure it bisects the mathlib
history for the commit that breaks the project. A change of state, from healthy
to broken or back, sends a Zulip alert.

**On-demand runs.** A maintainer can test one project's bumping branch against
the head of mathlib master. The result shows how far forward that branch can
move. Every result goes to Zulip, not only a change of state.

**Pull request checks.** A `!downstream-check` comment on a mathlib4 pull
request builds the named projects against that pull request. The service posts
one comment with the verdicts. This run is ephemeral: it does not change the
recorded state of a project.

**Summaries.** A maintainer can send the current state of every project to Zulip
as a table.

The composite actions read the results of these runs for you. See
[`docs/actions.md`](docs/actions.md) for what each action reads and reports.
