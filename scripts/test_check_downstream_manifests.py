#!/usr/bin/env python3
"""
Tests for: scripts.check_downstream_manifests

Coverage scope:
    - ``evaluate_downstream`` — per-downstream decision logic.  Six
      layered filters (branch unchanged → pin unchanged → no FKB →
      compare result → in-flight set → ledger TTL); each filter has a
      dedicated test that fires exactly that branch.
    - ``build_candidates`` — orchestration: opt-in / enabled / no-FKB
      pre-filters that fire before any HTTP call.
    - ``pinned_from_manifest_payload`` — manifest-parse helper (lives
      in ``git_ops`` but is exercised here because the watcher is its
      only caller).
    - ``gh_in_flight_downstreams`` / ``gh_dispatch_workflow`` — HTTP
      layer, monkey-patched ``_gh_request``.
    - ``main`` — orchestration of the above plus the ledger writes.

Out of scope:
    - The 12-hour scheduled regression run; the watcher is a sub-15-minute
      hot-path service that piggybacks on top.  End-to-end behaviour is
      asserted by the workflow itself.
    - Real GitHub API calls.  The ``in-flight`` job-name regex
      (``select: <name>`` / ``probe: <name>``) and the dispatch payload
      shape are pinned against the documented API contract; if GitHub
      changes either, the integration would break and we would catch
      it in the deployed workflow's logs.

Why this matters
----------------
A false-positive dispatch (the watcher fires when the downstream
hasn't actually moved) wastes a self-hosted runner.  A false-negative
(the watcher misses a real bump past FKB) means a downstream that just
landed on a known-broken commit waits up to 12 hours for the next
scheduled run to pick it up — which is exactly the gap the watcher
exists to close.  The six-filter ladder is the contract that makes
both kinds of error rare; each test below pins one rung of it.

Test architecture note
----------------------
The decision logic is tested with **injected fetcher callables** rather
than monkey-patched ``urllib`` — the agent audit flagged this as
over-mocking.  We accept that trade-off here because the production
HTTP layer is covered separately by ``InFlightSetTests`` /
``DispatchPayloadTests``, and unit-testing the decision logic with
hand-crafted lambdas keeps the cases legible.

# REVIEW: ``MainOrchestrationTests._RecordingBackend`` exposes
# ``dispatched`` and ``upserts`` attributes that are added dynamically
# inside the test rather than declared on the test double.  This is a
# code smell carried over from the pre-refactor file; the recording
# backend should be promoted to a typed test double.  Left as-is to
# avoid changing test semantics; tracked in the audit report's
# "Flags Left in Code" section.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import check_downstream_manifests as watcher
from scripts.check_downstream_manifests import Candidate, evaluate_downstream
from scripts.git_ops import pinned_from_manifest_payload
from scripts.models import DownstreamConfig
from scripts.storage import DownstreamStatusRecord, ManifestWatcherLedgerRow


_FKB = "f" * 40
_OLD_PIN = "1" * 40
_NEW_PIN = "2" * 40
_BRANCH_OLD = "a" * 40
_BRANCH_NEW = "b" * 40
_NOW = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
# Test fixture only — production default is 4h (--ledger-ttl-hours).  The
# fresh/stale fixtures below are 1h and 12h, so any reasonable TTL works.
_TTL = timedelta(hours=6)


def _config(
    name: str = "PhysLib",
    *,
    default_branch: str = "main",
    dependency_name: str = "mathlib",
    watch_manifest: bool = True,
) -> DownstreamConfig:
    """Minimal enabled DownstreamConfig.

    Defaults to ``watch_manifest=True`` because the per-downstream evaluation
    tests assume the entry is opted in.  Opt-out is exercised explicitly in
    ``BuildCandidatesTests``.
    """
    return DownstreamConfig(
        name=name,
        repo=f"org/{name}",
        default_branch=default_branch,
        dependency_name=dependency_name,
        watch_manifest=watch_manifest,
    )


def _status(
    *,
    pinned: str | None = _OLD_PIN,
    fkb: str | None = _FKB,
    downstream_commit: str | None = _BRANCH_OLD,
) -> DownstreamStatusRecord:
    return DownstreamStatusRecord(
        last_known_good_commit=None,
        first_known_bad_commit=fkb,
        pinned_commit=pinned,
        downstream_commit=downstream_commit,
    )


def _manifest(rev: str | None, *, dep: str = "mathlib") -> dict:
    """Build a minimal lake-manifest.json payload pinning `dep` at `rev`."""
    return {
        "packages": [
            {"name": dep, "type": "git", "rev": rev, "url": "https://example/m4.git"},
        ],
    }


_DEFAULT = object()  # Sentinel so callers can pass `status=None` explicitly.


def _evaluate(
    *,
    config: DownstreamConfig | None = None,
    status: object = _DEFAULT,
    ledger_row: ManifestWatcherLedgerRow | None = None,
    in_flight: set[str] | None = None,
    branch_head: str | None = _BRANCH_NEW,
    manifest: object = _DEFAULT,
    compare: str | None = "ahead",
) -> tuple[Candidate | None, dict]:
    """Drive evaluate_downstream with a default-rich set of injected mocks.

    Returns (candidate, calls) where `calls` records which fetchers were
    invoked, so tests can assert short-circuiting (e.g. branch unchanged ⇒
    manifest fetcher never called).
    """

    config = config or _config()
    if status is _DEFAULT:
        status = _status()
    in_flight = in_flight or set()
    if manifest is _DEFAULT:
        manifest = _manifest(_NEW_PIN)

    calls: dict[str, list] = {"branch": [], "manifest": [], "compare": []}

    def _branch(repo: str, branch: str) -> str | None:
        calls["branch"].append((repo, branch))
        return branch_head

    def _manifest_fetcher(repo: str, sha: str) -> object:
        calls["manifest"].append((repo, sha))
        return manifest

    def _compare(upstream: str, base: str, head: str) -> str | None:
        calls["compare"].append((upstream, base, head))
        return compare

    candidate = evaluate_downstream(
        config,
        upstream_repo="leanprover-community/mathlib4",
        status=status,
        ledger_row=ledger_row,
        in_flight=in_flight,
        ttl=_TTL,
        now=_NOW,
        fetch_branch_head=_branch,
        fetch_manifest=_manifest_fetcher,
        fetch_compare=_compare,
    )
    return candidate, calls


class EvaluateDownstreamTests(unittest.TestCase):
    """Per-downstream skip / dispatch decision logic in `evaluate_downstream`."""

    def test_branch_unchanged_short_circuits(self) -> None:
        """Scenario: branch HEAD == status.downstream_commit ⇒ no manifest fetch."""
        candidate, calls = _evaluate(branch_head=_BRANCH_OLD)
        self.assertIsNone(candidate)
        self.assertEqual(calls["branch"], [("org/PhysLib", "main")])
        self.assertEqual(calls["manifest"], [])
        self.assertEqual(calls["compare"], [])

    def test_pin_unchanged_skipped(self) -> None:
        """Scenario: branch moved but manifest still pins the same mathlib SHA."""
        candidate, calls = _evaluate(manifest=_manifest(_OLD_PIN))
        self.assertIsNone(candidate)
        self.assertEqual(calls["compare"], [])  # never reached the compare call

    def test_no_fkb_skipped_before_compare(self) -> None:
        """Scenario: pin moved but no active regression ⇒ compare API never called."""
        candidate, calls = _evaluate(status=_status(fkb=None))
        self.assertIsNone(candidate)
        self.assertEqual(calls["compare"], [])

    def test_compare_behind_skipped(self) -> None:
        """Scenario: new pin is in the safe range below FKB ⇒ skip."""
        candidate, _ = _evaluate(compare="behind")
        self.assertIsNone(candidate)

    def test_compare_diverged_skipped(self) -> None:
        """Scenario: divergent upstream history ⇒ defensive skip."""
        candidate, _ = _evaluate(compare="diverged")
        self.assertIsNone(candidate)

    def test_compare_identical_dispatches(self) -> None:
        """Scenario: pin landed exactly on FKB ⇒ candidate emitted (active bumping)."""
        candidate, _ = _evaluate(compare="identical")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.name, "PhysLib")

    def test_compare_ahead_dispatches(self) -> None:
        """Scenario: pin moved strictly past FKB, no dedup blockers ⇒ candidate."""
        candidate, _ = _evaluate(compare="ahead")
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.current_pin, _NEW_PIN)
        self.assertEqual(candidate.fkb, _FKB)

    def test_in_flight_blocks_dispatch(self) -> None:
        """Scenario: downstream is already in a queued/in-progress run ⇒ skip."""
        candidate, _ = _evaluate(in_flight={"PhysLib"})
        self.assertIsNone(candidate)

    def test_fresh_ledger_row_blocks_dispatch(self) -> None:
        """Scenario: ledger has a recent dispatch for the same (name, pin)."""
        recent = ManifestWatcherLedgerRow(
            downstream="PhysLib",
            observed_pin=_NEW_PIN,
            dispatched_at=(_NOW - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        )
        candidate, _ = _evaluate(ledger_row=recent)
        self.assertIsNone(candidate)

    def test_stale_ledger_row_does_not_block(self) -> None:
        """Scenario: ledger row older than TTL ⇒ re-dispatch (recovery path)."""
        stale = ManifestWatcherLedgerRow(
            downstream="PhysLib",
            observed_pin=_NEW_PIN,
            dispatched_at=(_NOW - timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
        )
        candidate, _ = _evaluate(ledger_row=stale)
        self.assertIsNotNone(candidate)

    def test_ledger_row_for_different_pin_does_not_block(self) -> None:
        """Scenario: ledger remembers an older pin; the new pin is fresh news."""
        old = ManifestWatcherLedgerRow(
            downstream="PhysLib",
            observed_pin="9" * 40,
            dispatched_at=_NOW.isoformat().replace("+00:00", "Z"),
        )
        candidate, _ = _evaluate(ledger_row=old)
        self.assertIsNotNone(candidate)

    def test_missing_dependency_in_manifest_skipped(self) -> None:
        """Scenario: manifest fetched but `dependency_name` not present ⇒ skip safely."""
        candidate, _ = _evaluate(manifest={"packages": []})
        self.assertIsNone(candidate)

    def test_manifest_fetch_returned_none_skipped(self) -> None:
        """Scenario: 404 / unparseable manifest ⇒ skip without crash."""
        candidate, _ = _evaluate(manifest=None)
        self.assertIsNone(candidate)

    def test_branch_head_lookup_failure_skipped(self) -> None:
        """Scenario: GitHub branch ref API failed ⇒ skip without crash."""
        candidate, calls = _evaluate(branch_head=None)  # type: ignore[arg-type]
        self.assertIsNone(candidate)
        self.assertEqual(calls["manifest"], [])

    def test_no_status_record_treated_as_no_fkb(self) -> None:
        """Scenario: brand-new downstream, never validated ⇒ skipped (no FKB to compare)."""
        candidate, calls = _evaluate(status=None)
        self.assertIsNone(candidate)
        self.assertEqual(calls["compare"], [])


class BuildCandidatesTests(unittest.TestCase):
    """Top-level orchestration in `build_candidates` (inventory filtering, ordering)."""

    def test_disabled_downstream_excluded(self) -> None:
        """Scenario: build_candidates filters out enabled=False before any HTTP call."""
        disabled = DownstreamConfig(
            name="Disabled",
            repo="org/Disabled",
            default_branch="main",
            enabled=False,
            watch_manifest=True,
        )

        def _boom(*_args, **_kwargs):
            self.fail("disabled downstream should not be queried")

        out = watcher.build_candidates(
            inventory={"Disabled": disabled},
            statuses={},
            ledger={},
            in_flight=set(),
            upstream_repo="x/y",
            ttl=_TTL,
            now=_NOW,
            fetch_branch_head=_boom,
            fetch_manifest=_boom,
            fetch_compare=_boom,
        )
        self.assertEqual(out, [])

    def test_unwatched_downstream_excluded(self) -> None:
        """Scenario: enabled=True but watch_manifest=False ⇒ no HTTP call, no candidate."""
        unwatched = _config("Unwatched", watch_manifest=False)

        def _boom(*_args, **_kwargs):
            self.fail("unwatched downstream should not be queried")

        out = watcher.build_candidates(
            inventory={"Unwatched": unwatched},
            statuses={"Unwatched": _status()},
            ledger={},
            in_flight=set(),
            upstream_repo="x/y",
            ttl=_TTL,
            now=_NOW,
            fetch_branch_head=_boom,
            fetch_manifest=_boom,
            fetch_compare=_boom,
        )
        self.assertEqual(out, [])

    def test_mixed_inventory_only_watched_evaluated(self) -> None:
        """Scenario: mix of opted-in and opted-out entries — only the opted-in get HTTP calls."""
        inventory = {
            "Watched": _config("Watched", watch_manifest=True),
            "Unwatched": _config("Unwatched", watch_manifest=False),
        }
        statuses = {n: _status() for n in inventory}
        seen: list[str] = []

        def _branch(repo: str, branch: str) -> str:
            seen.append(repo)
            return _BRANCH_NEW

        out = watcher.build_candidates(
            inventory=inventory,
            statuses=statuses,
            ledger={},
            in_flight=set(),
            upstream_repo="x/y",
            ttl=_TTL,
            now=_NOW,
            fetch_branch_head=_branch,
            fetch_manifest=lambda repo, sha: _manifest(_NEW_PIN),
            fetch_compare=lambda upstream, base, head: "ahead",
        )
        self.assertEqual([c.name for c in out], ["Watched"])
        self.assertEqual(seen, ["org/Watched"])  # only the opted-in repo touched

    def test_no_fkb_excluded_before_http(self) -> None:
        """Scenario: opted-in but no first_known_bad_commit ⇒ no HTTP call (pre-filter)."""
        config = _config("PhysLib", watch_manifest=True)

        def _boom(*_args, **_kwargs):
            self.fail("downstream without FKB should not be queried")

        out = watcher.build_candidates(
            inventory={"PhysLib": config},
            statuses={"PhysLib": _status(fkb=None)},
            ledger={},
            in_flight=set(),
            upstream_repo="x/y",
            ttl=_TTL,
            now=_NOW,
            fetch_branch_head=_boom,
            fetch_manifest=_boom,
            fetch_compare=_boom,
        )
        self.assertEqual(out, [])

    def test_no_status_record_excluded_before_http(self) -> None:
        """Scenario: opted-in but never validated (no DB row) ⇒ no HTTP call."""
        config = _config("BrandNew", watch_manifest=True)

        def _boom(*_args, **_kwargs):
            self.fail("downstream without a status record should not be queried")

        out = watcher.build_candidates(
            inventory={"BrandNew": config},
            statuses={},  # no row for BrandNew
            ledger={},
            in_flight=set(),
            upstream_repo="x/y",
            ttl=_TTL,
            now=_NOW,
            fetch_branch_head=_boom,
            fetch_manifest=_boom,
            fetch_compare=_boom,
        )
        self.assertEqual(out, [])

    def test_candidates_returned_in_name_order(self) -> None:
        """Scenario: parallel evaluation completes in arbitrary order; output is sorted."""
        names = ["Charlie", "Alpha", "Bravo"]
        inventory = {n: _config(n) for n in names}
        statuses = {n: _status() for n in names}

        out = watcher.build_candidates(
            inventory=inventory,
            statuses=statuses,
            ledger={},
            in_flight=set(),
            upstream_repo="x/y",
            ttl=_TTL,
            now=_NOW,
            fetch_branch_head=lambda repo, branch: _BRANCH_NEW,
            fetch_manifest=lambda repo, sha: _manifest(_NEW_PIN),
            fetch_compare=lambda upstream, base, head: "ahead",
        )
        self.assertEqual([c.name for c in out], ["Alpha", "Bravo", "Charlie"])


class PinnedFromManifestPayloadTests(unittest.TestCase):
    """Helper shared between the on-disk reader and the watcher."""

    def test_dep_present_returns_rev(self) -> None:
        """Scenario: dependency listed as a git package ⇒ return its rev."""
        payload = {
            "packages": [
                {"name": "other", "type": "git", "rev": "x" * 40},
                {"name": "mathlib", "type": "git", "rev": _NEW_PIN},
            ]
        }
        self.assertEqual(pinned_from_manifest_payload(payload, "mathlib"), _NEW_PIN)

    def test_dep_absent_returns_none(self) -> None:
        """Scenario: target dependency not in packages list."""
        payload = {"packages": [{"name": "other", "type": "git", "rev": "x" * 40}]}
        self.assertIsNone(pinned_from_manifest_payload(payload, "mathlib"))

    def test_non_git_dep_returns_none(self) -> None:
        """Scenario: dep present but typed as 'path' rather than 'git'."""
        payload = {"packages": [{"name": "mathlib", "type": "path"}]}
        self.assertIsNone(pinned_from_manifest_payload(payload, "mathlib"))

    def test_malformed_payload_returns_none(self) -> None:
        """Scenario: payload is not a dict (parse failure upstream)."""
        self.assertIsNone(pinned_from_manifest_payload("not a dict", "mathlib"))

    def test_empty_rev_returns_none(self) -> None:
        """Scenario: rev field is empty string."""
        payload = {"packages": [{"name": "mathlib", "type": "git", "rev": ""}]}
        self.assertIsNone(pinned_from_manifest_payload(payload, "mathlib"))


class InFlightSetTests(unittest.TestCase):
    """Walks the GitHub Actions API to build the set of live downstream names."""

    def _fake_requests(self, responses: dict[str, object]):
        """Return a `_gh_request` stand-in that maps url-or-path → JSON payload.

        Any path not in the dict returns HTTP 404, mimicking the real API.
        """
        encoded = {key: json.dumps(val).encode() for key, val in responses.items()}

        def fake(method: str, path: str, token: str, *, body: bytes | None = None):
            if method != "GET":
                raise AssertionError(f"unexpected method {method}")
            for key, payload in encoded.items():
                if key in path:
                    return 200, payload
            return 404, b'{"message":"not found"}'

        return fake

    def test_no_runs_returns_empty(self) -> None:
        """Scenario: no queued or in-progress runs ⇒ empty set."""
        fake = self._fake_requests({
            "?status=in_progress": {"workflow_runs": []},
            "?status=queued": {"workflow_runs": []},
        })
        with mock.patch.object(watcher, "_gh_request", fake):
            out = watcher.gh_in_flight_downstreams("org/repo", "report.yml", "tok")
        self.assertEqual(out, set())

    def test_in_progress_run_jobs_collected(self) -> None:
        """Scenario: one in-progress run with select+probe jobs ⇒ names collected."""
        fake = self._fake_requests({
            "?status=in_progress": {"workflow_runs": [{"id": 42}]},
            "?status=queued": {"workflow_runs": []},
            "/runs/42/jobs": {
                "jobs": [
                    {"name": "plan"},
                    {"name": "select: A"},
                    {"name": "probe: A"},
                    {"name": "select: B"},
                    {"name": "report"},
                ],
            },
        })
        with mock.patch.object(watcher, "_gh_request", fake):
            out = watcher.gh_in_flight_downstreams("org/repo", "report.yml", "tok")
        self.assertEqual(out, {"A", "B"})

    def test_queued_and_in_progress_unioned(self) -> None:
        """Scenario: queued and in-progress runs both contribute jobs."""

        def fake(method, path, token, *, body=None):
            if "?status=in_progress" in path:
                return 200, json.dumps({"workflow_runs": [{"id": 1}]}).encode()
            if "?status=queued" in path:
                return 200, json.dumps({"workflow_runs": [{"id": 2}]}).encode()
            if "/runs/1/jobs" in path:
                return 200, json.dumps({"jobs": [{"name": "probe: A"}]}).encode()
            if "/runs/2/jobs" in path:
                return 200, json.dumps({"jobs": [{"name": "select: B"}]}).encode()
            return 404, b""

        with mock.patch.object(watcher, "_gh_request", fake):
            out = watcher.gh_in_flight_downstreams("org/repo", "report.yml", "tok")
        self.assertEqual(out, {"A", "B"})

    def test_non_matrix_jobs_ignored(self) -> None:
        """Scenario: jobs without 'select:'/'probe:' prefix never enter the set."""
        fake = self._fake_requests({
            "?status=in_progress": {"workflow_runs": [{"id": 7}]},
            "?status=queued": {"workflow_runs": []},
            "/runs/7/jobs": {"jobs": [{"name": "plan"}, {"name": "report"}]},
        })
        with mock.patch.object(watcher, "_gh_request", fake):
            out = watcher.gh_in_flight_downstreams("org/repo", "report.yml", "tok")
        self.assertEqual(out, set())


class DispatchPayloadTests(unittest.TestCase):
    """Body and success-handling for gh_dispatch_workflow."""

    def test_204_returns_true_with_correct_payload(self) -> None:
        """Scenario: HTTP 204 ⇒ True; body carries `ref` and `inputs.downstream`."""
        captured: dict = {}

        def fake(method, path, token, *, body=None):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return 204, b""

        with mock.patch.object(watcher, "_gh_request", fake):
            ok = watcher.gh_dispatch_workflow(
                "org/repo", "report.yml", "main", {"downstream": "A,B"}, "tok"
            )
        self.assertTrue(ok)
        self.assertEqual(captured["method"], "POST")
        self.assertIn("workflows/report.yml/dispatches", captured["path"])
        decoded = json.loads(captured["body"])
        self.assertEqual(decoded, {"ref": "main", "inputs": {"downstream": "A,B"}})

    def test_non_204_returns_false(self) -> None:
        """Scenario: HTTP 422 ⇒ False, error logged, caller will not write the ledger."""

        def fake(method, path, token, *, body=None):
            return 422, b'{"message":"Unprocessable"}'

        with mock.patch.object(watcher, "_gh_request", fake):
            ok = watcher.gh_dispatch_workflow(
                "org/repo", "report.yml", "main", {"downstream": "A"}, "tok"
            )
        self.assertFalse(ok)


class _RecordingBackend:
    """Minimal in-memory backend that records the ledger upserts main() makes."""

    def __init__(self) -> None:
        self.statuses: dict[str, DownstreamStatusRecord] = {"PhysLib": _status()}
        self.ledger: dict[str, ManifestWatcherLedgerRow] = {}
        self.upserts: list[list[ManifestWatcherLedgerRow]] = []

    def load_all_statuses(self, workflow, upstream):
        return self.statuses

    def load_manifest_watcher_ledger(self, upstream):
        return self.ledger

    def upsert_manifest_watcher_ledger(self, upstream, rows):
        self.upserts.append(list(rows))


class MainOrchestrationTests(unittest.TestCase):
    """End-to-end main() — verifies dispatch + ledger only fire on the live path."""

    def _run_main(self, *, dry_run: bool) -> _RecordingBackend:
        backend = _RecordingBackend()
        argv = [
            "check_downstream_manifests.py",
            "--inventory", "(unused; load_inventory is patched)",
            "--upstream", "leanprover-community/mathlib4",
        ]
        if dry_run:
            argv.append("--dry-run-dispatch")

        env = {"GITHUB_TOKEN": "tok", "GH_REPO": "org/downstream-reports"}
        inventory = {"PhysLib": _config("PhysLib")}

        dispatched: list[dict] = []

        def fake_dispatch(repo, workflow_file, ref, inputs, token):
            dispatched.append({"inputs": inputs})
            return True

        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(watcher, "create_backend", return_value=backend), \
             mock.patch.object(watcher, "load_inventory", return_value=inventory), \
             mock.patch.object(watcher, "gh_in_flight_downstreams", return_value=set()), \
             mock.patch.object(watcher, "gh_get_branch_head", return_value=_BRANCH_NEW), \
             mock.patch.object(watcher, "gh_get_raw_manifest", return_value=_manifest(_NEW_PIN)), \
             mock.patch.object(watcher, "gh_compare_status", return_value="ahead"), \
             mock.patch.object(watcher, "gh_dispatch_workflow", side_effect=fake_dispatch):
            rc = watcher.main()
        self.assertEqual(rc, 0)
        backend.dispatched = dispatched  # type: ignore[attr-defined]
        return backend

    def test_dry_run_does_not_dispatch_or_write_ledger(self) -> None:
        """Scenario: --dry-run-dispatch ⇒ candidate logged but no API call, no ledger row."""
        backend = self._run_main(dry_run=True)
        self.assertEqual(backend.dispatched, [])  # type: ignore[attr-defined]
        self.assertEqual(backend.upserts, [])

    def test_live_path_dispatches_and_writes_ledger(self) -> None:
        """Scenario: live path ⇒ one dispatch with comma-joined names, ledger upserted."""
        backend = self._run_main(dry_run=False)
        self.assertEqual(len(backend.dispatched), 1)  # type: ignore[attr-defined]
        self.assertEqual(
            backend.dispatched[0]["inputs"], {"downstream": "PhysLib"}  # type: ignore[attr-defined]
        )
        self.assertEqual(len(backend.upserts), 1)
        [row] = backend.upserts[0]
        self.assertEqual(row.downstream, "PhysLib")
        self.assertEqual(row.observed_pin, _NEW_PIN)


if __name__ == "__main__":
    unittest.main()
