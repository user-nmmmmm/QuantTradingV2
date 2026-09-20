from copy import deepcopy

import pandas as pd

from backtest.engine import BacktestEngine
from config.config import config
from core.state import MarketState
from core.runtime import MarketDataSlice
from router.router import Router
from strategies.base import Strategy


class Hold(Strategy):
    def __init__(self):
        super().__init__("Hold", set(MarketState))

    def should_enter(self, symbol, i, df, state, portfolio):
        if i == 30:
            return {"action": "buy", "order_type": "market", "stop_loss": 90.}

    def should_exit(self, *args):
        return None


def run_case(monkeypatch, missing_tail=False):
    def factory(strategies, configuration, log_path=None):
        return Router(strategies, regime_map={s.name: "Hold" for s in MarketState}, log_path=log_path)
    monkeypatch.setattr("backtest.engine.build_router", factory)
    settings = deepcopy(config._config)
    settings["strategy_health"]["enabled"] = False
    monkeypatch.setattr(config, "_config", settings)
    index = pd.date_range("2024-01-01", periods=60, freq="D")
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 1e6}, index=index)
    frames = {"AAA-USDT": frame.iloc[:45] if missing_tail else frame}
    if missing_tail:
        frames["BBB-USDT"] = frame
    engine = BacktestEngine(warmup_period=25, terminal_policy="valuation_only")
    result = engine.run(frames, strategies={"Hold": Hold()}, routing_log_enabled=False)
    return engine, result


def test_valuation_only_does_not_invent_exit_or_close_event(monkeypatch):
    engine, result = run_case(monkeypatch)
    assert result["trades"]
    assert all(t["exit_reason"] != "EndOfBacktest" for t in result["trades"])
    assert not engine.execution_adapter.broker.close_events
    assert result["terminal_valuation"]["positions"]
    assert result["terminal_valuation"]["synthetic_fill_count"] == 0
    assert result["terminal_valuation"]["status"] == "ok"
    assert result["accounting_check"]["ok"]


def test_stale_tail_is_not_tradable_and_zero_recovery_is_reported(monkeypatch):
    engine, result = run_case(monkeypatch, True)
    valuation = result["terminal_valuation"]
    assert valuation["status"] == "insufficient"
    assert valuation["stale_long_zero_recovery_loss"] > 0
    assert result["valuation_quality"]
    assert not engine.execution_adapter.broker.close_events
    assert all(t["fill_time"] <= pd.Timestamp("2024-02-14")
        for t in result["trades"] if t["symbol"] == "AAA-USDT")


def test_future_delist_fact_does_not_censor_earlier_execution():
    class Controller:
        metadata = {"AAA-USDT": {"events": [{"kind": "spot_delisted", "source_status": "verified",
            "available_at": "2024-01-03", "effective_at": "2024-01-05T08:00:00Z"}]}}
    engine = BacktestEngine(portfolio_controller=Controller(), terminal_policy="valuation_only")
    def event(day):
        return MarketDataSlice(pd.Timestamp(day), {"AAA-USDT": pd.Series({"close": 100})}, {})
    assert "AAA-USDT" in engine._causal_executable_event(event("2024-01-02")).bars
    assert "AAA-USDT" in engine._causal_executable_event(event("2024-01-04")).bars
    assert "AAA-USDT" not in engine._causal_executable_event(event("2024-01-05")).bars
