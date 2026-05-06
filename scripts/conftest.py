"""Shared fixtures and constants for the downstream-reports test suite.

Architecture
------------
The test files live alongside the production code in ``scripts/`` rather
than under a separate ``tests/`` tree.  Each ``test_<module>.py`` exercises
the public surface of the matching production module:

    test_aggregate_results.py        → aggregate_results.py
    test_check_downstream_manifests  → check_downstream_manifests.py
    test_export_lkg_snapshot.py      → export_lkg_snapshot.py
    test_export_runs_snapshot.py     → export_runs_snapshot.py
    test_git_ops.py                  → git_ops.py (subset)
    test_notifications.py            → notifications.py
    test_plan_cache_warm_jobs.py     → plan_cache_warm_jobs.py
    test_record_warm_shas.py         → record_warm_shas.py
    test_run_downstream_regression   → cache.py + validation.py +
                                       select/probe regression scripts
                                       (the file pre-dates the select/probe
                                       split and still uses the old name)
    test_storage.py                  → storage.py (FilesystemBackend only)

Out of scope for the unit suite
-------------------------------
* SQL backend integration paths beyond what
  ``test_export_runs_snapshot.LoadLatestRunPerDownstreamTests`` covers
  via in-memory SQLite.  The production ``SqlBackend`` against PostgreSQL
  is exercised in CI by the regression workflow itself, not here.
* Real network calls.  ``fetch_*`` helpers in ``aggregate_results`` /
  ``notifications`` are tested with the HTTP layer mocked.
* Real ``hopscotch`` invocations.  ``invoke_tool`` and the regression
  scripts are tested with ``run_validation_attempt`` patched out.

Fixture scopes
--------------
* ``function``-scoped (default) for stateful objects — backends,
  temporary directories, mock recorders.
* ``module``-scoped for read-only fixtures whose construction is
  expensive but whose state is never mutated by tests (e.g. the git
  fixture repo in ``test_git_ops``).
* ``session``-scoped is intentionally avoided to keep test isolation
  obvious.
"""

from __future__ import annotations

# Shared SHA-shaped constants.  Tests across multiple files used to repeat
# `"a" * 40`, `"f" * 40`, etc. ad hoc; collecting them here gives the magic
# values stable, semantic names without changing any assertions.
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40
SHA_E = "e" * 40
SHA_F = "f" * 40

# Conventional fixture pins used by aggregate_results / probe tests.
# `OLD_PIN` is the previous downstream pin recorded on `downstream_status`;
# `NEW_PIN` is the value reported by the current run.
OLD_PIN = "1" * 40
NEW_PIN = "2" * 40

# Fixed "now" used by the manifest watcher tests.  Aligns with the date
# pinned in MEMORY.md so generated artifacts are stable across runs.
import datetime as _dt

FIXED_NOW = _dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=_dt.timezone.utc)
DEFAULT_LEDGER_TTL = _dt.timedelta(hours=6)

__all__ = [
    "SHA_A",
    "SHA_B",
    "SHA_C",
    "SHA_D",
    "SHA_E",
    "SHA_F",
    "OLD_PIN",
    "NEW_PIN",
    "FIXED_NOW",
    "DEFAULT_LEDGER_TTL",
]
