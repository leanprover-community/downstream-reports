# scripts/

Python scripts that power the downstream regression workflow. The CI workflow
validates downstream Lean projects against Mathlib upstream commits, bisects
failures, and reports results.

## How the workflow runs

```
plan (build matrix from inventory)
  |
  v
validate (per downstream, parallel)
  1. select_downstream_regression_window.py  -- HEAD probe + bisect window selection
  2. probe_downstream_regression_window.py   -- bisect probe (if needed)
  |
  v
report
  aggregate_results.py  -- apply state machine, persist results, render markdown
```

There is also an **on-demand** variant that validates downstream bumping-branches
on request. It uses `select_ondemand_plan.py` and
`select_ondemand_window.py` in place of the regression scripts above.

## Modules

### Core libraries

| File | Purpose |
|------|---------|
| `models.py` | Shared data types: `Outcome`, `DownstreamConfig`, `CommitDetail`, `WindowSelection`, `ValidationResult`, `load_inventory()`, `utc_now()`. |
| `git_ops.py` | Git operations: cloning, ref resolution, ancestry checks, commit windows, lakefile/manifest pin resolution. |
| `cache.py` | Lake artifact caching: `cache_env()`, `warm_downstream_cache()`, toolchain helpers. |
| `validation.py` | Hopscotch tool invocation, result building (`build_result_from_tool`, `build_error_result`), artifact/summary rendering, JSON persistence. |
| `storage.py` | Storage abstraction (`StorageBackend` protocol) with `FilesystemBackend` and `SqlBackend` implementations. Also provides `create_backend()` factory and `add_backend_args()` for CLI scripts. |

### CLI scripts (called by GitHub Actions)

| File | Workflow step | What it does |
|------|--------------|--------------|
| `select_downstream_regression_window.py` | validate | Runs a HEAD probe, decides whether to bisect, writes `selection.json` and possibly `result.json`. |
| `probe_downstream_regression_window.py` | validate | Loads `selection.json`, runs the bisect probe, writes `result.json`. |
| `aggregate_results.py` | report | Loads all `result.json` artifacts, applies the episode state machine, persists to storage, renders a markdown report. |
| `select_ondemand_plan.py` | plan (ondemand) | Queries GitHub API for new bumping-branch commits, outputs a job matrix. |
| `select_ondemand_window.py` | validate (ondemand) | Like `select_downstream_regression_window.py` but for bumping branches. |
| `generate_site.py` | generate-pages | Generates a static HTML status page from SQL or filesystem state. |
| `storage.py` | `create_schema(engine)` | Creates/migrates the SQL schema. See [storage backends doc](../docs/storage-backends.md) for provisioning instructions. |

### Tests

| File | Covers |
|------|--------|
| `test_run_downstream_regression.py` | Cache setup, tool invocation, commit-plan artifacts, window-selection round-trip. |
| `test_aggregate_results.py` | Episode state machine (`apply_result`), log truncation, culprit filtering. |
| `test_storage.py` | `result_to_row`, `create_backend` factory, `FilesystemBackend` round-trips. |

Run all tests: `python3 -m unittest discover scripts/ -p 'test_*.py'`

## Key concepts

- **Downstream**: A Lean project that depends on Mathlib (defined in `ci/inventory/downstreams.json`).
- **Episode**: A regression episode tracks a failure from first detection to recovery. States: `passing`, `new_failure`, `failing`, `recovered`, `error`.
- **Head probe**: Test the upstream target commit alone. If it fails, a bisect window is opened.
- **Bisect window**: The range from the last-known-good commit to the failing target, searched by hopscotch.
- **Storage backend**: Either local JSON files (`FilesystemBackend`) or PostgreSQL (`SqlBackend`).
