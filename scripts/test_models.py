#!/usr/bin/env python3
"""
Tests for: scripts.models

Coverage scope:
    - ``DownstreamConfig`` field defaults — particularly the
      ``skip_already_good`` / ``skip_known_bad_bisect`` flags that
      gate the regression workflow's optimisation heuristics.
    - ``load_inventory`` — JSON inventory deserialisation, with
      attention to the per-downstream skip-flag overrides that let an
      operator permanently opt a downstream out of one or both
      heuristics.

Out of scope:
    - ``ValidationResult`` / ``WindowSelection`` / ``CommitDetail`` —
      exercised by the round-trip tests in ``test_validation``.
    - ``Outcome`` enum values — exercised wherever
      ``classify_exit_code`` is tested.

Why this matters
----------------
``DownstreamConfig`` is constructed by ``load_inventory`` from JSON
via ``DownstreamConfig(**entry)``.  Adding a required field to
``DownstreamConfig`` without a default would break inventory loading
immediately — the default-value tests below are the contract that
guards against accidental required-field additions.  The
``skip_*`` flags in particular default to ``True`` so the workflow's
optimisations are opt-out, not opt-in; any inversion would silently
disable the most expensive savings the regression workflow has.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.models import (
    DownstreamConfig,
    WindowSelection,
    apply_config_forwarding,
    config_from_selection,
    forwarded_config_fields,
    load_inventory,
)


class TestDownstreamConfigDefaults:
    """Field defaults on ``DownstreamConfig``."""

    def test_skip_flags_default_to_true(self) -> None:
        """Both skip heuristics default on.

        ``skip_already_good`` and ``skip_known_bad_bisect`` are the
        regression workflow's two largest cost savers.  Opt-out is the
        right default: most downstreams benefit, and the per-downstream
        flag exists for the rare cases that don't.  Inverting these
        defaults silently disables optimisations CI engineers depend on.
        """
        # Arrange / Act
        config = DownstreamConfig(name="foo", repo="owner/foo", default_branch="main")

        # Assert
        assert config.skip_already_good, (
            "skip_already_good defaults on; flipping silently disables a major optimisation"
        )
        assert config.skip_known_bad_bisect, (
            "skip_known_bad_bisect defaults on; flipping silently disables a major optimisation"
        )


class TestLoadInventorySkipFlags:
    """Inventory propagation of the per-downstream skip-flag overrides."""

    def test_inventory_can_disable_skip_already_good_per_downstream(self, tmp_path: Path) -> None:
        """A JSON inventory entry with ``skip_already_good: false`` propagates.

        Some downstreams have unreliable state-tracking (or have known
        non-deterministic builds) where the skip heuristic would
        produce wrong answers.  The inventory is the authoritative
        opt-out for those cases — ``load_inventory`` must honour the
        flag exactly, not coerce it to the default.
        """
        # Arrange
        inventory = {
            "schema_version": 1,
            "downstreams": [
                {
                    "name": "slow-downstream",
                    "repo": "owner/slow-downstream",
                    "default_branch": "main",
                    "skip_already_good": False,
                    "skip_known_bad_bisect": True,
                    "enabled": True,
                },
            ],
        }
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps(inventory))

        # Act
        loaded = load_inventory(path)

        # Assert
        assert not loaded["slow-downstream"].skip_already_good, (
            "Inventory's `skip_already_good: false` must propagate to DownstreamConfig"
        )
        assert loaded["slow-downstream"].skip_known_bad_bisect, (
            "Per-flag overrides are independent — only the flagged one flips"
        )


class TestRunsOnField:
    """Inventory propagation of the per-downstream ``runs_on`` override."""

    def test_default_is_self_hosted_pr(self) -> None:
        """``runs_on`` defaults to the self-hosted PR pool.

        Most downstreams should land on the dedicated probe fleet; the
        ``ubuntu-latest`` override exists for downstreams whose build
        needs something the self-hosted image lacks (e.g. a populated
        ``/usr/share/zoneinfo`` database).  Flipping the default would
        silently shift every probe onto GitHub-hosted minutes.
        """
        config = DownstreamConfig(name="foo", repo="owner/foo", default_branch="main")
        assert config.runs_on == ["self-hosted", "pr"], (
            "runs_on must default to the self-hosted PR pool"
        )

    def test_default_factory_returns_fresh_list(self) -> None:
        """Mutating one config's ``runs_on`` must not leak into another.

        ``field(default_factory=...)`` is the only correct way to give a
        frozen dataclass a list default; a shared literal would let one
        downstream's override silently rewire its neighbours.
        """
        a = DownstreamConfig(name="a", repo="owner/a", default_branch="main")
        b = DownstreamConfig(name="b", repo="owner/b", default_branch="main")
        a.runs_on.append("smoke-test-mutation")
        assert b.runs_on == ["self-hosted", "pr"], (
            "Each instance must own a fresh list, not share the default"
        )

    def test_inventory_override_propagates(self, tmp_path: Path) -> None:
        """Inventory ``runs_on: ["ubuntu-latest"]`` reaches DownstreamConfig.

        The probe job reads the labels from the matrix; the matrix is
        seeded from inventory.  If ``load_inventory`` dropped the field,
        every probe would land on the default pool regardless of
        configuration — exactly the bug this knob exists to avoid.
        """
        inventory = {
            "schema_version": 1,
            "downstreams": [
                {
                    "name": "Robo",
                    "repo": "hhu-adam/Robo",
                    "default_branch": "main",
                    "enabled": True,
                    "runs_on": ["ubuntu-latest"],
                },
            ],
        }
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps(inventory))

        loaded = load_inventory(path)

        assert loaded["Robo"].runs_on == ["ubuntu-latest"], (
            "Inventory's `runs_on` override must propagate to DownstreamConfig"
        )


class TestNukeLakedirFlag:
    """Inventory propagation of the per-downstream ``nuke_lakedir`` opt-in."""

    def test_default_off(self) -> None:
        """``nuke_lakedir`` is opt-in: paranoia knob, not the default.

        Wiping ``.lake/`` between probes adds rebuild time on every
        commit; only downstreams demonstrating stale-artifact symptoms
        (e.g. "ProofWidgets not up-to-date") should pay that cost.
        """
        config = DownstreamConfig(name="foo", repo="owner/foo", default_branch="main")
        assert not config.nuke_lakedir, "nuke_lakedir must default to False"

    def test_inventory_opt_in_propagates(self, tmp_path: Path) -> None:
        """``nuke_lakedir: true`` in inventory JSON reaches DownstreamConfig.

        The probe step keys on this flag to set HOPSCOTCH_DEBUG_NUKE_LAKEDIR;
        if loading silently drops it, affected downstreams would keep
        bisecting into false culprits.
        """
        inventory = {
            "schema_version": 1,
            "downstreams": [
                {
                    "name": "BrauerGroup",
                    "repo": "Whysoserioushah/BrauerGroup",
                    "default_branch": "main",
                    "enabled": True,
                    "nuke_lakedir": True,
                },
            ],
        }
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps(inventory))

        loaded = load_inventory(path)

        assert loaded["BrauerGroup"].nuke_lakedir, (
            "Inventory's `nuke_lakedir: true` must propagate to DownstreamConfig"
        )


class TestVerifyStepsAndBuildArgs:
    """Inventory propagation of the verify-step and build-argument knobs."""

    def test_defaults_are_off_and_empty(self) -> None:
        """``run_test``/``run_lint`` default off and the ``*_args`` default empty.

        Most downstreams have no test/lint driver wired up, and hopscotch
        aborts a run whose enabled verify step has no driver — so the
        extra steps and arguments must be strict opt-ins, not defaults.
        """
        config = DownstreamConfig(name="foo", repo="owner/foo", default_branch="main")
        assert not config.run_test and not config.run_lint
        assert config.build_args == [] and config.test_args == [] and config.lint_args == []

    def test_inventory_settings_propagate(self, tmp_path: Path) -> None:
        """Inventory verify-step / build-argument settings reach DownstreamConfig.

        The probe step keys on these to pass hopscotch's --test / --lint
        / --build-args / --test-args / --lint-args; if loading silently
        dropped them, the downstream would quietly revert to a plain
        ``lake build`` with no extra arguments.
        """
        inventory = {
            "schema_version": 1,
            "downstreams": [
                {
                    "name": "withTests",
                    "repo": "owner/withTests",
                    "default_branch": "main",
                    "enabled": True,
                    "run_test": True,
                    "run_lint": True,
                    "build_args": ["-Kenv=dev"],
                    "test_args": ["--verbose"],
                    "lint_args": ["--update"],
                },
            ],
        }
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps(inventory))

        loaded = load_inventory(path)["withTests"]

        assert loaded.run_test and loaded.run_lint
        assert loaded.build_args == ["-Kenv=dev"]
        assert loaded.test_args == ["--verbose"]
        assert loaded.lint_args == ["--update"]


class TestTargetMode:
    """The ``target_mode`` field (master vs next-release)."""

    def test_default_is_next_release(self) -> None:
        """Omitting target_mode opts into release-stepping (the fleet default):
        target the next release tag, falling back to master HEAD when caught up.

        Reverting this default to "master" would stop every downstream stepping
        through release tags, so the default is asserted explicitly.
        """
        config = DownstreamConfig(name="foo", repo="owner/foo", default_branch="main")
        assert config.target_mode == "next-release"

    def test_invalid_value_raises(self) -> None:
        """A typo'd target_mode is rejected at construction, not silently
        treated as 'master' (which would hide the misconfiguration)."""
        with pytest.raises(ValueError, match="invalid target_mode"):
            DownstreamConfig(
                name="foo",
                repo="owner/foo",
                default_branch="main",
                target_mode="nextrelease",
            )

    def test_inventory_master_override_propagates(self, tmp_path: Path) -> None:
        """The non-default ``target_mode: "master"`` opt-out reaches
        DownstreamConfig (testing a non-default value, since "next-release" is
        now the default and would pass even if propagation were broken)."""
        inventory = {
            "downstreams": [
                {
                    "name": "Bleeding",
                    "repo": "owner/Bleeding",
                    "default_branch": "main",
                    "enabled": True,
                    "target_mode": "master",
                },
            ],
        }
        path = tmp_path / "inventory.json"
        path.write_text(json.dumps(inventory))

        loaded = load_inventory(path)

        assert loaded["Bleeding"].target_mode == "master", (
            "Inventory's `target_mode` override must propagate to DownstreamConfig"
        )


class TestConfigForwarding:
    """The name-driven config → selection → probe forwarding contract."""

    # Non-default value for every forwarded field, so the round-trip test
    # proves each one actually travels rather than matching by default.
    _FORWARDED_OVERRIDES = {
        "skip_known_bad_bisect": False,
        "revalidate_boundary": True,
        "nuke_lakedir": True,
        "run_test": True,
        "run_lint": True,
        "build_args": ["-Kwerror"],
        "test_args": ["--fast"],
        "lint_args": ["--strict"],
    }

    def test_forwarded_fields_round_trip_through_selection(self) -> None:
        """Every forwarded field survives config → selection → reconstructed
        config, and the forwarded set is exactly the non-identity fields the
        two dataclasses share.

        This is the contract that makes a new tool flag a two-dataclass
        change: declare it on ``DownstreamConfig`` and ``WindowSelection``
        and the select scripts forward it while the probe reads it back —
        no per-script plumbing.  A field listed here but missing from the
        round-trip means the forwarding helper regressed.
        """
        # Arrange
        assert set(forwarded_config_fields()) == set(self._FORWARDED_OVERRIDES), (
            "forwarded set changed: update _FORWARDED_OVERRIDES with a "
            "non-default value for the new field so the round-trip covers it"
        )
        config = DownstreamConfig(
            name="foo",
            repo="owner/foo",
            default_branch="main",
            dependency_name="mathlib",
            **self._FORWARDED_OVERRIDES,
        )
        selection = WindowSelection(
            downstream=config.name,
            repo=config.repo,
            default_branch=config.default_branch,
            dependency_name=config.dependency_name,
        )

        # Act
        apply_config_forwarding(selection, config)
        rebuilt = config_from_selection(
            WindowSelection.from_json(selection.to_json())
        )

        # Assert — identity fields plus every forwarded field survive the
        # trip through selection.json serialisation.
        assert rebuilt.name == config.name
        assert rebuilt.repo == config.repo
        assert rebuilt.default_branch == config.default_branch
        assert rebuilt.dependency_name == config.dependency_name
        for field_name in forwarded_config_fields():
            assert getattr(rebuilt, field_name) == getattr(config, field_name), (
                f"{field_name} did not survive the config → selection → config round-trip"
            )

    def test_exclude_leaves_selection_default_in_place(self) -> None:
        """Scenario: the on-demand select leg excludes ``revalidate_boundary``
        so the bumping branch (which moves the manifest by design) never
        triggers the manifest-unchanged revalidation guard."""
        # Arrange
        config = DownstreamConfig(
            name="foo", repo="owner/foo", default_branch="main",
            **self._FORWARDED_OVERRIDES,
        )
        selection = WindowSelection(downstream="foo", repo="owner/foo", default_branch="main")

        # Act
        apply_config_forwarding(selection, config, exclude={"revalidate_boundary"})

        # Assert
        assert selection.revalidate_boundary is False, (
            "excluded fields keep their WindowSelection default"
        )
        assert selection.nuke_lakedir is True, (
            "non-excluded fields are still forwarded"
        )
