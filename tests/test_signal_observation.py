"""P0 causal measurement, execution isolation and counterfactual contracts."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import random
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from backtest.reporting.signal_observation import (
    signal_observation_digest, write_signal_observation_report,
)
from backtest.signal_ghost import replay_ghosts
from composition.factory import build_strategy_registry, build_state_machine, build_risk_manager
from config.config import config
from core.broker import Broker
from core.portfolio import Portfolio
from core.runtime import MarketDataSlice
from core.market_data import HistoricalMarketDataAdapter
from core.reproducibility import deterministic_result_digest
from core.signal_actuals import reconcile_actuals
from core.signal_observation import SignalObserver
from core.signal_observation_types import (
    ContextSnapshot, ObservationPolicy, SignalCandidateEvent, canonical, close_time, iso,
)
from core.signal_outcomes import ForwardOutcomeTracker, ObservationCosts
from core.state import MarketState
from router.router import Router
from strategies.base import Strategy
from tests.engine_baseline_harness import build_synthetic_data_map


def prices(n=8, start="2020-01-01", volume=100000):
    close = [100+i*2 for i in range(n)]
    return pd.DataFrame({"open": close, "high": [p+1 for p in close],
                         "low": [p-1 for p in close], "close": close,
                         "volume": volume}, index=pd.date_range(start, periods=n))


def candidate(at="2020-01-01", *, symbol="X", direction="long", cid="c", price=100):
    return SignalCandidateEvent(cid, iso(at), symbol, "Test", direction, 1.0, price, "v1",
        canonical({"action": "buy" if direction == "long" else "short", "price": price,
                   "stop_loss": price*.95 if direction == "long" else price*1.05}),
        ContextSnapshot(iso(at), close_time(at, "1d"), "1d", "TREND_UP", "active", 1.0,
                        "normal", 1.0, 0.0, False, "{}", "v1"), 0.0)


def event(frame, i, symbol="X"):
    return MarketDataSlice(frame.index[i], {symbol: frame.iloc[i]}, {symbol: frame},
                           timeframe="1d", positions={symbol: i})


class DailyRaw(Strategy):
    def __init__(self):
        super().__init__("Test", set(MarketState))

    def raw_entry_signal(self, symbol, i, df):
        assert len(df) == i+1  # provider cannot access the future rows
        return {"action": "buy", "price": float(df.close.iat[i]), "stop_loss": float(df.close.iat[i])*.95}

    def should_enter(self, symbol, i, df, state, portfolio):
        return {"action": "buy", "stop_loss": float(df.close.iat[i])*.95}

    def should_exit(self, symbol, i, df, state, portfolio):
        return None


def observer_for(strategies=None, **policy):
    strategies = strategies or {"Test": DailyRaw()}
    return SignalObserver(policy=ObservationPolicy(enabled=True, **policy),
        costs=ObservationCosts(), strategies=strategies, state_machine=build_state_machine(config))


@pytest.mark.parametrize("direction, sign", [("long", 1), ("short", -1)])
def test_fixed_horizon_starts_next_open_and_only_matures_at_close(direction, sign):
    frame = prices(3)
    frame.loc[frame.index[1], ["open", "high", "low", "close"]] = [110, 125, 100, 120]
    tracker = ForwardOutcomeTracker(ObservationPolicy(enabled=True, horizons=(1, 3)), ObservationCosts())
    c = candidate(direction=direction)
    tracker.add(c); tracker.add(c)
    tracker.advance(event(frame, 0))
    assert not tracker.results
    tracker.advance(event(frame, 1)); tracker.advance(event(frame, 1))
    assert len(tracker.results) == 1
    row = tracker.results[0]
    assert row["entry_reference"] == 110
    assert row["net_pnl"] == sign * 100
    assert row["net_return_bps"] == pytest.approx(sign*10000/11)
    assert row["available_at"] == iso("2020-01-03")
    tracker.finish(iso("2020-01-03"))
    assert tracker.results[-1]["status"] == "censored_end_of_data"
    assert tracker.results[-1]["net_pnl"] is None


def test_missing_bar_and_missing_funding_are_not_zero_returns():
    frame = prices(5).drop(pd.Timestamp("2020-01-02"))
    tracker = ForwardOutcomeTracker(ObservationPolicy(horizons=(1,)), ObservationCosts())
    tracker.add(candidate()); tracker.advance(event(frame, 1))
    assert tracker.results[0]["status"] == "censored_missing_bar"
    frame = prices(2)
    tracker = ForwardOutcomeTracker(ObservationPolicy(horizons=(1,)), ObservationCosts(account_mode="perpetual"))
    tracker.add(candidate()); tracker.advance(event(frame, 1))
    assert tracker.results[0]["status"] == "censored_missing_funding"
    assert tracker.results[0]["net_return_bps"] is None


def test_slippage_fees_impact_and_borrow_are_charged_once():
    frame = prices(4)
    costs = ObservationCosts(commission_rate=.001, slippage=.0005, spread_bps=2,
        volatility_slippage_factor=.02, use_impact_cost=True,
        account_mode="spot_margin", default_borrow_rate_annual=.10)
    tracker = ForwardOutcomeTracker(ObservationPolicy(horizons=(2,)), costs)
    tracker.add(candidate(direction="short"))
    tracker.advance(event(frame, 1)); tracker.advance(event(frame, 2))
    row = tracker.results[0]
    assert row["net_pnl"] == pytest.approx(row["gross_pnl"] - row["commission"] - row["slippage"] - row["impact"] - row["carry"])
    assert row["carry"] == pytest.approx(10*104*.1/365)
    assert row["slippage"] > 0 and row["impact"] > 0


def test_snapshot_is_immutable_detached_and_time_stamped():
    c = candidate()
    with pytest.raises(FrozenInstanceError):
        c.reference_price = 1
    data = c.to_dict(); data["signal"]["price"] = 1
    assert c.to_dict()["signal"]["price"] == 100
    assert c.context.available_at > c.timestamp


@pytest.mark.parametrize("settings", [{"horizons": (0,)}, {"horizons": (1, 1)},
    {"reference_notional": float("nan")}, {"enabled": "false"}, {"ghost_horizon": 0}])
def test_invalid_observation_configuration_rejected(settings):
    with pytest.raises(ValueError):
        ObservationPolicy(**settings)


def test_every_registered_provider_is_passive_and_ignores_health_and_state():
    frame = build_synthetic_data_map(symbols=("X",))["X"]
    strategies = build_strategy_registry(config)
    original = frame.copy(deep=True)
    for strategy in strategies.values():
        if hasattr(strategy, "health"):
            strategy.health.manual_lock("test", at=frame.index[0])
        before = deepcopy(strategy.context)
        health = strategy.health.to_dict() if hasattr(strategy, "health") else None
        counts = getattr(strategy, "raw_setup_count", None)
        trade_state = deepcopy(getattr(strategy, "trade_state", None))
        for i in range(30, len(frame)):
            strategy.raw_entry_signal("X", i, frame.iloc[:i+1].copy())
        assert strategy.context == before
        assert getattr(strategy, "raw_setup_count", None) == counts
        assert getattr(strategy, "trade_state", None) == trade_state
        if health is not None:
            assert strategy.health.to_dict() == health
    pd.testing.assert_frame_equal(frame, original)


def test_raw_observation_survives_routing_rejection_without_evaluating_later_gates():
    obs = observer_for()
    frame, risk = prices(65), build_risk_manager(config)
    router = Router(obs.strategies, {s.name: "Cash" for s in MarketState})
    row = {"reason": "market_state_cash"}
    obs.observe(event(frame, 63), "X", portfolio=Portfolio(10000), risk_manager=risk, router=router, audit=row)
    obs.observe(event(frame, 63), "X", portfolio=Portfolio(10000), risk_manager=risk, router=router, audit=row)
    obs.settle_decisions()
    assert len(obs.candidates) == len(obs.decisions) == 1
    assert obs.decisions[0]["veto_stage"] == "regime"
    assert obs.decisions[0]["unvisited_gates"] == "unknown"
    assert not obs.errors


def test_future_suffix_cannot_change_candidate_contexts_or_matured_labels():
    frame = prices(85)
    risk = build_risk_manager(config)
    def collect(data):
        obs = observer_for(horizons=(1, 3))
        router = Router(obs.strategies, {s.name: "Test" for s in MarketState})
        for i in range(60, len(data)):
            ev = event(data, i); obs.advance(ev)
            obs.observe(ev, "X", portfolio=Portfolio(10000), risk_manager=risk, router=router,
                        audit={"reason": "position_held"})
            obs.settle_decisions()
        return obs
    full, prefix = collect(frame), collect(frame.iloc[:72].copy())
    assert not full.errors and not prefix.errors
    assert [c.to_dict() for c in full.candidates[:12]] == [c.to_dict() for c in prefix.candidates]
    known = [r for r in full.outcomes.results if pd.Timestamp(r["available_at"]) <= pd.Timestamp("2020-03-13", tz="UTC")]
    assert known == prefix.outcomes.results
    changed = frame.copy(); changed.loc[changed.index[72]:, ["open", "high", "low", "close"]] *= 10
    other = collect(changed)
    assert [c.to_dict() for c in other.candidates[:12]] == [c.to_dict() for c in prefix.candidates]


def ghost_payload(cs, policy=None):
    policy = policy or ObservationPolicy(enabled=True, horizons=(1,), ghost_horizon=1)
    from dataclasses import asdict
    return {"policy": asdict(policy), "candidates": [c.to_dict() for c in cs],
            "decisions": [{"candidate_id": c.candidate_id, "accepted": False, "veto_stage": "regime"} for c in cs]}


def test_ghost_uses_next_bar_on_both_legs_and_busy_blocking():
    frame = prices(5)
    template = Broker(Portfolio(10000), commission_rate=.001, slippage=.0005)
    cs = [candidate(frame.index[i], cid=str(i), price=float(frame.close.iat[i])) for i in range(3)]
    ghost = replay_ghosts(HistoricalMarketDataAdapter({"X": frame}, timeframe="1d"), ghost_payload(cs), template)
    rows = [r for r in ghost["rows"] if r["mode"] == "isolated_track"]
    assert [r["status"] for r in rows] == ["closed", "busy_blocked", "closed"]
    assert rows[0]["first_fill_time"] == iso(frame.index[1])
    assert rows[0]["last_exit_time"] == iso(frame.index[2])
    assert rows[0]["net_pnl"] > 0
    assert not template.trades and template.portfolio.cash == 10000


def test_ghost_partial_fills_cancel_remainder_then_liquidate_over_real_bars():
    frame = prices(8, volume=1)
    template = Broker(Portfolio(10000), commission_rate=0, max_participation_rate=1)
    ghost = replay_ghosts(HistoricalMarketDataAdapter({"X": frame}, timeframe="1d"),
                         ghost_payload([candidate()]), template)
    row = ghost["rows"][0]
    assert row["status"] == "closed"
    assert row["filled_quantity"] == 1
    assert row["last_exit_time"] == iso(frame.index[2])
    assert any(a.get("reason") == "participation_limit" for a in ghost["execution_audit"])


def test_capital_constrained_ghost_rejects_competing_orders():
    data = {s: prices(4) for s in ("A", "B")}
    cs = [candidate(symbol=s, cid=s) for s in data]
    payload = ghost_payload(cs, ObservationPolicy(enabled=True, ghost_capital=1000, reference_notional=600, ghost_horizon=1))
    ghost = replay_ghosts(HistoricalMarketDataAdapter(data, timeframe="1d"), payload,
                         Broker(Portfolio(10000), commission_rate=0))
    shared = [r for r in ghost["rows"] if r["mode"] == "capital_constrained"]
    assert [r["status"] for r in shared] == ["closed", "capital_blocked"]
    assert not ghost["errors"]


def test_actual_partial_closes_link_order_and_exclude_valuation_transfer():
    payload = {"candidates": [candidate().to_dict()], "decisions": [{"candidate_id": "c", "order_id": "o"}]}
    base = {"symbol": "X", "qty": 2., "fill_time": "2020-01-02", "commission": 2., "fill_price": 100.}
    trades = [{**base, "order_id": "o", "side": "buy"},
              {**base, "qty": 1., "order_id": "exit", "side": "sell", "fill_price": 110., "commission": 1.,
               "fill_time": "2020-01-03", "exit_reason": "DrawdownBudgetReduce"},
              {**base, "qty": 1., "order_id": "tail", "side": "sell", "exit_reason": "EndOfBacktest"}]
    result = reconcile_actuals(payload, trades, {"o": SimpleNamespace(stop_loss=95)}, [])
    summary = result["summaries"][0]
    assert summary["actual_status"] == "open_or_partially_closed"
    assert summary["realized_net_pnl_ex_carry"] == 8
    assert summary["realized_R"] == 1.6
    assert result["closes"][0]["exit_controller"] == "account_risk"
    assert result["closes"][0]["available_at"] == iso("2020-01-04")
    assert len(result["valuation_transfers"]) == 1


@pytest.mark.parametrize("random_slip", [False, True])
def test_observation_never_changes_official_fills_equity_health_or_risk(random_slip, tmp_path):
    data = build_synthetic_data_map()
    def run(enabled):
        random.seed(456)
        return BacktestEngine(random_slip=random_slip,
            signal_observation={"enabled": enabled, "horizons": [1, 3], "ghost_horizon": 3}).run(
                data, routing_log_enabled=False)
    off, on = run(False), run(True)
    assert off["trades"]
    assert deterministic_result_digest(off) == deterministic_result_digest(on)
    assert off["strategy_health"] == on["strategy_health"]
    assert off["allocation_audit"] == on["allocation_audit"]
    payload = on["signal_observation"]
    assert payload["candidates"] and not payload["errors"]
    assert not payload["actual"]["unmatched"] and not payload["ghost"]["errors"]
    assert payload["status"] == "complete"
    summary = write_signal_observation_report(payload, tmp_path)
    assert summary["decision_partition_ok"] and summary["candidate_ids_unique"]
    assert len(list(tmp_path.glob("signal_*.csv"))) >= 12
    assert json.loads((tmp_path/"signal_observation_summary.json").read_text(encoding="utf-8"))["status"] == "complete"
    assert "门控" in (tmp_path/"gate_effectiveness.md").read_text(encoding="utf-8")


def test_empty_report_has_headers_and_does_not_invent_results(tmp_path):
    obs = observer_for()
    obs.finish()
    summary = write_signal_observation_report(obs.export(), tmp_path)
    assert summary["candidate_count"] == 0
    assert pd.read_csv(tmp_path/"signal_candidates.csv").empty
    assert pd.read_csv(tmp_path/"gate_effectiveness.csv").empty


def test_ghost_execution_error_keeps_fills_and_never_restarts_the_account():
    frame = prices(6)
    cs = [candidate(frame.index[i], cid=str(i)) for i in (0, 2, 3)]
    template = Broker(Portfolio(10000, account_mode="perpetual"),
                      commission_rate=0, funding_rate_required=True)
    ghost = replay_ghosts(HistoricalMarketDataAdapter({"X": frame}, timeframe="1d"),
                         ghost_payload(cs), template)
    rows = [r for r in ghost["rows"] if r["mode"] == "isolated_track"]
    assert [r["status"] for r in rows] == [
        "unresolved_execution_error", "unresolved_track_error", "unresolved_track_error"]
    assert all(r["net_pnl"] is None for r in rows)
    assert len(ghost["errors"]) == len(ghost["accounts"]) == 2
    assert len(ghost["fills"]) == 2  # entry facts survive carry failure
    assert all(a["status"] == "unresolved_execution_error" for a in ghost["accounts"])


def test_nonmarket_signal_is_not_mislabelled_as_market_fill():
    c = replace(candidate(), signal_json=canonical({"action": "buy", "price": 95, "order_type": "limit"}))
    tracker = ForwardOutcomeTracker(ObservationPolicy(horizons=(1, 3)), ObservationCosts())
    tracker.add(c)
    assert not tracker.pending
    assert {r["status"] for r in tracker.results} == {"censored_unsupported_order_type"}
    assert all(r["net_pnl"] is None for r in tracker.results)


def test_late_input_is_rejected_without_calling_the_raw_provider():
    obs = observer_for()
    frame = prices(65)
    frame["available_at"] = frame.index + pd.Timedelta(days=2)
    obs.observe(event(frame, 63), "X", portfolio=Portfolio(10000),
        risk_manager=build_risk_manager(config),
        router=Router(obs.strategies, {s.name: "Cash" for s in MarketState}), audit={})
    assert not obs.candidates
    assert obs.export()["status"] == "incomplete"
    assert "not yet available" in obs.errors[0]["message"]


def test_unimplemented_raw_provider_is_visible_not_silently_probed():
    class Unsupported(DailyRaw):
        raw_entry_signal = Strategy.raw_entry_signal

        def should_enter(self, *args):
            raise AssertionError("observation must never invoke the trading path")
    obs = observer_for({"Test": Unsupported()})
    frame = prices(65)
    obs.observe(event(frame, 63), "X", portfolio=Portfolio(10000),
        risk_manager=build_risk_manager(config),
        router=Router(obs.strategies, {s.name: "Cash" for s in MarketState}), audit={})
    assert obs.errors[0]["reason"] == "NotImplementedError"
    assert obs.export()["status"] == "incomplete"


def test_registered_signals_are_prefix_invariant():
    frame = build_synthetic_data_map(symbols=("X",))["X"]
    def collect(source):
        obs = observer_for(build_strategy_registry(config))
        router = Router(obs.strategies, {s.name: "Cash" for s in MarketState})
        risk, portfolio = build_risk_manager(config), Portfolio(10000)
        for i in range(30, 130):
            obs.observe(event(source, i), "X", portfolio=portfolio,
                risk_manager=risk, router=router, audit={"reason": "position_held"})
        obs.settle_decisions()
        assert not obs.errors
        return [c.to_dict() for c in obs.candidates]
    prefix = collect(frame.iloc[:130].copy())
    changed = frame.copy()
    changed.loc[changed.index[130]:, ["open", "high", "low", "close"]] *= 10
    assert prefix and prefix == collect(frame) == collect(changed)


def test_observation_replay_digest_is_deterministic():
    data = build_synthetic_data_map(symbols=("X",), bars=100)
    def run():
        random.seed(456)
        return BacktestEngine(signal_observation={"enabled": True, "horizons": [1], "ghost_horizon": 1}).run(
            data, routing_log_enabled=False)["signal_observation"]
    first, second = run(), run()
    assert first["status"] == second["status"] == "complete"
    assert signal_observation_digest(first) == signal_observation_digest(second)


def test_ghost_does_not_charge_borrow_for_a_flat_gap_between_trades():
    frame = prices(10)
    frame[["open", "close"]] = 100
    frame["high"], frame["low"] = 101, 99
    template = Broker(Portfolio(10000, account_mode="spot_margin"),
                      commission_rate=0, default_borrow_rate_annual=.10)
    cs = [candidate(frame.index[i], cid=str(i), direction="short") for i in (0, 5)]
    payload = ghost_payload(cs, ObservationPolicy(enabled=True, ghost_horizon=2))
    ghost = replay_ghosts(HistoricalMarketDataAdapter({"X": frame}, timeframe="1d"), payload, template)
    rows = [r for r in ghost["rows"] if r["mode"] == "isolated_track"]
    assert not ghost["errors"]
    assert [r["status"] for r in rows] == ["closed", "closed"]
    # horizon=2 spans two real overnight intervals, including the final
    # interval settled at the exit fill. The flat gap adds no interest.
    assert rows[0]["carry"] == rows[1]["carry"] == pytest.approx(2*1000*.1/365)


def test_ghost_margin_breach_is_unknown_not_an_insolvent_profit_label():
    frame = prices(6)
    frame.loc[frame.index[2]:, ["open", "high", "low", "close"]] *= 4
    template = Broker(Portfolio(1000, account_mode="spot_margin"), commission_rate=0)
    policy = ObservationPolicy(enabled=True, ghost_capital=1000, reference_notional=950, ghost_horizon=3)
    ghost = replay_ghosts(HistoricalMarketDataAdapter({"X": frame}, timeframe="1d"),
                         ghost_payload([candidate(direction="short")], policy), template)
    assert len(ghost["errors"]) == 2
    assert all("margin breach" in e["reason"] for e in ghost["errors"])
    assert all(r["status"] == "unresolved_execution_error" and r["net_pnl"] is None for r in ghost["rows"])


def test_manifest_replay_preserves_input_symbol_order_and_research_facts(tmp_path, capsys):
    from main import replay_manifest
    from core.reproducibility import save_data_snapshots, sha256_file, write_manifest
    from pathlib import Path
    data = build_synthetic_data_map(symbols=("Z", "A"), bars=100)
    engine = BacktestEngine(initial_capital=10000,
        signal_observation={"enabled": True, "horizons": [1], "ghost_horizon": 1})
    result = engine.run(data, routing_log_enabled=False)
    execution = {"capital": 10000, "seed": 456, "random_slip": False,
        "slippage": engine.slippage, "warmup_period": engine.warmup_period,
        "alignment_mode": engine.alignment_mode, "benchmark_mode": engine.benchmark_mode,
        "benchmark_rebalance_cost_bps": engine.benchmark_rebalance_cost_bps,
        "timeframe": engine.timeframe, "account_mode": result["account_mode"],
        "result_digest": deterministic_result_digest(result), "data_symbol_order": list(data),
        "signal_observation": result["signal_observation"]["policy"],
        "signal_observation_digest": signal_observation_digest(result["signal_observation"])}
    manifest = {"schema_version": "2.0", "code": {}, "run_id": result["run_id"],
        "config": {"sha256": sha256_file(Path(__file__).resolve().parents[1]/"config/params.yaml")},
        "data_snapshots": save_data_snapshots(data, tmp_path/"data_inputs"), "execution": execution}
    path = tmp_path/"run_manifest.json"
    write_manifest(path, manifest)
    assert replay_manifest(str(path)) == 0
    execution["data_symbol_order"] = ["Z", "Z"]
    write_manifest(path, manifest)
    assert replay_manifest(str(path)) == 7
