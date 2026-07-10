#!/usr/bin/env python3
"""
Tests for: scripts.publish_run

Coverage scope:
    - ``main`` (CLI) — reads a staged persist payload and replays it as a
      single ``save_run`` against the backend.  Integration-style: writes a
      payload with ``write_run_payload``, runs the CLI against an on-disk
      SQLite backend, and asserts the reported statuses landed.
    - The empty-payload no-op path — a run with zero result records (a
      skipped-only or fully tripwire-excluded run) must persist nothing and
      still exit 0.

Out of scope:
    - The payload contract itself (schema-version / missing-file strictness):
      covered by ``test_storage.py``'s ``TestRunPayloadRoundTrip``.

Why this matters
----------------
``publish_run`` is the only writer in the regression / on-demand report
pipeline.  The report job runs on every branch with a read-only DSN and never
writes; if this CLI silently dropped the payload the database would stop
advancing without any job failing.  These tests pin that it persists exactly
what the report job staged.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

from scripts.conftest import make_run_result_record
from scripts.publish_run import main as publish_main
from scripts.storage import (
    DownstreamStatusRecord,
    SqlBackend,
    create_schema,
    create_sql_engine,
    write_run_payload,
)

_UPSTREAM = "leanprover-community/mathlib4"


def _write_payload(path, *, results):
    """Stage a persist payload; a nonempty run carries a matching status row."""
    return write_run_payload(
        path,
        run_id="run_1",
        workflow="regression",
        upstream=_UPSTREAM,
        upstream_ref="master",
        run_url="https://example.com/run/1",
        created_at="2026-06-10T00:00:00Z",
        results=results,
        updated_statuses={
            "physlib": DownstreamStatusRecord(
                last_known_good_commit="g" * 40,
                first_known_bad_commit="b" * 40,
            )
        } if results else {},
    )


def test_cli_persists_payload_into_backend(tmp_path) -> None:
    """Scenario: publish_run replays a staged payload as a save_run, so the
    reported statuses land in the write-capable database."""
    dsn = f"sqlite:///{tmp_path}/state.db"
    engine = create_sql_engine(dsn)
    create_schema(engine)
    results = [
        make_run_result_record(
            downstream="physlib",
            outcome="failed",
            episode_state="failing",
            last_known_good="g" * 40,
            first_known_bad="b" * 40,
            head_probe_outcome="failed",
        )
    ]
    payload = _write_payload(tmp_path / "persist.json", results=results)
    argv = [
        "publish_run.py",
        "--backend", "sql",
        "--dsn", dsn,
        "--persist-input", str(payload),
    ]
    with patch.object(sys, "argv", argv):
        assert publish_main() == 0
    statuses = SqlBackend(engine).load_all_statuses("regression", _UPSTREAM)
    assert statuses["physlib"].first_known_bad_commit == "b" * 40


def test_cli_empty_payload_is_a_no_op(tmp_path) -> None:
    """Scenario: a payload with no result records persists nothing and still
    exits 0 — a skipped-only or fully tripwire-excluded run needs no write."""
    dsn = f"sqlite:///{tmp_path}/state.db"
    engine = create_sql_engine(dsn)
    create_schema(engine)
    payload = _write_payload(tmp_path / "persist.json", results=[])
    argv = [
        "publish_run.py",
        "--backend", "sql",
        "--dsn", dsn,
        "--persist-input", str(payload),
    ]
    with patch.object(sys, "argv", argv):
        assert publish_main() == 0
    assert SqlBackend(engine).load_all_statuses("regression", _UPSTREAM) == {}
