"""The reality-gap experiment script stays runnable on the current tree.

It broke silently once: two imports moved and the data-adoption gate started refusing its
window, while nothing imported or ran it. These checks use a synthetic fixture for the
engine path (never performance evidence) and the tracked adopted inputs for the data rules.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from scripts import run_reality_gap_experiments as suite


def synthetic_market(length: int = 220, count: int = 3) -> dict[str, pd.DataFrame]:
    index = pd.date_range("2023-01-01", periods=length, freq="D")
    frames = {}
    for number in range(count):
        returns = 0.004 + 0.012 * np.sin(np.arange(length) / 11 + number)
        close = 100 * np.exp(np.cumsum(returns))
        frames[f"TEST{number}-USDT"] = pd.DataFrame({
            "open": close * 0.999, "high": close * 1.004, "low": close * 0.995,
            "close": close, "volume": 5_000_000.0,
        }, index=index)
    return frames


def test_every_group_has_a_runner():
    assert tuple(suite.GROUP_RUNNERS) == suite.GROUPS


def test_cycle_windows_are_contiguous_and_end_inside_the_adopted_window():
    windows = suite.CYCLE_WINDOWS
    for (_, _, end), (_, start, _) in zip(windows, windows[1:]):
        assert pd.Timestamp(start) == pd.Timestamp(end) + pd.Timedelta(days=1)
    assert windows[0][1] == suite.DEFAULT_START and windows[-1][2] == suite.DEFAULT_END
    assert suite.OOS_WINDOW[1] == suite.DEFAULT_END
    assert pd.Timestamp(suite.OOS_WINDOW[0]) - pd.Timestamp(suite.IS_WINDOW[1]) > pd.Timedelta(days=30)


@pytest.mark.slow
def test_one_arm_runs_on_a_synthetic_market_and_keeps_the_summary_contract(tmp_path):
    frames = synthetic_market()
    summary = suite.run_arm("SMOKE", frames, output_dir=tmp_path)
    assert summary["accounting_ok"] is True
    # The engine appends one terminal row when it closes a position still open at the end.
    assert summary["bars"] in (220, 221) and summary["date_range"] == ["2023-01-01", "2023-08-08"]
    assert summary["round_trip_trades"] > 0 and summary["total_fills"] > 0
    assert summary["initial_capital"] == 10000.0 and summary["end_equity"] is not None
    for section, keys in {
        "costs": {"commission", "slippage", "impact", "financing", "total"},
        "execution": {"partial_fills", "rejected_orders", "gap_risk_resizes", "drawdown_budget_reduces"},
        "lifecycle": {"status", "raw_setups", "suppressed_raw_setups", "final_risk_multiplier"},
        "exposure": {"avg_gross_exposure", "max_gross_exposure", "pct_bars_invested"},
        "diagnostics": {"exit_by_reason", "pnl_concentration_top_n", "profit_hhi"},
    }.items():
        assert keys <= set(summary[section]), section
    json.dumps(summary, default=str)


def test_config_override_is_restored_even_when_the_body_fails():
    before = suite.config._config["strategy_health"]["enabled"]
    with pytest.raises(RuntimeError):
        with suite.override_config({"strategy_health": {"enabled": not before}}):
            assert suite.config._config["strategy_health"]["enabled"] is (not before)
            raise RuntimeError("arm failed")
    assert suite.config._config["strategy_health"]["enabled"] is before


def test_universe_data_stops_at_each_adopted_research_end():
    policy = suite._research_policy(suite.DATA_DIR)["files"]
    data_map, symbols = suite._load_universe_data("2024-01-01", suite.DEFAULT_END)
    assert len(symbols) == len(policy) == 30
    for symbol, frame in data_map.items():
        cutoff = policy[symbol.replace("-", "_") + ".csv"]["research_end_inclusive"]
        assert str(frame.index[-1])[:10] == cutoff, symbol
    assert str(data_map["MATIC-USDT"].index[-1])[:10] < suite.DEFAULT_END


def test_a_window_after_a_symbols_adopted_end_skips_it_instead_of_failing():
    data_map, _ = suite._load_universe_data("2025-01-01", "2025-01-31")
    assert "MATIC-USDT" not in data_map and "BTC-USDT" in data_map


def test_the_standard_entry_refuses_requests_beyond_the_adopted_window():
    gate = suite.verify_research_window_gate()
    assert gate["status"] == "rejected_as_expected"
    assert [p["rejected"] for p in gate["probes"]] == [True] * len(suite.GATE_PROBES)


def test_gate_probe_reports_a_folder_without_a_research_policy(tmp_path):
    bars = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                        index=pd.date_range("2026-08-01", periods=60, freq="D", name="timestamp"))
    for symbol, _ in suite.GATE_PROBES:
        bars.to_csv(tmp_path / (symbol.replace("-", "_") + ".csv"))
    assert suite.verify_research_window_gate(tmp_path)["status"] == "gate_missing"


def test_adopted_universe_agrees_with_the_adopted_data():
    universe, overrides = suite.adopted_universe()
    data_map, _ = suite._load_universe_data(suite.DEFAULT_START, suite.DEFAULT_END)
    clipped = universe.apply(data_map)  # raises when a membership outlives its data
    assert clipped["MATIC-USDT"].attrs["pit_exit"]["last_tradable_bar"].startswith("2024-09-09")
    assert bool(clipped["MATIC-USDT"]["scheduled_exit"].iloc[-1])
    for item in overrides:
        assert item["used_delisted_at"] > item["adopted_research_end"]


def test_the_tracked_universe_file_applies_to_the_adopted_data_as_is():
    """The file once ended MATIC's membership after its adopted data, which the engine refuses."""
    data_map, _ = suite._load_universe_data(suite.DEFAULT_START, suite.DEFAULT_END)
    clipped = suite.PointInTimeUniverse.from_csv(suite.UNIVERSE_CSV).apply(data_map)
    assert len(clipped) == 30
    assert suite.adopted_universe()[1] == []


def test_adopted_universe_ends_a_membership_that_outlives_its_adopted_data(tmp_path):
    stale = tmp_path / "universe.csv"
    stale.write_text(suite.UNIVERSE_CSV.read_text(encoding="utf-8").replace(
        "MATIC-USDT,2019-04-26,2024-09-10", "MATIC-USDT,2019-04-26,2025-03-25"), encoding="utf-8")
    data_map, _ = suite._load_universe_data(suite.DEFAULT_START, suite.DEFAULT_END)
    with pytest.raises(ValueError, match="Missing final legal exit bar"):
        suite.PointInTimeUniverse.from_csv(stale).apply(data_map)
    universe, overrides = suite.adopted_universe(stale)
    assert overrides == [{"symbol": "MATIC-USDT", "tracked_delisted_at": "2025-03-25",
                          "used_delisted_at": "2024-09-10", "adopted_research_end": "2024-09-09"}]
    assert len(universe.apply(data_map)) == 30


def test_adopted_universe_leaves_a_folder_without_a_policy_unchanged(tmp_path):
    universe, overrides = suite.adopted_universe(suite.UNIVERSE_CSV, tmp_path)
    assert overrides == []
    assert universe.to_manifest() == suite.PointInTimeUniverse.from_csv(suite.UNIVERSE_CSV).to_manifest()


def test_adverse_drift_matches_each_fill_to_its_own_decision():
    replay = {
        "orders": [
            {"order_id": "a", "reference_price": 100.0},
            {"order_id": "b", "reference_price": 200.0},
            {"order_id": "c", "reference_price": 50.0},
        ],
        "fills": [
            {"order_id": "a", "side": "buy", "fill_price": 101.0},   # +100 bps worse
            {"order_id": "b", "side": "buy", "fill_price": 198.0},   # -100 bps (better)
            {"order_id": "c", "side": "sell", "fill_price": 49.0},   # +200 bps worse
            {"order_id": "missing", "side": "sell", "fill_price": 1.0},
        ],
    }
    assert suite._adverse_drift_bps(replay) == {"buy_adverse_drift_bps": 0.0, "sell_adverse_drift_bps": 200.0}
    assert suite._adverse_drift_bps({"orders": [], "fills": []}) == {
        "buy_adverse_drift_bps": None, "sell_adverse_drift_bps": None}


def test_a_partial_run_never_overwrites_the_full_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(suite, "run_suite", lambda groups, output_dir: {
        "meta": {"groups": list(groups), "total_elapsed_sec": 0.0}})
    monkeypatch.setattr(suite.logging, "disable", lambda level: None)
    suite.main(["--groups", "E5", "E1", "--output-dir", str(tmp_path)])
    assert not (tmp_path / "summary.json").exists()
    partial = json.loads((tmp_path / "summary_partial.json").read_text(encoding="utf-8"))
    assert partial["meta"]["groups"] == ["E1", "E5"]
    suite.main(["--output-dir", str(tmp_path)])
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))["meta"]["groups"] == list(suite.GROUPS)
