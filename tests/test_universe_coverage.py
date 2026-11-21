"""The point-in-time universe has to agree with the adopted data (reality-gap RG-10).

The tracked universe once ended MATIC's membership half a year after its adopted data,
which made the engine refuse the adopted 30-symbol set. These checks name each kind of
disagreement on constructed inputs and keep the repository itself free of blocking ones.
"""
from __future__ import annotations

import json

import pytest

from scripts import data_inputs

COMMON_END = "2026-08-26"


def write_inputs(tmp_path, universe_rows, files, *, policy=True):
    """``files``: name -> (first, last, adopted_end or None)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    manifest = {"schema_version": data_inputs.SCHEMA, "files": {
        name: {"symbol": name[:-4].replace("_", "/"), "first": f"{first}T00:00:00", "last": f"{last}T00:00:00"}
        for name, (first, last, _) in files.items()}}
    (data_dir / data_inputs.FROZEN_MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    if policy:
        body = {"common_research_end_inclusive": COMMON_END, "files": {
            name: {"research_end_inclusive": adopted or last} for name, (_, last, adopted) in files.items()}}
        (data_dir / data_inputs.RESEARCH_POLICY).write_text(json.dumps(body), encoding="utf-8")
    universe = tmp_path / "universe.csv"
    lines = ["symbol,listed_at,delisted_at,source"] + [",".join(row) + ",fixture" for row in universe_rows]
    universe.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return universe, data_dir


def kinds(findings):
    return [(item["symbol"], item["kind"], item["blocking"]) for item in findings]


CONSISTENT_FILES = {
    "AAA_USDT.csv": ("2020-01-01", "2026-09-19", COMMON_END),      # a raw file may run past its adopted end
    "OLD_USDT.csv": ("2019-04-26", "2024-09-09", "2024-09-09"),
}
CONSISTENT_ROWS = [("AAA-USDT", "2020-01-01", ""), ("OLD-USDT", "2019-04-26", "2024-09-10")]


def test_consistent_inputs_have_no_findings(tmp_path):
    universe, data_dir = write_inputs(tmp_path, CONSISTENT_ROWS, CONSISTENT_FILES)
    assert data_inputs.universe_coverage(universe, data_dir) == []


def test_a_membership_that_ends_after_its_data_blocks(tmp_path):
    rows = [CONSISTENT_ROWS[0], ("OLD-USDT", "2019-04-26", "2025-03-25")]       # the recorded MATIC case
    universe, data_dir = write_inputs(tmp_path, rows, CONSISTENT_FILES)
    findings = data_inputs.universe_coverage(universe, data_dir)
    assert kinds(findings) == [("OLD-USDT", "membership_outlives_data", True)]
    assert "expected 2024-09-10" in findings[0]["detail"]


def test_data_that_stops_early_without_a_delisting_date_blocks(tmp_path):
    rows = [CONSISTENT_ROWS[0], ("OLD-USDT", "2019-04-26", "")]
    universe, data_dir = write_inputs(tmp_path, rows, CONSISTENT_FILES)
    assert kinds(data_inputs.universe_coverage(universe, data_dir)) == [("OLD-USDT", "membership_outlives_data", True)]


def test_bars_on_or_after_the_delisting_date_block(tmp_path):
    rows = [CONSISTENT_ROWS[0], ("OLD-USDT", "2019-04-26", "2024-09-09")]
    universe, data_dir = write_inputs(tmp_path, rows, CONSISTENT_FILES)
    assert kinds(data_inputs.universe_coverage(universe, data_dir)) == [("OLD-USDT", "data_outlives_membership", True)]


def test_bars_before_the_listing_date_block(tmp_path):
    rows = [("AAA-USDT", "2020-03-01", ""), CONSISTENT_ROWS[1]]
    universe, data_dir = write_inputs(tmp_path, rows, CONSISTENT_FILES)
    assert kinds(data_inputs.universe_coverage(universe, data_dir)) == [("AAA-USDT", "data_before_listing", True)]


@pytest.mark.parametrize("window_start,blocking", [
    (None, False),              # reported only: the engine accepts a late data start
    ("2017-08-17", True),       # a member at the window start without bars
    ("2019-12-31", True),
    ("2020-01-01", False),      # bars exist from the window start on
    ("2021-06-01", False),
])
def test_a_head_gap_blocks_only_for_a_window_that_starts_inside_it(tmp_path, window_start, blocking):
    rows = [("AAA-USDT", "2017-08-17", ""), CONSISTENT_ROWS[1]]
    universe, data_dir = write_inputs(tmp_path, rows, CONSISTENT_FILES)
    findings = data_inputs.universe_coverage(universe, data_dir, window_start=window_start)
    assert kinds(findings) == [("AAA-USDT", "head_gap", blocking)]
    assert "867 days without bars" in findings[0]["detail"]


def test_symbols_missing_on_either_side_and_duplicates_block(tmp_path):
    rows = [CONSISTENT_ROWS[0], ("AAA/USDT", "2020-01-01", ""), ("NEW-USDT", "2023-01-01", "")]
    universe, data_dir = write_inputs(tmp_path, rows, CONSISTENT_FILES)
    assert kinds(data_inputs.universe_coverage(universe, data_dir)) == [
        ("AAA/USDT", "duplicate_membership", True),
        ("NEW-USDT", "missing_data", True),
        ("OLD/USDT", "missing_membership", True),
    ]


def test_without_a_research_policy_the_registered_last_bar_is_the_data_end(tmp_path):
    files = {"OLD_USDT.csv": ("2019-04-26", "2024-09-09", None)}
    universe, data_dir = write_inputs(tmp_path, [("OLD-USDT", "2019-04-26", "2024-09-10")], files, policy=False)
    assert data_inputs.universe_coverage(universe, data_dir) == []
    universe.write_text("symbol,listed_at,delisted_at,source\nOLD-USDT,2019-04-26,2024-10-01,fixture\n", encoding="utf-8")
    assert kinds(data_inputs.universe_coverage(universe, data_dir)) == [("OLD-USDT", "membership_outlives_data", True)]


def test_blocking_findings_sort_first(tmp_path):
    rows = [("AAA-USDT", "2017-08-17", ""), ("OLD-USDT", "2019-04-26", "2025-03-25")]
    universe, data_dir = write_inputs(tmp_path, rows, CONSISTENT_FILES)
    assert kinds(data_inputs.universe_coverage(universe, data_dir)) == [
        ("OLD-USDT", "membership_outlives_data", True), ("AAA-USDT", "head_gap", False)]


def test_the_repository_universe_has_no_blocking_disagreement_with_the_adopted_data():
    findings = data_inputs.universe_coverage()
    assert [item for item in findings if item["blocking"]] == []
    assert {item["kind"] for item in findings} <= {"head_gap"}


def test_every_member_has_bars_from_the_decided_research_window_start():
    """ADR 0044: thirty-symbol research starts on 2020-01-01, where BTC and ETH data begin."""
    from scripts import run_reality_gap_experiments as suite
    assert data_inputs.RESEARCH_WINDOW_START == suite.DEFAULT_START == "2020-01-01"
    findings = data_inputs.universe_coverage(window_start=data_inputs.RESEARCH_WINDOW_START)
    assert [item for item in findings if item["blocking"]] == []


def test_the_command_checks_the_research_window_start_unless_told_otherwise(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(data_inputs, "universe_coverage", lambda window_start=None: seen.append(window_start) or [])
    assert data_inputs.main(["coverage"]) == 0
    assert "(window start 2020-01-01)" in capsys.readouterr().out
    assert data_inputs.main(["coverage", "--window-start", "2017-08-17"]) == 0
    assert data_inputs.main(["coverage", "--no-window"]) == 0
    assert "(no window start)" in capsys.readouterr().out
    assert seen == ["2020-01-01", "2017-08-17", None]


def test_command_exit_code_and_streams_follow_the_blocking_findings(monkeypatch, capsys):
    reported = [{"symbol": "AAA-USDT", "kind": "head_gap", "blocking": False, "detail": "late start"}]
    monkeypatch.setattr(data_inputs, "universe_coverage", lambda window_start=None: reported)
    assert data_inputs.main(["coverage"]) == 0
    out = capsys.readouterr()
    assert "note: AAA-USDT: head_gap: late start" in out.out and "0 blocking, 1 reported only" in out.out

    blocking = reported + [{"symbol": "OLD-USDT", "kind": "membership_outlives_data", "blocking": True, "detail": "x"}]
    monkeypatch.setattr(data_inputs, "universe_coverage", lambda window_start=None: blocking)
    assert data_inputs.main(["coverage", "--window-start", "2021-06-01"]) == 1
    out = capsys.readouterr()
    assert "ERROR: OLD-USDT: membership_outlives_data: x" in out.err
    assert "1 blocking, 1 reported only (window start 2021-06-01)" in out.out


def test_the_data_gate_runs_the_coverage_check():
    from scripts import dev
    commands = dev.TASKS["data"].build([])
    assert [command[-1] for command in commands] == ["verify", "coverage"]
