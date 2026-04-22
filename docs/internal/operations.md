# Operations guide

## Interpreting the report

After each run, `aggregate_regressions.py` appends a Markdown report to the
GitHub Actions job summary. Each downstream has an **outcome** and an **episode
state**.

### Outcomes

| Outcome | Meaning |
| --- | --- |
| `passed` | The downstream built successfully against the target mathlib commit. |
| `failed` | The downstream build failed. If a bisect window was available, `first_known_bad` identifies the culprit commit; otherwise only the target commit is known to be bad. |
| `error` | A transient infrastructure problem prevented a meaningful result (e.g. a git clone timed out, a runner was preempted, or the hopscotch tool exited with an unexpected code). Error results **do not change episode state**. |

### Episode states

The episode state describes the *transition* relative to the previous run, not
just the current outcome. It is what tells you whether something changed.

| State | Previous | Current | What it means |
| --- | --- | --- | --- |
| `passing` | passing | passed | Healthy; no action needed. |
| `new_failure` | passing | failed | A regression was introduced. Investigate `first_known_bad`. |
| `failing` | failing | failed | Ongoing regression. `first_known_bad` is preserved from the initial episode. |
| `recovered` | failing | passed | The downstream builds again; the episode is closed. |
| `error` | (any) | error | Transient problem; episode state is unchanged. Check the validate job log. |

**`error` results are silent with respect to the state machine.** If a
downstream produces `error` on every run, `new_failure` will never be recorded
even if the downstream is genuinely broken. Check the runner or network when
`error` recurs across multiple runs.

The `first_known_bad` commit shown for a `failing` downstream is always
the commit from the *initial* `new_failure` episode, not the current run. It
represents the earliest known introduction of the regression.


## Re-running a single downstream

To re-run one downstream without waiting for the next scheduled run, trigger
`downstream-regression-report.yml` manually from the Actions tab and set the
`downstream` input to the downstream's name exactly as it appears in
`ci/inventory/downstreams.json` (e.g. `PrimeNumberTheoremAnd`).

Leave `mathlib_ref` empty to test against `master`, or fill it in to test
against a specific commit or branch.


## Resetting episode state

If the database records incorrect episode state — for example, a transient
build failure was recorded as `new_failure`, or a recovery was not reflected
correctly — update `downstream_status` directly:

**Clear a false `new_failure` (mark as passing):**

```sql
UPDATE downstream_status
SET first_known_bad = NULL
WHERE downstream = 'PrimeNumberTheoremAnd'
  AND workflow = 'regression';
```

On the next run the downstream will be treated as passing and the episode will
be recorded as `passing` or `new_failure` depending on the actual outcome.

**Advance `last_known_good` to a commit confirmed good out of band:**

```sql
UPDATE downstream_status
SET last_known_good = '<full-commit-sha>'
WHERE downstream = 'PrimeNumberTheoremAnd'
  AND workflow = 'regression';
```

This shrinks the bisect window on the next run that needs one.

**Inspect current episode state:**

```sql
SELECT downstream, last_known_good, first_known_bad, updated_at
FROM downstream_status
WHERE workflow = 'regression'
ORDER BY downstream;
```
