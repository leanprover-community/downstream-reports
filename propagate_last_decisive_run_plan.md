# Plan: last_decisive_run — back-link skipped validations to their defining run

## Problem

When a skip heuristic fires (`try_skip_already_good` or `try_skip_known_bad_bisect`), the CI job produces no logs and no inspectable result. Users have no way to navigate to the run that actually determined the outcome.

## Concept

Track, per downstream, the **last decisive run** — any run where hopscotch actually executed (i.e. was not a skip). This covers:

- `head-only` runs (passing or failing, when no bisect step followed)
- `bisect` / `linear` runs (the probe step ran)

Explicitly excluded (not decisive):
- `skipped-already-good`
- `head-only-known-bad`
- `setup-error`

The record is updated by `aggregate_results.py` whenever it processes a decisive result for a downstream. The `render_report` function then looks it up and adds a back-link for skipped rows.

**No changes are needed to the validation pipeline.** The job URL never travels through `ValidationResult`, `models.py`, or `validation.py`. The entire feature lives in `storage.py` and `aggregate_results.py`.

---

## Files to modify

- `scripts/storage.py`
- `scripts/aggregate_results.py`
- `scripts/test_storage.py`
- `scripts/test_aggregate_results.py`

---

## Step-by-step changes

### 1. `scripts/storage.py` — new dataclass

Add after `DownstreamStatusRecord`:

```python
@dataclass
class LastDecisiveRunRecord:
    """The most recent non-skip validation run for one downstream.

    Updated whenever hopscotch actually executed (head-only, bisect, linear).
    Used to back-link skipped CI jobs to the run that established the outcome.
    """

    downstream: str
    job_url: str
    run_id: str
    recorded_at: str      # ISO-8601
    outcome: str          # 'passed' | 'failed' | 'error'
    search_mode: str
```

### 2. `scripts/storage.py` — SQL table

Add to the `SqlBackend` class body alongside the other `_sa_*` table definitions:

```python
_sa_last_decisive_run = Table(
    "last_decisive_run",
    _sa_metadata,
    Column("downstream", String, primary_key=True),
    Column("workflow", String, primary_key=True),
    Column("upstream", String, primary_key=True),
    Column("job_url", String, nullable=False),
    Column("run_id", String, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("outcome", String, nullable=False),
    Column("search_mode", String, nullable=False),
)
```

> **Migration note**: This is a new table, not an `ALTER TABLE`. Add a `CREATE TABLE IF NOT EXISTS` migration or use `_sa_metadata.create_all(engine, checkfirst=True)` on first run.

### 3. `scripts/storage.py` — `StorageBackend` protocol

Add two methods to the protocol:

```python
def load_last_decisive_runs(
    self, workflow: str, upstream: str
) -> dict[str, LastDecisiveRunRecord]:
    """Return the last decisive run keyed by downstream name."""
    ...

def save_last_decisive_run(
    self,
    *,
    conn: Any,  # or however the backend handles transactions
    workflow: str,
    upstream: str,
    record: LastDecisiveRunRecord,
) -> None:
    ...
```

Alternatively, accept `last_decisive_runs: dict[str, LastDecisiveRunRecord]` as an extra keyword arg on the existing `save_run()` method — simpler since `save_run` is already transactional.

> **Preferred**: add `last_decisive_runs: dict[str, LastDecisiveRunRecord] | None = None` to `save_run()` on the protocol and all three backends. Keeps the transaction boundary in one place.

### 4. `scripts/storage.py` — `FilesystemBackend`

**`load_last_decisive_runs`**:

```python
def load_last_decisive_runs(self, workflow: str, upstream: str) -> dict[str, LastDecisiveRunRecord]:
    status_key = _WORKFLOW_STATUS_KEY[workflow]
    path = self._root / "status" / f"{status_key}-last-decisive.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return {
        name: LastDecisiveRunRecord(**data)
        for name, data in payload.items()
    }
```

**`save_run`** — at the end of the method, if `last_decisive_runs` is provided:

```python
if last_decisive_runs:
    status_key = _WORKFLOW_STATUS_KEY[workflow]
    decisive_path = self._root / "status" / f"{status_key}-last-decisive.json"
    # Merge with any existing records so downstreams absent from this run are preserved.
    existing = json.loads(decisive_path.read_text()) if decisive_path.exists() else {}
    existing.update({
        name: dataclasses.asdict(r)
        for name, r in last_decisive_runs.items()
    })
    decisive_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
```

### 5. `scripts/storage.py` — `SqlBackend.load_last_decisive_runs`

```python
def load_last_decisive_runs(self, workflow: str, upstream: str) -> dict[str, LastDecisiveRunRecord]:
    t = _sa_last_decisive_run
    stmt = sa_select(
        t.c.downstream, t.c.job_url, t.c.run_id,
        t.c.recorded_at, t.c.outcome, t.c.search_mode,
    ).where(t.c.workflow == workflow, t.c.upstream == upstream)
    with self._engine.connect() as conn:
        rows = conn.execute(stmt).fetchall()
    return {
        row[0]: LastDecisiveRunRecord(
            downstream=row[0],
            job_url=row[1],
            run_id=row[2],
            recorded_at=row[3].isoformat() if hasattr(row[3], "isoformat") else row[3],
            outcome=row[4],
            search_mode=row[5],
        )
        for row in rows
    }
```

### 6. `scripts/storage.py` — `SqlBackend.save_run`

Inside the transaction, after the `downstream_status` upsert loop, add:

```python
for name, r in (last_decisive_runs or {}).items():
    self._upsert(
        conn,
        _sa_last_decisive_run,
        values={
            "downstream": name,
            "workflow": workflow,
            "upstream": upstream,
            "job_url": r.job_url,
            "run_id": r.run_id,
            "recorded_at": _parse_dt(r.recorded_at),
            "outcome": r.outcome,
            "search_mode": r.search_mode,
        },
        conflict_cols=["downstream", "workflow", "upstream"],
        update_cols=["job_url", "run_id", "recorded_at", "outcome", "search_mode"],
    )
```

### 7. `scripts/storage.py` — `DryRunBackend`

In `save_run`, print the decisive runs that would be written:

```python
for name, r in (last_decisive_runs or {}).items():
    print(f"  [dry-run] last_decisive_run[{name}]: job_url={r.job_url} outcome={r.outcome}")
```

`load_last_decisive_runs` returns `{}`.

---

### 8. `scripts/aggregate_results.py` — helper

Add a constant for the skip modes:

```python
_SKIP_SEARCH_MODES = frozenset({"skipped-already-good", "head-only-known-bad"})
```

Add a helper to decide whether a result is decisive:

```python
def _is_decisive(result: ValidationResult) -> bool:
    """Return True if this result reflects an actual hopscotch execution."""
    return result.search_mode not in _SKIP_SEARCH_MODES and result.search_mode != "setup-error"
```

### 9. `scripts/aggregate_results.py` — `main()`

**Before the result loop**, load the existing decisive run records:

```python
prior_decisive_runs = backend.load_last_decisive_runs(args.workflow, args.upstream)
```

**Inside the result loop**, after `apply_result()`, build the updated decisive run map:

```python
updated_decisive_runs: dict[str, LastDecisiveRunRecord] = {}

# (inside loop, after apply_result)
if _is_decisive(result):
    job_url = job_urls.get(result.downstream)
    if job_url:
        updated_decisive_runs[result.downstream] = LastDecisiveRunRecord(
            downstream=result.downstream,
            job_url=job_url,
            run_id=args.run_id,
            recorded_at=recorded_at,
            outcome=result.outcome.value,
            search_mode=result.search_mode,
        )
    # If no job_url available, preserve the prior record (don't overwrite with None).
```

**Pass to `backend.save_run()`**:

```python
backend.save_run(
    ...,
    last_decisive_runs=updated_decisive_runs,
)
```

### 10. `scripts/aggregate_results.py` — `render_report()`

Add parameter:

```python
def render_report(
    *,
    ...,
    last_decisive_runs: dict[str, LastDecisiveRunRecord] | None = None,
) -> str:
```

In the per-downstream `<details>` block, before "Previous state before this run:", add:

```python
decisive = (last_decisive_runs or {}).get(downstream_name)
if decisive and row["search_mode"] in _SKIP_SEARCH_MODES:
    lines.append(f"- Last decisive run: [view job]({decisive.job_url})")
```

**Pass from `main()`**:

Merge `prior_decisive_runs` and `updated_decisive_runs` (updated takes precedence) and pass to `render_report`:

```python
all_decisive = {**prior_decisive_runs, **updated_decisive_runs}
render_report(..., last_decisive_runs=all_decisive)
```

---

## Tests

### `test_storage.py` — new class `LastDecisiveRunTests`

```python
def test_round_trip_via_filesystem_backend(self):
    """Decisive run record is persisted and reloaded correctly."""

def test_missing_file_returns_empty_dict(self):
    """load_last_decisive_runs returns {} when no file exists yet (first run)."""

def test_decisive_run_is_merged_not_replaced(self):
    """Saving decisive runs for a subset of downstreams preserves records for others."""
```

### `test_aggregate_results.py` — new class `IsDecisiveTests`

```python
def test_head_only_is_decisive(self):
def test_bisect_is_decisive(self):
def test_skipped_already_good_is_not_decisive(self):
def test_head_only_known_bad_is_not_decisive(self):
def test_setup_error_is_not_decisive(self):
```

### `test_aggregate_results.py` — new class `RenderReportLastDecisiveRunTests`

```python
def test_back_link_shown_for_skipped_result(self):
    """render_report includes back-link when search_mode is a skip mode and decisive run is known."""

def test_back_link_not_shown_for_decisive_result(self):
    """render_report does not add back-link when the result itself is decisive."""

def test_back_link_not_shown_when_no_decisive_run_recorded(self):
    """render_report is silent when last_decisive_runs has no entry for a skipped downstream."""
```

---

## Verification

```bash
source /tmp/hr-venv/bin/activate
python3 -m pytest scripts/test_storage.py scripts/test_aggregate_results.py scripts/test_run_downstream_regression.py -x -v
```

Check that:
1. A non-skip result produces a `LastDecisiveRunRecord` entry.
2. A subsequent skip result has its `<details>` block contain a "Last decisive run" link.
3. The link is absent when the result is itself decisive.
4. The filesystem backend merges across runs (downstreams not in the current batch retain their prior record).
