"""Real V3 execution on declared synthetic fixtures; never performance evidence."""

from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from config.config import config
from scripts import run_trend_portfolio_v3 as runner


def synthetic_market(length=252, count=6):
    index = pd.date_range("2023-01-01", periods=length, freq="D")
    frames = {}
    for number in range(count):
        returns = .004 + .001 * np.sin(np.arange(length) / 9 + number / 5)
        close = 100 * np.exp(np.cumsum(returns))
        frames[f"TEST{number}-USDT"] = pd.DataFrame({
            "open": close * .999, "high": close * 1.001, "low": close * .998,
            "close": close, "volume": 1_000_000., "quote_volume": 50_000_000.,
            "close_time": index + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1),
        }, index=index)
    metadata = {symbol: {
        "source_kind": "synthetic_test_fixture_not_market_evidence",
        "source_status": "verified", "classification": "crypto",
        "classification_available_at": "2020-01-01T00:00:00Z",
        "listing_effective_at": "2020-01-01T00:00:00Z",
        "listing_available_at": "2020-01-01T00:00:00Z", "events": [],
    } for symbol in frames}
    return frames, metadata


def spec(variant="momentum", financing="assumed", suffix="main"):
    return {"run_id": f"synthetic_fixture_{variant}_{financing}_{suffix}", "variant": variant,
            "financing_mode": financing, "role": "engineering_fixture",
            "start": "2023-07-20", "end": "2023-09-09", "cost_multiplier": 1.,
            "borrow_rate": .08, "horizons": [60, 120], "min_quote_volume": 5_000_000.}


@pytest.mark.parametrize("variant", ["momentum", "breakout"])
def test_real_runner_weekly_targets_protective_stops_financing_and_valuation_only(tmp_path, variant):
    frames, metadata = synthetic_market()
    before = config._config
    baseline = deepcopy(before)
    run = spec(variant)
    summary = runner.run_one(run, frames, metadata, baseline, tmp_path)
    assert config._config is before
    assert config._config == baseline
    assert summary["status"] == "completed"
    assert summary["sample_classification"] == "synthetic_engineering_fixture"
    assert summary["accounting_check"]["ok"]
    assert summary["fill_count"] > 6
    output = tmp_path / "runs" / run["run_id"]
    trades = pd.read_csv(output / "trades.csv")
    buys = trades.loc[trades.side.eq("buy")]
    assert len(buys.symbol.unique()) == len(frames)
    assert pd.to_datetime(buys.fill_time).dt.to_period("W").nunique() > 1
    assert not trades.exit_reason.fillna("").str.contains("EndOfBacktest", case=False).any()
    terminal = json.loads((output / "terminal_valuation.json").read_text())
    assert terminal["policy"] == "valuation_only"
    assert terminal["synthetic_fill_count"] == 0
    assert terminal["positions"]
    assert summary["financing_gross"] > 0
    audit = pd.read_csv(output / "stop_order_audit.csv")
    assert not audit.empty
    curve = pd.read_csv(output / "equity.csv")
    assert curve.equity.notna().all()
    close_events = json.loads((output / "close_events.json").read_text())
    assert all(row["exit_reason"] != "EndOfBacktest" for row in close_events)
    controller = json.loads((output / "portfolio_controller.json").read_text())
    assert not any(row.get("reason") == "unreconciled_orders" for row in controller["audit"])


def test_verified_financing_mode_never_borrows_without_evidence(tmp_path):
    frames, metadata = synthetic_market()
    run = spec(financing="verified_only")
    summary = runner.run_one(run, frames, metadata, deepcopy(config._config), tmp_path)
    assert summary["fill_count"] > 0
    assert summary["financing_gross"] == 0
    assert summary["accounting_check"]["ok"]
    audit = json.loads((tmp_path / "runs" / run["run_id"] / "portfolio_controller.json").read_text())["audit"]
    assert max(row.get("gross_weight", 0) for row in audit) <= 1 + 1e-7


def test_pending_order_recovery_preserves_real_fill_stream(tmp_path, monkeypatch):
    frames, metadata = synthetic_market(length=224)
    baseline = deepcopy(config._config)
    control_spec = spec(suffix="recovery_control")
    recovered_spec = spec(suffix="recovery_restored")
    control_spec["end"] = recovered_spec["end"] = "2023-08-12"
    control = runner.run_one(control_spec, frames, metadata, baseline, tmp_path)
    factory = runner.make_controller
    restoration = []

    def restoring_factory(*args, **kwargs):
        class RestartingController:
            def __init__(self):
                self.active = factory(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.active, name)

            def process(self, **event_args):
                if self.active._state["orders"] and not restoration:
                    checkpoint = self.active.checkpoint()
                    replacement = factory(*args, **kwargs)
                    assert replacement is not self.active
                    replacement.restore(checkpoint)
                    self.active = replacement
                    restoration.append(checkpoint["sha256"])
                return self.active.process(**event_args)

        return RestartingController()

    monkeypatch.setattr(runner, "make_controller", restoring_factory)
    recovered = runner.run_one(recovered_spec, frames, metadata, baseline, tmp_path)
    assert restoration
    columns = ["symbol", "side", "qty", "fill_price", "fill_time", "exit_reason"]
    expected = pd.DataFrame(control["trades"])[columns]
    actual = pd.DataFrame(recovered["trades"])[columns]
    pd.testing.assert_frame_equal(actual, expected)
    assert recovered["accounting_check"]["ok"]


def test_known_delisting_exits_before_effective_halt_and_never_uses_later_bars(tmp_path):
    frames, metadata = synthetic_market(length=224)
    metadata["TEST0-USDT"]["events"] = [{
        "kind": "spot_delisted", "effective_at": "2023-07-29T08:00:00Z",
        "available_at": "2023-07-26T12:00:00Z", "source_status": "verified",
    }]
    run = spec(suffix="announced_delisting")
    run["end"] = "2023-08-12"
    summary = runner.run_one(run, frames, metadata, deepcopy(config._config), tmp_path)
    trades = pd.DataFrame(summary["trades"])
    selected = trades.loc[trades.symbol.eq("TEST0-USDT")]
    forced = selected.loc[selected.exit_reason.eq("v3_forced_exit")]
    assert not forced.empty
    assert (pd.to_datetime(forced.fill_time, utc=True) < pd.Timestamp("2023-07-29T08:00:00Z")).all()
    assert not (pd.to_datetime(selected.fill_time, utc=True) >= pd.Timestamp("2023-07-29T00:00:00Z")).any()
    assert summary["accounting_check"]["ok"]


def test_protective_exit_suppresses_old_target_until_next_monday(tmp_path):
    frames, metadata = synthetic_market(length=216)
    frames["TEST0-USDT"].loc["2023-07-27", "low"] *= .90
    run = spec(suffix="stop_reentry")
    run["end"] = "2023-08-04"
    summary = runner.run_one(run, frames, metadata, deepcopy(config._config), tmp_path)
    trades = pd.DataFrame(summary["trades"])
    selected = trades.loc[trades.symbol.eq("TEST0-USDT")]
    protective = selected.loc[selected.exit_reason.eq("protective_stop")]
    assert not protective.empty
    first_exit = pd.to_datetime(protective.fill_time, utc=True).min()
    after = selected.loc[selected.side.eq("buy") & (pd.to_datetime(selected.fill_time, utc=True) > first_exit)]
    assert not after.empty
    assert pd.to_datetime(after.fill_time, utc=True).min() >= pd.Timestamp("2023-07-31T00:00:00Z")
    assert summary["accounting_check"]["ok"]


@pytest.mark.parametrize("variant", ["momentum", "breakout"])
def test_real_fill_and_financing_prefix_is_invariant_to_future_data_and_input_order(tmp_path, variant):
    frames, metadata = synthetic_market(length=231)
    historical_frames = {symbol: history.iloc[:224].copy() for symbol, history in frames.items()}
    baseline = deepcopy(config._config)
    prefix_spec = spec(variant, suffix="causal_prefix")
    prefix_spec["end"] = "2023-08-12"
    prefix = runner.run_one(prefix_spec, historical_frames, metadata, baseline, tmp_path)
    future_metadata = deepcopy(metadata)
    for symbol, history in frames.items():
        history.loc[history.index >= pd.Timestamp("2023-08-13"), ["open", "high", "low", "close"]] *= 3
        future_metadata[symbol]["events"] = [{"kind": "spot_delisted", "source_status": "verified",
            "available_at": "2023-08-14T12:00:00Z", "effective_at": "2023-08-19T08:00:00Z"}]
    augmented_spec = spec(variant, suffix="causal_augmented")
    augmented_spec["end"] = "2023-08-19"
    augmented = runner.run_one(augmented_spec, dict(reversed(list(frames.items()))),
                               future_metadata, baseline, tmp_path)
    columns = ["symbol", "side", "qty", "fill_price", "fill_time", "commission", "exit_reason"]
    past = pd.DataFrame(prefix["trades"])[columns]
    actual = pd.DataFrame(augmented["trades"])
    actual = actual.loc[pd.to_datetime(actual.fill_time, utc=True) <= pd.Timestamp("2023-08-12T00:00:00Z"), columns]
    pd.testing.assert_frame_equal(actual.reset_index(drop=True), past.reset_index(drop=True))
    earlier_interest = pd.read_csv(tmp_path / "runs" / prefix_spec["run_id"] / "financing_ledger.csv")
    extended_interest = pd.read_csv(tmp_path / "runs" / augmented_spec["run_id"] / "financing_ledger.csv")
    assert not earlier_interest.empty
    extended_interest = extended_interest.loc[
        pd.to_datetime(extended_interest.timestamp, utc=True) <= pd.Timestamp("2023-08-12T00:00:00Z")]
    pd.testing.assert_frame_equal(extended_interest.reset_index(drop=True), earlier_interest.reset_index(drop=True))
    assert prefix["accounting_check"]["ok"]
    assert augmented["accounting_check"]["ok"]


def test_monday_window_executes_first_monday_from_completed_sunday_signal(tmp_path):
    frames, metadata = synthetic_market(length=206)
    run = spec(suffix="monday_window_boundary")
    run.update(start="2023-07-24", end="2023-07-25")
    summary = runner.run_one(run, frames, metadata, deepcopy(config._config), tmp_path)
    trades = pd.DataFrame(summary["trades"])
    assert not trades.empty
    buy_times = pd.to_datetime(trades.loc[trades.side.eq("buy"), "fill_time"], utc=True)
    assert buy_times.min() == pd.Timestamp("2023-07-24T00:00:00Z")
    assert (pd.to_datetime(trades.fill_time, utc=True) >= pd.Timestamp("2023-07-24T00:00:00Z")).all()
    curve = pd.read_csv(tmp_path / "runs" / run["run_id"] / "equity.csv")
    assert pd.to_datetime(curve.timestamp, utc=True).min() == pd.Timestamp("2023-07-24T00:00:00Z")
    assert summary["accounting_check"]["ok"]
