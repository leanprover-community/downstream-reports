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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.models import DownstreamConfig, load_inventory


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
