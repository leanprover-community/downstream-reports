# Batteries PR validation

`batteries-pr-validation.yml` tests the Batteries-to-Mathlib direction.
It complements `mathlib-pr-validation.yml`, which tests Mathlib changes against downstream projects.
Batteries dispatches this workflow only after its own PR CI succeeds.

The workflow runs from `main` on the existing isolated self-hosted `pr` pool.
Batteries needs no runner access. Both validation workflows share the `mathlib-pr-validation` queue.
They use `queue: max` so pending requests are not replaced by newer dispatches.

## Dispatch inputs

| Input | Meaning |
|---|---|
| `request_id` | Batteries workflow run ID and attempt, such as `123-1`. |
| `pr_number` | Batteries PR number. |
| `batteries_repo` | Repository containing the PR head. |
| `batteries_sha` | Exact Batteries head SHA. |
| `mathlib_sha` | Exact Mathlib master SHA. |
| `adaptation_fork` | Dedicated Mathlib fork name in `leanprover-community`. |
| `adaptation_pr` | Existing adaptation PR number, or an empty string for an initial build. |

The run name is `batteries-pr-validation:<request_id>`.
The Batteries controller uses that name to find its exact run and waits through queue and execution states.
It renews its App token during long waits. After 285 minutes it cancels the identified run and stops without a PR.

## Build and artifacts

The script checks out the Mathlib SHA, pins Batteries, and runs `lake --keep-toolchain update batteries`.
An initial check reads the public cache and builds Mathlib, Archive, and Counterexamples.
It does not upload a cache or run the complete Mathlib test and lint suite.
An existing adaptation skips the initial build. It merges the pinned Mathlib master revision and refreshes the dependency.
Conflicts stop preparation; the script does not discard human adaptations or force-push.

`batteries-validation-result` contains `result.json` at the artifact root.
It echoes every dispatch input as a string and adds `status` and `stage`.
The status is one of:

- `pass`: the initial build passes. No candidate bundle is published.
- `fail`: compilation fails and the candidate bundle is ready for publication.
- `prepared`: an existing adaptation branch is ready for publication and ordinary fork CI.
- `infra_failure`: setup, cache retrieval, merge, toolchain verification, or bundle creation fails.

`mathlib-adaptation-branch` contains `mathlib-adaptation.bundle` only for `fail` or `prepared`.
The bundle contains the candidate branch's Git objects relative to the pinned Mathlib revision.
`batteries-validation-log` contains the build log. All artifacts expire after seven days.

The Batteries publisher checks the returned inputs, fetches the bundle into a fresh bare repository, and pushes without force.
It opens a draft Mathlib PR only after a matching `fail` result. A `prepared` result updates the existing PR.
Cancellation, workflow failure, missing artifacts, and infrastructure errors cannot trigger PR creation.

## Permissions and rollout

Install the existing Mathlib nightly-testing GitHub App on downstream-reports.
The Batteries controller requests a repository token with Actions write and Contents read.
That token allows dispatch, polling, artifact downloads, and cancellation of its timed-out run.
The build job receives only read-only credentials. It receives no App tokens, database secrets, or cache upload credentials.
Lake subprocesses also strip token and service-secret variables from their environment.

Merge this companion before enabling the Batteries controller.
No new runner access grants, token-mint environments, cache containers, or Azure/R2 credentials are required here.
The separate adaptation fork, publisher App permissions, and ordinary Mathlib CI result reporter are specified in the Batteries draft.
