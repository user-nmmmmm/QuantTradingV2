"""Engine equivalence and hand-computed rewards, not investment evidence."""

from copy import deepcopy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from config.config import config
from core.state import MarketState
from research.ml_selection.environment import (
    FullEngineEnvironment, discounted_returns, episode_summary, equity_rewards,
)
from strategies.base import Strategy


class OneUnitHold(Strategy):
    def __init__(self, entry_index=0):
        super().__init__("OneUnitHold", set(MarketState))
        self.entry_index = entry_index

    def should_enter(self, symbol, i, df, state, portfolio):
        if i == self.entry_index:
            return {"action": "buy", "order_type": "market", "stop_loss": 90.0}

    def should_exit(self, *args):
        return None

    def initial_entry_quantity(self, **kwargs):
        return 1.0


class PassThrough:
    def __init__(self):
        self.calls = []

    def select(self, candidates, **kwargs):
        self.calls.append((kwargs["event"].timestamp, len(candidates)))
        return list(candidates)


def parameters():
    settings = deepcopy(config._config)
    settings["account"]["mode"] = "spot"
    settings["execution"]["fee_schedule"]["market_type"] = "spot"
    settings["execution"].update({
        "commission_rate_taker": .001, "commission_rate_maker": .001,
        "slippage_bps": 0., "spread_bps": 0., "volatility_slippage_factor": 0.,
        "use_impact_cost": False,
    })
    settings["risk"]["max_leverage"] = 1.
    settings["drawdown_budget"]["enabled"] = False
    settings["strategy_health"]["enabled"] = False
    settings["portfolio_risk"]["enabled"] = False
    settings["portfolio_targets"] = {"enabled": False}
    settings["router"]["cooldown_bars"] = 0
    settings["routing"] = {state.name: "OneUnitHold" for state in MarketState}
    return settings


def market():
    index = pd.date_range("2024-01-01", periods=5, freq="D")
    close = np.array([100., 101., 103., 102., 104.])
    frame = pd.DataFrame({
        "open": [100., 100., 101., 103., 102.],
        "high": close + 2., "low": 99., "close": close, "volume": 1_000_000.,
    }, index=index)
    return {"TEST-USDT": frame}


def environment(**options):
    return FullEngineEnvironment(
        market(), parameters=parameters(),
        engine_options={"initial_capital": 1000., "slippage": 0., "warmup_period": 0, **options},
        strategies={"OneUnitHold": OneUnitHold()},
    )


def trade_digest(rows):
    stable = [{key: str(row[key]) for key in (
        "symbol", "side", "qty", "fill_price", "fill_time", "commission", "exit_reason",
    )} for row in rows]
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def test_passthrough_replays_original_engine_exactly_and_resets_state():
    settings = parameters()
    before = config._config
    try:
        config._config = deepcopy(settings)
        engine = BacktestEngine(initial_capital=1000., slippage=0., warmup_period=0,
                                calculate_benchmarks=False)
        baseline = engine.run(market(), strategies={"OneUnitHold": OneUnitHold()},
                              routing_log_enabled=False)
    finally:
        config._config = before
    env = environment()
    selector = PassThrough()
    first = env.run_episode(selector)
    second = env.run_episode(PassThrough())
    assert selector.calls and any(count == 1 for _, count in selector.calls)
    pd.testing.assert_frame_equal(first.result["equity_curve"], baseline["equity_curve"])
    pd.testing.assert_frame_equal(second.result["equity_curve"], baseline["equity_curve"])
    assert trade_digest(first.result["trades"]) == trade_digest(baseline["trades"])
    assert trade_digest(second.result["trades"]) == trade_digest(baseline["trades"])
    assert first.engine is not second.engine
    assert first.engine.event_processor.portfolio is not second.engine.event_processor.portfolio
    np.testing.assert_allclose(first.result["equity_curve"].equity,
                               [1000., 1000.9, 1002.9, 1001.9, 1003.9, 1003.9])
    assert first.summary["actual_fill_count"] == 1  # zero-cost valuation close excluded
    assert first.summary["net_return"] == pytest.approx(.0039)
    assert first.summary["accounting_ok"]
    assert config._config is before


def test_empty_selection_keeps_cash_in_real_engine():
    class Cash:
        def select(self, candidates, **kwargs):
            return []
    episode = environment().run_episode(Cash())
    assert episode.result["trades"] == []
    np.testing.assert_array_equal(episode.result["equity_curve"].equity, 1000.)
    assert episode.summary["reward_sum"] == 0.


def test_warmup_history_and_trading_start_are_preserved():
    env = environment(trading_start="2024-01-03", warmup_period=2)
    env.strategies = {"OneUnitHold": OneUnitHold(entry_index=2)}
    episode = env.run_episode(PassThrough())
    assert episode.result["equity_curve"].index[0] == pd.Timestamp("2024-01-03")
    assert episode.result["trades"][0]["signal_time"] == pd.Timestamp("2024-01-03")
    assert episode.result["trades"][0]["fill_time"] == pd.Timestamp("2024-01-04")


def test_net_equity_unrealized_and_fee_reward_hand_calculation():
    index = pd.date_range("2024-01-01", periods=3)
    # Equity already contains the entry commission and live mark-to-market P&L.
    curve = pd.DataFrame({"equity": [999., 1099., 1049.]}, index=index)
    trades = [{"fill_time": index[0], "qty": 1., "fill_price": 100., "commission": 1.,
               "exit_reason": "signal"}]
    ledger = equity_rewards(curve, trades, initial_capital=1000.,
                            drawdown_penalty=.5, turnover_penalty=.1)
    returns = np.log(np.array([999. / 1000., 1099. / 999., 1049. / 1099.]))
    drawdown = np.array([.001, 0., 50. / 1099.])
    np.testing.assert_allclose(ledger.net_log_return, returns)
    np.testing.assert_allclose(ledger.drawdown_penalty, .5 * drawdown)
    np.testing.assert_allclose(ledger.turnover, [100. / 999., 0., 0.])
    np.testing.assert_allclose(ledger.reward, returns - .5 * drawdown - .1 * ledger.turnover)
    assert ledger.net_log_return.sum() == pytest.approx(np.log(1049. / 1000.))
    pd.testing.assert_index_equal(ledger.index, index)


def test_actual_partial_fills_only_and_synthetic_valuation_excluded():
    index = pd.date_range("2024-01-01", periods=2)
    terminal = index[-1] + pd.Timedelta(microseconds=1)
    curve = pd.DataFrame({"equity": [1000., 1000., 1000.]}, index=index.append(pd.DatetimeIndex([terminal])))
    trades = [
        {"fill_time": index[0], "qty": .3, "fill_price": 100., "exit_reason": "signal", "planned_qty": 50.},
        {"fill_time": index[1], "qty": .7, "fill_price": 100., "exit_reason": "signal"},
        {"fill_time": terminal, "qty": 1., "fill_price": 100., "exit_reason": "EndOfBacktest"},
    ]
    ledger = equity_rewards(curve, trades, initial_capital=1000., drawdown_penalty=0.)
    np.testing.assert_allclose(ledger.turnover, [.03, .07, 0.])
    assert ledger.reward.sum() == 0.


def test_forced_terminal_liquidation_retains_cost_and_turnover():
    index = pd.DatetimeIndex(["2024-01-01", "2024-01-01 00:00:00.000001"])
    curve = pd.DataFrame({"equity": [1000., 999.]}, index=index)
    trades = [{"fill_time": index[1], "qty": 1., "fill_price": 100., "commission": 1.,
               "exit_reason": "EndOfBacktest"}]
    ledger = equity_rewards(curve, trades, initial_capital=1000., drawdown_penalty=0.,
                            terminal_policy="forced_liquidation")
    assert ledger.reward.iloc[-1] == pytest.approx(np.log(.999))
    assert ledger.turnover.iloc[-1] == pytest.approx(100. / 999.)


def test_original_engine_forced_liquidation_charges_terminal_costs():
    episode = environment(terminal_policy="forced_liquidation").run_episode(PassThrough())
    assert episode.summary["actual_fill_count"] == 2
    assert episode.rewards.index[-1] == pd.Timestamp("2024-01-05") + pd.Timedelta(microseconds=1)
    assert episode.rewards.reward.iloc[-1] < 0
    assert episode.rewards.turnover.iloc[-1] > 0
    assert episode.summary["final_equity"] == pytest.approx(1003.796)


def test_original_engine_valuation_only_retains_unresolved_position():
    episode = environment(terminal_policy="valuation_only").run_episode(PassThrough())
    assert episode.summary["actual_fill_count"] == 1
    assert "TEST-USDT" in episode.summary["unresolved_positions"]
    assert episode.summary["terminal_valuation_status"] == "ok"
    assert episode.summary["final_equity"] == pytest.approx(1003.9)
    assert len(episode.rewards) == 5


def test_risk_termination_summary_excludes_frozen_tail_and_reports_unresolved_book():
    index = pd.date_range("2024-01-01", periods=4)
    result = {
        "equity_curve": pd.DataFrame({"equity": [1000., 900., 900., 900.],
                                      "gross_exposure_pct_equity": [.1, .1, .1, .1]}, index=index),
        "trades": [], "terminal_policy": "valuation_only",
        "lifecycle": {"status": "terminated_by_risk", "termination_timestamp": index[1],
                      "active_end": index[1], "termination_reason": "portfolio_liquidate",
                      "unresolved_risk_positions": {"A": {"qty": 1.}}},
        "terminal_valuation": {"status": "insufficient", "pending_orders": [{"remaining_quantity": 1.}]},
    }
    summary = episode_summary(result, 1000.)
    assert summary["evaluated_equity_rows"] == 2
    assert summary["excluded_frozen_tail_rows"] == 2
    assert summary["terminated_by_risk"]
    assert summary["unresolved_positions"] == {"A": {"qty": 1.}}
    assert summary["pending_orders"]
    assert summary["max_drawdown"] == pytest.approx(.1)
    assert summary["net_return"] == pytest.approx(-.1)


def test_configuration_restored_even_if_engine_mutates_then_fails(monkeypatch):
    env = environment()
    previous = config._config
    snapshot = deepcopy(previous)
    def fail(*args, **kwargs):
        config._config["risk"]["risk_per_trade"] = .9
        raise RuntimeError("injected execution failure")
    monkeypatch.setattr(BacktestEngine, "run", fail)
    with pytest.raises(RuntimeError, match="injected"):
        env.run_episode()
    assert config._config is previous
    assert config._config == snapshot
    assert env.parameters["risk"]["risk_per_trade"] != .9


@pytest.mark.parametrize("options", [{"timeframe": "1h"}, {"account_mode": "perpetual"},
                                     {"candidate_selector": PassThrough()}, {"portfolio_controller": object()}])
def test_scope_rejects_unsupported_engine_modes(options):
    with pytest.raises(ValueError):
        environment(**options)


@pytest.mark.parametrize("equity", [[0.], [-1.], [np.nan], [np.inf]])
def test_log_reward_rejects_nonpositive_or_nonfinite_equity(equity):
    with pytest.raises(ValueError, match="equity"):
        equity_rewards(pd.DataFrame({"equity": equity}, index=pd.date_range("2024-01-01", periods=1)),
                       [], initial_capital=1000.)


def test_discounted_return_to_go_hand_calculation():
    np.testing.assert_allclose(discounted_returns([1., 2., 3.], gamma=.5), [2.75, 3.5, 3.])
    np.testing.assert_array_equal(discounted_returns([], gamma=1.), [])
    with pytest.raises(ValueError):
        discounted_returns([1.], gamma=1.1)
