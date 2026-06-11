#!/usr/bin/env python3
"""
Tests for: scripts.generate_site

Coverage scope:
    - Time helpers (``_as_datetime``, ``iso_epoch``, ``fmt_dt``,
      ``fmt_duration``, ``days_between``) — both ISO-string and datetime
      inputs.
    - ``detail_narrative`` — the plain-English summary wording per outcome,
      including the adjacent-LKG/FKB phrasing and its truncated-window hedge.
    - ``render_window_strip`` — node merging, the adjacent junction, the
      unknown-break segment, and segment commit-distance labels derived from
      age/bump.
    - ``render_chart`` — row inclusion/exclusion, dual log/linear coordinates,
      vertical alignment of shared first-known-bad commits, and the
      shared-culprit callout.
    - ``render_history_strip`` / ``storage.load_recent_outcomes`` — the
      run-history strip and its data source.
    - ``render_table_row`` — the single-CI-link policy (validation job with
      full-run fallback), copy-SHA buttons, and filter/status attributes.
    - ``render`` — page-level fixtures: staleness warning markup, metadata,
      raw-data footer links.

Out of scope:
    - GitHub API / local-git lookup helpers (network and subprocess paths).
    - The page's client-side JavaScript behaviour.
"""

from __future__ import annotations

import re
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.generate_site import (
    HISTORY_LIMIT,
    _as_datetime,
    days_between,
    detail_narrative,
    fmt_dt,
    fmt_duration,
    iso_epoch,
    render,
    render_chart,
    render_history_strip,
    render_table_row,
    render_window_strip,
)


def _no_title(_sha):
    return None


def _make_row(**overrides) -> dict:
    """A baseline failed-with-bisect row; override the field under test."""
    row = {
        "downstream": "physlib",
        "repo": "example-org/physlib",
        "downstream_commit": "d" * 40,
        "outcome": "failed",
        "episode_state": "failing",
        "target_commit": "t" * 40,
        "last_known_good": "g" * 40,
        "first_known_bad": "b" * 40,
        "pinned_commit": "p" * 40,
        "age_commits": 10,
        "bump_commits": 7,
        "search_base_not_ancestor": False,
        "commit_window_truncated": False,
        "search_mode": "bisect",
        "failure_stage": "build",
        "error": None,
        "culprit_log_artifact_url": None,
        "job_url": "https://example.com/job/1",
        "run_url": "https://example.com/run/1",
        "row_reported_at": "2026-06-10T08:30:00Z",
    }
    row.update(overrides)
    return row


def _render_row(row: dict, **kwargs) -> str:
    return render_table_row(
        row,
        run_url="https://example.com/run/banner",
        commit_titles={},
        downstream_commit_titles={},
        sha_to_tag={},
        **kwargs,
    )


class TimeHelpersTests(unittest.TestCase):
    def test_as_datetime_accepts_iso_string_with_z(self) -> None:
        """Scenario: a Z-suffixed ISO string parses to an aware datetime."""
        dt = _as_datetime("2026-06-10T08:30:00Z")
        assert dt == datetime(2026, 6, 10, 8, 30, tzinfo=timezone.utc)

    def test_as_datetime_accepts_datetime_passthrough(self) -> None:
        """Scenario: SQL backends yield datetimes, which pass through aware."""
        aware = datetime(2026, 6, 10, 8, 30, tzinfo=timezone.utc)
        assert _as_datetime(aware) is aware

    def test_as_datetime_coerces_naive_datetime_to_utc(self) -> None:
        """Scenario: a naive datetime is treated as UTC."""
        dt = _as_datetime(datetime(2026, 6, 10, 8, 30))
        assert dt.tzinfo is not None
        assert iso_epoch(dt) == iso_epoch("2026-06-10T08:30:00Z")

    def test_as_datetime_rejects_garbage(self) -> None:
        """Scenario: unparseable input maps to None, not an exception."""
        assert _as_datetime("not a date") is None
        assert _as_datetime(None) is None

    def test_iso_epoch_round_trip(self) -> None:
        """Scenario: iso_epoch returns the Unix timestamp of the instant."""
        assert iso_epoch("1970-01-01T00:00:10Z") == 10

    def test_fmt_dt_formats_utc(self) -> None:
        """Scenario: fmt_dt renders an ISO string as YYYY-MM-DD HH:MM UTC."""
        assert fmt_dt("2026-06-10T08:30:00Z") == "2026-06-10 08:30 UTC"

    def test_fmt_dt_missing(self) -> None:
        """Scenario: a missing timestamp renders as an em dash."""
        assert fmt_dt(None) == "—"

    def test_fmt_duration_minutes_seconds(self) -> None:
        """Scenario: sub-hour durations render as Xm YYs."""
        assert fmt_duration("2026-06-10T08:00:00Z", "2026-06-10T08:12:34Z") == "12m 34s"

    def test_fmt_duration_hours(self) -> None:
        """Scenario: durations over an hour render as Xh YYm."""
        assert fmt_duration("2026-06-10T08:00:00Z", "2026-06-10T09:05:00Z") == "1h 05m"

    def test_fmt_duration_invalid_order(self) -> None:
        """Scenario: finish before start yields None rather than a negative."""
        assert fmt_duration("2026-06-10T09:00:00Z", "2026-06-10T08:00:00Z") is None

    def test_days_between(self) -> None:
        """Scenario: whole-day difference between two ISO dates."""
        assert days_between("2026-06-01T12:00:00Z", "2026-06-10T12:00:00Z") == 9
        assert days_between(None, "2026-06-10T12:00:00Z") is None


class DetailNarrativeTests(unittest.TestCase):
    def _narrate(self, row: dict) -> str:
        return detail_narrative(row, ct=_no_title, cd=_no_title, tg=_no_title)

    def test_passed_up_to_date(self) -> None:
        """Scenario: a passing row pinned at the target reads as up to date."""
        html = self._narrate(_make_row(outcome="passed", age_commits=0, bump_commits=0))
        assert "builds successfully" in html
        assert "fully up to date" in html

    def test_passed_with_bump(self) -> None:
        """Scenario: a passing row with headroom names the advance distance."""
        html = self._narrate(_make_row(outcome="passed", age_commits=5, bump_commits=5))
        assert "safely advanced by 5 commits" in html

    def test_failed_adjacent_pair_names_the_culprit(self) -> None:
        """Scenario: with both endpoints and no truncation, FKB is presented
        as the commit that introduced the break."""
        html = self._narrate(_make_row())
        assert "introduced by" in html
        assert "commit immediately before it" in html

    def test_failed_truncated_window_hedges(self) -> None:
        """Scenario: a truncated window keeps the hedged earliest-known wording."""
        html = self._narrate(_make_row(commit_window_truncated=True))
        assert "earliest known incompatible" in html
        assert "introduced by" not in html

    def test_failed_without_endpoints(self) -> None:
        """Scenario: an unbisected failure says the break is not located yet."""
        html = self._narrate(_make_row(last_known_good=None, first_known_bad=None))
        assert "not been located yet" in html

    def test_failed_detached_pin(self) -> None:
        """Scenario: a detached pin explains why no window could be searched."""
        html = self._narrate(_make_row(
            last_known_good=None, first_known_bad=None, search_base_not_ancestor=True,
        ))
        assert "not part of the target" in html

    def test_error_is_framed_as_infrastructure(self) -> None:
        """Scenario: error rows point at infrastructure, not incompatibility."""
        html = self._narrate(_make_row(outcome="error"))
        assert "infrastructure" in html


class WindowStripTests(unittest.TestCase):
    def _strip(self, row: dict) -> str:
        return render_window_strip(row, ct=_no_title, cd=_no_title, tg=_no_title)

    def test_error_and_detached_rows_render_nothing(self) -> None:
        """Scenario: error outcomes and detached pins have no commit window."""
        assert self._strip(_make_row(outcome="error")) == ""
        assert self._strip(_make_row(search_base_not_ancestor=True)) == ""

    def test_single_node_renders_nothing(self) -> None:
        """Scenario: pinned == target with no other endpoints has no extent."""
        sha = "s" * 40
        row = _make_row(
            outcome="passed", pinned_commit=sha, target_commit=sha,
            last_known_good=sha, first_known_bad=None,
        )
        assert self._strip(row) == ""

    def test_adjacent_junction_between_lkg_and_fkb(self) -> None:
        """Scenario: the LKG→FKB boundary renders as the adjacent junction."""
        html = self._strip(_make_row())
        assert "ws-adjacent" in html
        assert "Adjacent commits" in html

    def test_unbisected_failure_shows_unknown_segment_with_distance(self) -> None:
        """Scenario: with no endpoints, the segment is dashed and labelled with
        the full pinned→target distance."""
        html = self._strip(_make_row(last_known_good=None, first_known_bad=None))
        assert "ws-unknown" in html
        assert "break not yet located · 10 commits" in html

    def test_segment_distances_from_age_and_bump(self) -> None:
        """Scenario: pinned→LKG is bump; FKB→target is age − bump − 1."""
        html = self._strip(_make_row(age_commits=10, bump_commits=7))
        assert "7 commits" in html   # pinned → last known good
        assert "2 commits" in html   # first known bad → target (10 - 7 - 1)

    def test_merged_node_labels(self) -> None:
        """Scenario: coinciding commits merge into one node with joined labels."""
        sha = "s" * 40
        row = _make_row(
            outcome="passed", last_known_good=sha, target_commit=sha,
            first_known_bad=None, age_commits=4, bump_commits=4,
        )
        html = self._strip(row)
        assert "last known good = target" in html


class AdvanceMapTests(unittest.TestCase):
    def _chart(self, rows: list[dict]) -> str:
        return render_chart(rows, commit_titles={}, sha_to_tag={})

    def test_excluded_rows_are_listed_not_dropped(self) -> None:
        """Scenario: detached pins and distance-less rows appear in callouts."""
        html = self._chart([
            _make_row(),
            _make_row(downstream="detachedlib", search_base_not_ancestor=True),
            _make_row(downstream="nodatalib", age_commits=None),
        ])
        assert "detachedlib" in html
        assert "not part of the target" in html
        assert "nodatalib" in html
        assert "no commit-distance data" in html

    def test_shared_fkb_markers_align_and_get_a_callout(self) -> None:
        """Scenario: two projects with the same FKB distance-behind-target get
        markers at the identical x position and a shared-culprit callout."""
        shared_fkb = "b" * 40
        rows = [
            _make_row(downstream="alib", age_commits=10, bump_commits=7,
                      first_known_bad=shared_fkb),
            _make_row(downstream="blib", age_commits=5, bump_commits=2,
                      first_known_bad=shared_fkb),
        ]
        html = self._chart(rows)
        positions = re.findall(r'chart-marker-fkb" style="left:([\d.]+)%', html)
        assert len(positions) == 2
        assert positions[0] == positions[1]
        assert "2 projects are broken by the same commit" in html

    def test_dual_scale_coordinates(self) -> None:
        """Scenario: every bar and marker carries log and linear coordinates."""
        html = self._chart([_make_row()])
        bars = re.findall(r'class="chart-bar [^"]+" ([^>]+)>', html)
        assert bars
        for attrs in bars:
            assert "data-log-left" in attrs and "data-lin-left" in attrs
            assert "data-log-width" in attrs and "data-lin-width" in attrs
        assert 'data-scale="log"' in html

    def test_linear_bar_width_is_proportional(self) -> None:
        """Scenario: in linear coordinates, a bump of half the age yields a
        bar of half the track width."""
        html = self._chart([_make_row(age_commits=10, bump_commits=5)])
        m = re.search(r'chart-bar chart-bar-good"[^>]*data-lin-width="([\d.]+)"', html)
        assert m is not None
        assert abs(float(m.group(1)) - 50.0) < 0.01

    def test_marker_shape_is_nested_inside_the_tooltip_anchor(self) -> None:
        """Scenario: the FKB diamond's rotation lives on an inner span, so the
        tooltip pseudo-elements on the anchor render upright."""
        html = self._chart([_make_row()])
        anchor = re.search(
            r'<a class="chart-marker chart-marker-fkb"[^>]*>(.*?)</a>', html,
        )
        assert anchor is not None
        assert re.search(r'<span class="[^"]*chart-shape-fkb[^"]*"', anchor.group(1))

    def test_axis_has_target_tick_for_both_scales(self) -> None:
        """Scenario: both tick sets anchor at the target on the right edge."""
        html = self._chart([_make_row()])
        assert html.count(">target</span>") == 2  # one per scale


class HistoryStripTests(unittest.TestCase):
    def test_single_entry_renders_nothing(self) -> None:
        """Scenario: one run of history adds nothing over the row itself."""
        assert render_history_strip([{"outcome": "passed"}]) == ""

    def test_cells_are_oldest_to_newest(self) -> None:
        """Scenario: newest-first input renders oldest→newest left-to-right."""
        history = [
            {"outcome": "failed", "reported_at": "2026-06-10T08:00:00Z", "run_url": None},
            {"outcome": "passed", "reported_at": "2026-06-09T08:00:00Z", "run_url": None},
        ]
        html = render_history_strip(history)
        assert html.index("hist-passed") < html.index("hist-failed")

    def test_limit_is_enforced(self) -> None:
        """Scenario: at most HISTORY_LIMIT cells render."""
        history = [
            {"outcome": "passed", "reported_at": f"2026-05-{d:02d}", "run_url": None}
            for d in range(1, 25)
        ]
        html = render_history_strip(history)
        assert html.count("hist-cell") == HISTORY_LIMIT

    def test_different_breaking_commit_renders_orange(self) -> None:
        """Scenario: a past failure with a different first-known-bad than the
        most recent failure renders orange, naming both that break and the
        current one so the difference is verifiable from the tooltip alone."""
        history = [
            {"outcome": "failed", "first_known_bad": "a" * 40, "reported_at": "2026-06-10", "run_url": None},
            {"outcome": "failed", "first_known_bad": "z" * 40, "reported_at": "2026-06-09", "run_url": None},
            {"outcome": "failed", "first_known_bad": "a" * 40, "reported_at": "2026-06-08", "run_url": None},
        ]
        html = render_history_strip(history)
        assert html.count("hist-failed-other") == 1
        assert f"earlier incompatibility — first known bad {'z' * 7}" in html
        assert f"the current break is {'a' * 7}" in html
        assert html.count('hist-cell hist-failed"') == 2

    def test_same_breaking_commit_stays_red(self) -> None:
        """Scenario: failures from the same incompatibility all stay red."""
        history = [
            {"outcome": "failed", "first_known_bad": "a" * 40, "reported_at": "2026-06-10", "run_url": None},
            {"outcome": "failed", "first_known_bad": "a" * 40, "reported_at": "2026-06-09", "run_url": None},
        ]
        html = render_history_strip(history)
        assert "hist-failed-other" not in html

    def test_unbisected_failures_are_not_marked_different(self) -> None:
        """Scenario: a failure with no recorded first-known-bad can't be
        attributed to a different break, so it stays red."""
        history = [
            {"outcome": "failed", "first_known_bad": "a" * 40, "reported_at": "2026-06-10", "run_url": None},
            {"outcome": "failed", "first_known_bad": None, "reported_at": "2026-06-09", "run_url": None},
        ]
        html = render_history_strip(history)
        assert "hist-failed-other" not in html

    def test_cells_link_to_their_run_when_known(self) -> None:
        """Scenario: entries with a run_url render as links, others as spans."""
        history = [
            {"outcome": "passed", "reported_at": "2026-06-10", "run_url": "https://example.com/r/2"},
            {"outcome": "failed", "reported_at": "2026-06-09", "run_url": None},
        ]
        html = render_history_strip(history)
        assert 'href="https://example.com/r/2"' in html
        assert "<span class=\"hist-cell" in html


class TableRowTests(unittest.TestCase):
    def test_validation_job_link_suppresses_full_run(self) -> None:
        """Scenario: rows with job metadata show only the validation-job link."""
        html = _render_row(_make_row())
        assert "Validation job" in html
        assert "Full run" not in html

    def test_full_run_is_the_fallback_ci_link(self) -> None:
        """Scenario: rows without job metadata fall back to the run link."""
        html = _render_row(_make_row(job_url=None))
        assert "Full run" in html
        assert "Validation job" not in html

    def test_copy_buttons_for_lkg_and_fkb(self) -> None:
        """Scenario: LKG and FKB cells carry copy-SHA buttons with full SHAs."""
        row = _make_row()
        html = _render_row(row)
        assert html.count('class="copy-sha"') == 2
        assert f'data-sha="{row["last_known_good"]}"' in html
        assert f'data-sha="{row["first_known_bad"]}"' in html

    def test_no_copy_buttons_without_endpoints(self) -> None:
        """Scenario: rows without LKG/FKB render no copy buttons."""
        html = _render_row(_make_row(last_known_good=None, first_known_bad=None))
        assert "copy-sha" not in html

    def test_history_strip_is_embedded(self) -> None:
        """Scenario: per-row history renders inside the compatibility cell."""
        history = [
            {"outcome": "failed", "reported_at": "2026-06-10", "run_url": None},
            {"outcome": "passed", "reported_at": "2026-06-09", "run_url": None},
        ]
        html = _render_row(_make_row(), history=history)
        assert "history-strip" in html

    def test_status_and_filter_attributes(self) -> None:
        """Scenario: rows carry the outcome and searchable tokens for the
        client-side filter."""
        html = _render_row(_make_row())
        assert 'data-status="failed"' in html
        assert "physlib" in html.split('data-filter="')[1].split('"')[0]


class LoadRecentOutcomesTests(unittest.TestCase):
    """End-to-end coverage of the SQL history helper on in-memory SQLite."""

    _UPSTREAM = "leanprover-community/mathlib4"

    def _engine(self):
        from sqlalchemy import create_engine

        from scripts.storage import create_schema

        engine = create_engine("sqlite:///:memory:")
        create_schema(engine)
        self.addCleanup(engine.dispose)
        return engine

    def _seed_run(
        self,
        engine,
        run_id: str,
        reported_at: datetime,
        *,
        downstream: str = "physlib",
        outcome: str = "passed",
        workflow: str = "regression",
        first_known_bad: str | None = None,
    ) -> None:
        from scripts.storage import (
            DownstreamStatusRecord,
            RunResultRecord,
            SqlBackend,
        )

        result = RunResultRecord(
            upstream=self._UPSTREAM,
            downstream=downstream,
            repo=f"example-org/{downstream}",
            downstream_commit="d" * 40,
            outcome=outcome,
            episode_state="passing" if outcome == "passed" else "failing",
            target_commit="t" * 40,
            previous_last_known_good=None,
            previous_first_known_bad=None,
            last_known_good=None,
            first_known_bad=first_known_bad,
            current_last_successful=None,
            current_first_failing=None,
            failure_stage=None,
            search_mode="head-only",
            commit_window_truncated=False,
            error=None,
            head_probe_outcome=outcome,
            head_probe_failure_stage=None,
            culprit_log_text=None,
        )
        SqlBackend(engine).save_run(
            run_id=run_id,
            workflow=workflow,
            upstream=self._UPSTREAM,
            upstream_ref="refs/heads/master",
            run_url=f"https://example.com/runs/{run_id}",
            created_at=reported_at.isoformat().replace("+00:00", "Z"),
            results=[result],
            updated_statuses={downstream: DownstreamStatusRecord()},
        )

    def test_newest_first_per_downstream(self) -> None:
        """Scenario: outcomes are grouped per downstream, newest run first."""
        from scripts.storage import load_recent_outcomes

        engine = self._engine()
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        self._seed_run(engine, "1", base.replace(day=1), outcome="passed")
        self._seed_run(engine, "2", base.replace(day=2), outcome="failed",
                       first_known_bad="b" * 40)
        self._seed_run(engine, "3", base.replace(day=3), outcome="error")

        history = load_recent_outcomes(engine, self._UPSTREAM)
        assert [h["outcome"] for h in history["physlib"]] == ["error", "failed", "passed"]
        assert history["physlib"][0]["run_url"] == "https://example.com/runs/3"
        assert history["physlib"][1]["first_known_bad"] == "b" * 40

    def test_limit_is_applied_per_downstream(self) -> None:
        """Scenario: only the most recent *limit* runs are returned."""
        from scripts.storage import load_recent_outcomes

        engine = self._engine()
        for d in range(1, 6):
            self._seed_run(
                engine, str(d), datetime(2026, 6, d, tzinfo=timezone.utc),
                outcome="passed" if d < 5 else "failed",
            )
        history = load_recent_outcomes(engine, self._UPSTREAM, limit=2)
        assert [h["outcome"] for h in history["physlib"]] == ["failed", "passed"]

    def test_other_workflows_and_upstreams_excluded(self) -> None:
        """Scenario: on-demand runs and other upstreams are not history."""
        from scripts.storage import load_recent_outcomes

        engine = self._engine()
        self._seed_run(engine, "1", datetime(2026, 6, 1, tzinfo=timezone.utc))
        self._seed_run(
            engine, "2", datetime(2026, 6, 2, tzinfo=timezone.utc), workflow="ondemand",
        )
        history = load_recent_outcomes(engine, self._UPSTREAM)
        assert len(history["physlib"]) == 1
        assert load_recent_outcomes(engine, "other/upstream") == {}


class RenderPageTests(unittest.TestCase):
    def _page(self, rows: list[dict]) -> str:
        return render(
            run_id="42",
            run_url="https://example.com/run/42",
            upstream_ref="master",
            reported_at="2026-06-10T08:30:00Z",
            generated_at="2026-06-10 09:00 UTC",
            rows=rows,
            commit_titles={},
            downstream_commit_titles={},
            sha_to_tag={},
        )

    def test_stale_warning_markup_present_but_hidden(self) -> None:
        """Scenario: the staleness banner ships hidden with the report epoch
        for the client-side check."""
        html = self._page([_make_row()])
        assert 'id="stale-warning" class="stale-warning" hidden' in html
        assert 'data-reported-epoch=' in html

    def test_page_metadata_and_favicon(self) -> None:
        """Scenario: the page carries description/OpenGraph meta and an icon."""
        html = self._page([_make_row()])
        assert '<meta name="description"' in html
        assert html.count('property="og:') == 3
        assert "data:image/svg+xml" in html

    def test_raw_data_links_in_footer(self) -> None:
        """Scenario: the footer links both published JSON snapshots."""
        html = self._page([_make_row()])
        assert "lkg/latest.json" in html
        assert "runs/latest.json" in html

    def test_header_tooltips_are_focusable(self) -> None:
        """Scenario: every column-header tooltip is keyboard-reachable."""
        html = self._page([_make_row()])
        headers = re.findall(r'<th[^>]*>(<span data-tooltip="[^"]+"[^>]*>)', html)
        assert headers, "expected tooltip-bearing column headers"
        for span in headers:
            assert 'tabindex="0"' in span


if __name__ == "__main__":
    unittest.main()
