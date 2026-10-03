from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from analysis.paper_study import (
    ARMS, arm_parameters, chronological_label_baseline, equity_returns,
    factor_proxies, freeze_registration, load_verified_inputs,
)
from config.config import config
from core.reproducibility import sha256_file


def test_frozen_registration_rejects_drift_without_overwrite(tmp_path):
    path = tmp_path / "registration.json"
    identity = {"arms": list(ARMS), "data": "abc", "cost": 1.0}
    original = freeze_registration(path, identity)
    assert freeze_registration(path, deepcopy(identity)) == original
    with pytest.raises(ValueError, match="identity changed"):
        freeze_registration(path, {**identity, "cost": 1.5})
    assert json.loads(path.read_text()) == identity


def test_research_arms_keep_hard_controls_and_restore_base():
    base = deepcopy(config._config)
    before = deepcopy(base)
    for arm in ARMS:
        parameters = arm_parameters(base, arm, multiplier=1.5)
        assert parameters["risk"] == before["risk"]
        assert parameters["strategy_governance"] == before["strategy_governance"]
        assert parameters["execution"]["commission_rate_taker"] == pytest.approx(
            before["execution"]["commission_rate_taker"] * 1.5)
        assert parameters["backtest"]["end_of_backtest_mode"] == "forced_liquidation"
        assert not parameters["signal_meta_replay"]["enabled"]
    assert base == before


def test_manifest_hash_and_path_escape_are_rejected(tmp_path):
    index = pd.date_range("2020-01-01", periods=3)
    frame = pd.DataFrame({"open": [1., 2., 3.], "high": [2., 3., 4.],
                          "low": [.5, 1., 2.], "close": [1., 2., 3.],
                          "volume": [1., 1., 1.]}, index=index)
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index_label="timestamp")
    entry = {"file": "bars.csv", "sha256": sha256_file(path), "rows": 3,
             "first": str(index[0]), "last": str(index[-1])}
    manifest = {"exchange": "binance", "market_type": "spot", "timeframe": "1d",
                "symbols": {"BTC/USDT": entry, "ETH/USDT": entry.copy()}}
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(manifest))
    frames, _ = load_verified_inputs(target)
    assert len(frames) == 2
    manifest["symbols"]["BTC/USDT"]["file"] = "../outside.csv"
    target.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="escapes"):
        load_verified_inputs(target)
    manifest["symbols"]["BTC/USDT"]["file"] = "bars.csv"
    manifest["symbols"]["BTC/USDT"]["sha256"] = "wrong"
    target.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="hash mismatch"):
        load_verified_inputs(target)


def test_factor_position_is_lagged_and_future_does_not_change_prefix():
    index = pd.date_range("2020-01-01", periods=100)
    frames = {"BTC": pd.DataFrame({"close": np.exp(np.arange(100) * .01)}, index=index),
              "ETH": pd.DataFrame({"close": np.exp(np.arange(100) * .005)}, index=index)}
    original = factor_proxies(frames)
    mutated = {s: f.copy() for s, f in frames.items()}
    mutated["BTC"].iloc[81:, 0] *= 100
    pd.testing.assert_frame_equal(original.iloc[:81], factor_proxies(mutated).iloc[:81])
    assert original.momentum_spread.iloc[:61].isna().all()


def test_label_baseline_excludes_overlapping_unavailable_training_labels():
    rows = [
        {"candidate_id": "old", "entry_time": "2020-01-01", "available_at": "2020-01-03", "net_return_bps": 10., "training_eligible": True},
        {"candidate_id": "overlap", "entry_time": "2020-01-02", "available_at": "2020-01-20", "net_return_bps": 9999., "training_eligible": True},
        {"candidate_id": "query", "entry_time": "2020-01-04", "available_at": "2020-01-06", "net_return_bps": -10., "training_eligible": True},
    ]
    report = chronological_label_baseline(rows, minimum_train=1)
    prediction = next(r for r in report["predictions"] if r["candidate_id"] == "query")
    assert prediction["training_count"] == 1
    assert prediction["predicted_net_bps"] == 10.
    assert prediction["latest_training_label_at"] == "2020-01-03"


def test_equity_daily_returns_include_first_bar_and_terminal_cash_tail():
    timeline = pd.date_range("2021-01-01", periods=4)
    equity = pd.DataFrame({"equity": [9900., 9800.]}, index=timeline[:2])
    curve, returns = equity_returns(equity, timeline)
    assert curve.iloc[-1] == 9800.
    assert (1 + returns).prod() - 1 == pytest.approx(-.02)
    assert returns.iloc[-1] == 0.


def test_terminal_liquidation_cost_remains_in_last_daily_return():
    timeline = pd.date_range("2021-01-01", periods=2)
    raw_times = pd.DatetimeIndex([timeline[0], timeline[1], timeline[1] + pd.Timedelta(seconds=1)])
    equity = pd.DataFrame({"equity": [10000., 10500., 10400.]}, index=raw_times)
    curve, returns = equity_returns(equity, timeline)
    assert curve.iloc[-1] == 10400.
    assert returns.iloc[-1] == pytest.approx(.04)


def test_missing_equity_bars_are_not_silently_filled():
    timeline = pd.date_range("2021-01-01", periods=4)
    for indices in (timeline[1:3], timeline[[0, 2]]):
        with pytest.raises(ValueError, match="Missing leading or internal"):
            equity_returns(pd.DataFrame({"equity": [10000., 9900.]}, index=indices), timeline)
    with pytest.raises(ValueError, match="Nonfinite observed"):
        equity_returns(pd.DataFrame({"equity": [10000., np.nan]}, index=timeline[:2]), timeline)


def test_label_queries_do_not_depend_on_future_maturity_or_ambiguity():
    common = {"entry_time": "2020-01-03", "signal_available_at": "2020-01-03"}
    rows = [{"candidate_id": "old", "entry_time": "2020-01-01", "available_at": "2020-01-02",
             "net_return_bps": 10., "training_eligible": True},
            {**common, "candidate_id": "ambiguous", "net_return_bps": -10., "training_eligible": False},
            {**common, "candidate_id": "immature", "net_return_bps": None, "training_eligible": False}]
    result = chronological_label_baseline(rows, minimum_train=1)
    by_id = {r["candidate_id"]: r for r in result["predictions"]}
    assert by_id["ambiguous"]["predicted_net_bps"] == 10.
    assert by_id["immature"]["predicted_net_bps"] == 10.
    assert result["evaluation_excluded"] == 2
