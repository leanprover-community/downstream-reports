#!/usr/bin/env python3
"""Tests for check_downstream_manifests.py — the regression-run watcher.

The watcher's pure decision logic (`evaluate_downstream` / `build_candidates`)
is exercised here directly with injected fetcher callables; the HTTP layer is
covered by `InFlightSetTests` and `DispatchPayloadTests` via monkey-patched
`_gh_request`.
"""

from __future__ import annotations

import json
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
_TTL = timedelta(hours=6)


def _config(
    name: str = "PhysLib", *, default_branch: str = "main", dependency_name: str = "mathlib"
) -> DownstreamConfig:
    """Minimal enabled DownstreamConfig."""
    return DownstreamConfig(
        name=name,
        repo=f"org/{name}",
        default_branch=default_branch,
        dependency_name=dependency_name,
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


class BuildCandidatesTests(unittest.TestCase):
    """Skip / dispatch decision logic for one downstream."""

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

    def test_disabled_downstream_excluded_by_build_candidates(self) -> None:
        """Scenario: build_candidates filters out enabled=False before any HTTP call."""
        disabled = DownstreamConfig(
            name="Disabled",
            repo="org/Disabled",
            default_branch="main",
            enabled=False,
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


if __name__ == "__main__":
    unittest.main()
