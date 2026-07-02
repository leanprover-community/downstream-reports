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
    test_select_downstream_regression_window.py
                                     → select job (window selection + skip
                                       heuristics), plus cache.py +
                                       validation.py helpers
    test_probe_downstream_regression_window.py
                                     → probe job (HEAD probe, bisect, skip
                                       + revalidation heuristics)
    test_storage.py                  → storage.py (SqlBackend via in-memory
                                       SQLite, plus factory/retry helpers)

Out of scope for the unit suite
-------------------------------
* SQL backend integration paths beyond the in-memory SQLite coverage in
  ``test_storage.py`` and ``test_export_runs_snapshot``.  The production
  ``SqlBackend`` against PostgreSQL is exercised in CI by the regression
  workflow itself, not here.
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

import datetime as _dt
import sys as _sys
from pathlib import Path as _Path

# The repo root must be on sys.path before any ``scripts.*`` import so the
# suite works when pytest is invoked from anywhere in the tree; the two
# project imports below therefore sit after this insert (hence the noqa).
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from scripts.models import DownstreamConfig, WindowSelection  # noqa: E402
from scripts.storage import RunResultRecord  # noqa: E402

# Shared SHA-shaped constants: stable, semantic names for the ``"a" * 40``
# style placeholder values used across test files.
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
FIXED_NOW = _dt.datetime(2026, 5, 5, 12, 0, 0, tzinfo=_dt.timezone.utc)
DEFAULT_LEDGER_TTL = _dt.timedelta(hours=6)

# ----------------------------------------------------------------------
# Skip-heuristic test fixtures.  Both
# ``test_select_downstream_regression_window`` and
# ``test_probe_downstream_regression_window`` exercise heuristics that
# operate on a ``WindowSelection`` for a single downstream; the helpers
# below construct minimal-but-valid instances so each test only spells
# out the fields its scenario varies.
# ----------------------------------------------------------------------

PHYSLIB_CONFIG = DownstreamConfig(
    name="physlib",
    repo="leanprover-community/physlib",
    default_branch="master",
)


def make_selection(**overrides) -> WindowSelection:
    """Build a ``WindowSelection`` with sensible skip-heuristic defaults.

    What state it provides
    ----------------------
    A selection for ``physlib`` against ``master``, with all four
    SHA-shaped fields populated using stable ``"t"*40`` / ``"d"*40`` /
    ``"p"*40`` placeholders.  Each test passes ``**overrides`` to vary
    only the fields its scenario cares about — the rest stay stable so
    the test reads as the diff between baseline and scenario.
    """
    defaults = dict(
        downstream="physlib",
        repo="leanprover-community/physlib",
        default_branch="master",
        upstream_ref="master",
        target_commit="t" * 40,
        downstream_commit="d" * 40,
        pinned_commit="p" * 40,
    )
    defaults.update(overrides)
    return WindowSelection(**defaults)


def make_run_result_record(**overrides) -> RunResultRecord:
    """Build a minimal-but-valid ``RunResultRecord``.

    What state it provides
    ----------------------
    A passing head-only run for ``physlib`` with every required field
    populated: SHA-shaped placeholders for the commits, ``None`` for the
    episode endpoints, and a consistent ``outcome`` /
    ``episode_state`` / ``head_probe_outcome`` triple.  Tests pass
    ``**overrides`` for exactly the fields their scenario varies —
    ``RunResultRecord`` has ~20 required fields, so spelling them all
    out per test site would bury the scenario in filler.
    """
    defaults = dict(
        upstream="leanprover-community/mathlib4",
        downstream="physlib",
        repo="leanprover-community/physlib",
        downstream_commit="d" * 40,
        outcome="passed",
        episode_state="passing",
        target_commit="t" * 40,
        previous_last_known_good=None,
        previous_first_known_bad=None,
        last_known_good=None,
        first_known_bad=None,
        current_last_successful=None,
        current_first_failing=None,
        failure_stage=None,
        search_mode="head-only",
        commit_window_truncated=False,
        error=None,
        head_probe_outcome="passed",
        head_probe_failure_stage=None,
        culprit_log_text=None,
    )
    defaults.update(overrides)
    return RunResultRecord(**defaults)


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
    "PHYSLIB_CONFIG",
    "make_selection",
    "make_run_result_record",
]
