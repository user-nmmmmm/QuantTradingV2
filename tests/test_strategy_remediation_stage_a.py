"""Stage A of docs/strategy_remediation_roadmap_20261110.md.

Safety and consistency fixes that must not change candidate backtest
behaviour: A1 time-exit latch and cumulative reduction proof, A3 live history
preflight, A4 health persistence frequency, A5 single source for ADX inputs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from core.broker import Broker
from core.trading.portfolio import Portfolio
from core.trading.state import MarketState
from live_trading.execution_adapter import AccountEntryBlocked, RecordedExecutionAdapter
from router.router import Router

SYMBOL = "A-USDT"
START = pd.Timestamp("2024-01-01", tz="UTC")


def _frame(periods: int) -> pd.DataFrame:
    frame = pd.DataFrame(
        {field: [100.0] * periods for field in ("open", "high", "low", "close")},
        index=pd.date_range(START, periods=periods, freq="1D"),
    )
    frame["volume"] = 1_000_000.0
    return frame


def _held_router_and_broker():
    portfolio = Portfolio(10_000)
    portfolio.update_position(SYMBOL, 2, 100, strategy_id="Probe",
                              order_id="opening", time=START)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    router = Router({}, regime_map={"TREND_UP": "Probe"}, max_holding_days=1)
    return router, broker, portfolio


# ---- A1: MaxHoldingPeriod exit latch -------------------------------------


def test_unfilled_time_exit_is_not_resubmitted_on_the_next_bar():
    router, broker, portfolio = _held_router_and_broker()
    frame = _frame(5)
    for i in (2, 3, 4):  # three expired bars, the first exit never fills
        assert router.process_position_management(
            SYMBOL, i, frame, MarketState.TREND_UP, portfolio, broker)
    exits = [order for order in broker.pending_orders
             if order.exit_reason == "MaxHoldingPeriod"]
    assert len(exits) == 1
    assert router.checkpoint()["time_exits"] == {SYMBOL: exits[0].id}


def test_terminal_time_exit_releases_the_latch_for_remaining_inventory():
    router, broker, portfolio = _held_router_and_broker()
    frame = _frame(5)
    router.process_position_management(SYMBOL, 2, frame, MarketState.TREND_UP, portfolio, broker)
    first = broker.pending_orders[0]
    broker.cancel_symbol_orders(SYMBOL)  # terminal fact, position unchanged
    router.process_position_management(SYMBOL, 3, frame, MarketState.TREND_UP, portfolio, broker)
    exits = [order for order in broker.pending_orders
             if order.exit_reason == "MaxHoldingPeriod"]
    assert [order.id for order in exits] != [first.id]
    assert len(exits) == 1


def test_flat_symbol_clears_latch_and_checkpoint_round_trips():
    router, broker, portfolio = _held_router_and_broker()
    frame = _frame(4)
    router.process_position_management(SYMBOL, 2, frame, MarketState.TREND_UP, portfolio, broker)
    saved = router.checkpoint()
    restored = Router({}, regime_map={"TREND_UP": "Probe"}, max_holding_days=1)
    restored.restore_checkpoint(saved)
    assert restored._time_exit_orders == router._time_exit_orders
    legacy = {key: value for key, value in saved.items() if key != "time_exits"}
    restored.restore_checkpoint(legacy)  # pre-A1 checkpoints stay readable
    assert restored._time_exit_orders == {}
    portfolio.update_position(SYMBOL, -2, 100, strategy_id="Probe", time=frame.index[3])
    router.process_position_management(SYMBOL, 3, frame, MarketState.TREND_UP, portfolio, broker)
    assert router._time_exit_orders == {}


# ---- A1: cumulative in-flight reduction proof ----------------------------


class _LedgerBroker:
    """Durable-ledger broker double: holds ``held`` and lists working orders."""

    def __init__(self, held: float, working: list[dict]):
        self.portfolio = Portfolio(10_000)
        if held:
            self.portfolio.update_position(SYMBOL, held, 100, strategy_id="Probe")
        self.order_store = MagicMock()
        self.order_store.list_non_terminal.return_value = working
        self.order_store.get.return_value = None
        self.submissions = []

    def submit_order(self, symbol, side, qty, *, reduce_only=False):
        self.submissions.append((symbol, side, qty))
        return "submitted"


def _working(side: str, qty: float, order_type: str = "market", order_id: str = "w1"):
    return {"symbol": SYMBOL, "side": side, "remaining_qty": qty,
            "order_type": order_type, "client_order_id": order_id}


def test_second_in_flight_sell_exceeding_position_is_rejected():
    broker = _LedgerBroker(1.5, [_working("sell", 1.0)])
    adapter = RecordedExecutionAdapter(broker, opening_guard=MagicMock(return_value=False))
    with pytest.raises(AccountEntryBlocked):
        adapter.submit_order(SYMBOL, "sell", 1.0)
    assert broker.submissions == []


def test_in_flight_sells_within_position_still_pass_without_the_guard():
    broker = _LedgerBroker(2.0, [_working("sell", 1.0)])
    guard = MagicMock(return_value=False)
    adapter = RecordedExecutionAdapter(broker, opening_guard=guard)
    assert adapter.submit_order(SYMBOL, "sell", 1.0) == "submitted"
    guard.assert_not_called()


def test_resting_protective_stop_does_not_consume_exit_inventory():
    broker = _LedgerBroker(1.0, [_working("sell", 1.0, order_type="stop")])
    guard = MagicMock(return_value=False)
    adapter = RecordedExecutionAdapter(broker, opening_guard=guard)
    assert adapter.submit_order(SYMBOL, "sell", 1.0) == "submitted"
    guard.assert_not_called()


def test_simulated_broker_book_is_counted_too():
    portfolio = Portfolio(10_000)
    portfolio.update_position(SYMBOL, 1.0, 100, strategy_id="Probe", order_id="o", time=START)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    broker.submit_order(SYMBOL, "sell", 1.0, price=100.0, timestamp=START,
                        strategy_id="Probe", exit_reason="signal")
    adapter = RecordedExecutionAdapter(broker, opening_guard=MagicMock(return_value=False))
    with pytest.raises(AccountEntryBlocked):
        adapter.submit_order(SYMBOL, "sell", 1.0, price=100.0, timestamp=START)


# ---- A3: live history preflight ------------------------------------------

from copy import deepcopy  # noqa: E402

from config.config import config  # noqa: E402
from composition.factory import build_state_machine, build_strategy_registry  # noqa: E402
from live_trading.history_preflight import (  # noqa: E402
    InsufficientLiveHistory, check_live_history,
)
from strategies.trend_portfolio_v3 import TrendPortfolioV3Strategy  # noqa: E402


class _Sections:
    def __init__(self, sections):
        self.sections = sections

    def get(self, section, key=None):
        value = self.sections.get(section)
        return value if key is None else (value or {}).get(key)

    def require(self, section, key=None):
        value = self.sections[section]
        return value if key is None else value[key]


def _routed_to(name: str) -> _Sections:
    sections = deepcopy(config._config)
    sections["routing"] = {"TREND_UP": name, "TREND_DOWN": "Cash",
                           "SIDEWAYS": "Cash", "VOLATILE": "Cash"}
    return _Sections(sections)


def test_default_routes_fit_the_default_live_window():
    registry = build_strategy_registry()
    needs = check_live_history(100, registry, config.require("routing"),
                               build_state_machine(config))
    assert set(needs) == {"MarketStateMachine", "TrendBreakout"}
    assert max(needs.values()) <= 100


def test_v3_route_with_100_bars_is_refused_with_the_reason():
    strategy = TrendPortfolioV3Strategy()
    assert strategy.required_history == 121
    with pytest.raises(InsufficientLiveHistory, match="TrendPortfolioV3 needs 121"):
        check_live_history(100, {strategy.name: strategy}, {"TREND_UP": strategy.name},
                           build_state_machine(config))
    check_live_history(121, {strategy.name: strategy}, {"TREND_UP": strategy.name},
                       build_state_machine(config))


def test_state_machine_need_is_checked_even_when_everything_routes_to_cash():
    machine = build_state_machine(config)
    with pytest.raises(InsufficientLiveHistory, match="MarketStateMachine"):
        check_live_history(machine.required_history - 1, {}, {"TREND_UP": "Cash"}, machine)


def test_live_engine_refuses_to_start_when_v3_is_routed_on_100_bars(tmp_path):
    from core.risk import RiskManager
    from live_trading.engine import LiveTradingEngine

    strategy = TrendPortfolioV3Strategy()
    broker = MagicMock()
    broker.exchange_id, broker.account_id, broker.market_type = "binance", "spot", "spot"
    broker.portfolio = Portfolio()
    with pytest.raises(InsufficientLiveHistory, match="TrendPortfolioV3 needs 121"):
        LiveTradingEngine(["A"], {strategy.name: strategy}, broker, RiskManager(),
                          _routed_to(strategy.name), data_fetcher=MagicMock(),
                          lookback_days=30, state_file=str(tmp_path / "status.json"),
                          close_grace_seconds=0)


# ---- A4: health persistence only on change -------------------------------

from strategies.trend_breakout import TrendBreakoutStrategy  # noqa: E402


class _CountingStore:
    def __init__(self):
        self.rows, self.writes = {}, []

    def get(self, key):
        return deepcopy(self.rows.get(key))

    def set(self, key, value):
        self.writes.append(key)
        self.rows[key] = deepcopy(value)


def _health_writes(store, strategy):
    return [key for key in store.writes if key == strategy.health_state_key]


def test_unchanged_health_is_not_rewritten_on_every_check():
    store = _CountingStore()
    strategy = TrendBreakoutStrategy()
    strategy.bind_state_store(store)
    for day in range(5):
        assert strategy.check_health(START + pd.Timedelta(days=day))
    assert len(_health_writes(store, strategy)) == 1  # first check records the baseline


def test_health_transition_is_written_and_survives_restart():
    store = _CountingStore()
    strategy = TrendBreakoutStrategy()
    strategy.bind_state_store(store)
    strategy.check_health(START)
    strategy.health.manual_lock("fixture", at=START)
    strategy.check_health(START + pd.Timedelta(days=1))
    strategy.check_health(START + pd.Timedelta(days=2))
    assert len(_health_writes(store, strategy)) == 2
    restored = TrendBreakoutStrategy()
    restored.bind_state_store(store)
    assert restored.health.status is strategy.health.status
    assert not restored.check_health(START + pd.Timedelta(days=3))


def test_closed_trade_is_always_persisted():
    store = _CountingStore()
    strategy = TrendBreakoutStrategy()
    strategy.bind_state_store(store)
    strategy.check_health(START)
    strategy.on_trade_closed(SYMBOL, -5.0, {"close_event_id": "c1", "exit_reason": "signal",
                                            "timestamp": START, "initial_risk": 10.0}, 1)
    assert len(_health_writes(store, strategy)) == 2
    assert store.rows[strategy.health_state_key]["cohorts"]


# ---- A5: one ADX definition for state and scoring ------------------------

from composition.factory import build_candidate_score_policy  # noqa: E402


def test_scoring_reads_adx_period_and_threshold_from_state():
    policy = build_candidate_score_policy(config)
    assert policy.adx_period == config.require("state", "adx_period")
    assert policy.adx_threshold == config.require("state", "adx_threshold")
    assert "adx_threshold" not in config.require("candidate_scoring")


def test_changing_state_adx_moves_the_scoring_column_with_it():
    sections = deepcopy(config._config)
    sections["state"]["adx_period"] = 21
    sections["state"]["adx_threshold"] = 30
    policy = build_candidate_score_policy(_Sections(sections))
    strategy = TrendBreakoutStrategy()
    strategy.configure_score_policy(policy)
    assert (policy.adx_threshold, strategy._adx_column) == (30, "ADX_21")
    assert strategy.required_history == 42


def test_conflicting_scoring_adx_copy_fails_closed():
    sections = deepcopy(config._config)
    sections["candidate_scoring"]["adx_threshold"] = 20
    with pytest.raises(ValueError, match="conflicts with state.adx_threshold"):
        build_candidate_score_policy(_Sections(sections))
    sections["candidate_scoring"]["adx_threshold"] = sections["state"]["adx_threshold"]
    assert build_candidate_score_policy(_Sections(sections)).adx_threshold == 25
