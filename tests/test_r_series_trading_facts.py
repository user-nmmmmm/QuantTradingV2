"""R-series money and lifecycle contracts exercised through real venues."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from backtest.reporting.trades import TradeReconstructionMixin
from config.config import config
from core.accounts import AccountMode
from core.broker import Broker, BacktestOrderStatus
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.risk.actions import RiskActionPlan
from core.state import MarketState
from strategies.base import Strategy


class Probe(Strategy):
    def __init__(self):
        super().__init__("Probe", set(MarketState))
        self.callbacks = []
        self.partials = []

    def should_enter(self, *args):
        return None

    def should_exit(self, *args):
        return None

    def on_trade_closed(self, symbol, pnl, trade, bar_index):
        self.callbacks.append((symbol, pnl, trade, bar_index))

    def on_partial_close(self, symbol, pnl, event, bar_index):
        self.partials.append((symbol, pnl, bar_index))


def bar(day, price=100., volume=1000.):
    return pd.Series(dict(open=price, high=price, low=price, close=price, volume=volume),
                     name=pd.Timestamp("2024-01-01") + pd.Timedelta(days=day))


def fill(broker, side, qty, day, price=100., symbol="A", volume=1000., **kwargs):
    order = broker.submit_order(symbol, side, qty, price,
                                timestamp=bar(day-1).name, strategy_id="Probe", **kwargs)
    broker.process_orders({symbol: bar(day, price, volume)})
    return order


def test_cash_limit_includes_resolved_cost_and_execution_rechecks_gap():
    broker = Broker(Portfolio(1000), commission_rate=.01, slippage=.05)
    risk = RiskManager(max_leverage=10, max_pos_size_pct=1, min_entry_notional_pct=0)
    risk.drawdown_budget.bind(broker, {})
    risk.drawdown_budget.update({"A": 100}, {"A": bar(0)}, bar(0).name)
    qty = risk.clamp_entry_qty(broker.portfolio, "A", 100, 100,
                              current_prices={"A": 100}, reservation_projection=broker.reservation_projection)
    assert qty < 10
    assert risk.drawdown_budget.costs["slippage_bps"] == 500
    fill(broker, "buy", qty, 1, price=110)
    assert broker.portfolio.cash >= 0
    assert broker.trades[0]["qty"] * broker.trades[0]["fill_price"] * 1.01 <= 1000
    assert any(item["reason"] == "cash_affordability" for item in broker.execution_audit)


def test_short_reserves_exposure_but_no_spot_cash():
    broker = Broker(Portfolio(1000, account_mode="spot_margin"))
    broker.submit_order("A", "short", 2, 100, timestamp=bar(0).name)
    broker.submit_order("B", "buy", 3, 50, timestamp=bar(0).name)
    assert broker.reservation_projection.pending_notional() == {"A": 200, "B": 150}
    assert broker.reservation_projection.pending_cash() == 150


def test_price_none_market_uses_only_current_symbol_mark():
    broker = Broker(Portfolio(10000), commission_rate=0)
    risk = RiskManager(drawdown_budget_policy={"enabled": True}, min_entry_notional_pct=0)
    risk.drawdown_budget.bind(broker, {})
    risk.drawdown_budget.update({"A": 100}, {"A": bar(0)}, bar(0).name)
    order = broker.submit_order("A", "buy", 1, None, timestamp=bar(0).name, stop_loss=90)
    assert order.status is BacktestOrderStatus.CREATED
    assert order.intent.reference_price == 100
    assert broker.reservation_projection.pending_notional() == {"A": 100}
    broker.process_orders({"A": bar(1)})
    assert broker.portfolio.get_position("A")["qty"] == 1
    stale = broker.submit_order("A", "buy", 1, None, timestamp=bar(2).name, stop_loss=90)
    assert stale.status is BacktestOrderStatus.REJECTED
    risk.drawdown_budget.update({"A": 100}, {}, bar(2).name)
    bad = broker.submit_order("A", "buy", 1, None, timestamp=bar(2).name, stop_loss=90)
    assert bad.status is BacktestOrderStatus.REJECTED


def test_partial_closes_emit_one_position_callback_and_local_symbol_index():
    broker, strategy = Broker(Portfolio(10000), commission_rate=0), Probe()
    fill(broker, "buy", 2, 1, stop_loss=90, approved_risk_amount=20)
    fill(broker, "buy", 1, 1, symbol="B", stop_loss=90)
    fill(broker, "sell", 1, 2, price=90)
    strategy._consume_execution_trades("A", 7, broker.portfolio, broker)
    assert strategy.callbacks == [] and len(strategy.partials) == 1
    fill(broker, "sell", 1, 3, price=110)
    fill(broker, "sell", 1, 3, price=105, symbol="B")
    strategy._consume_execution_trades("A", 8, broker.portfolio, broker)
    strategy._consume_execution_trades("B", 2, broker.portfolio, broker)
    strategy._consume_execution_trades("A", 99, broker.portfolio, broker)
    assert [(c[0], c[1], c[3]) for c in strategy.callbacks] == [("A", 0, 8), ("B", 5, 2)]
    assert strategy.callbacks[0][2]["initial_risk"] == 20


def test_merged_partial_entries_and_exits_conserve_authoritative_lot_facts():
    broker = Broker(Portfolio(10000), commission_rate=.01)
    order = broker.submit_order("A", "buy", 2, 100, timestamp=bar(0).name,
                                strategy_id="Probe", stop_loss=90, approved_risk_amount=20)
    broker.process_orders({"A": bar(1, 100, 1)})
    broker.process_orders({"A": bar(2, 120, 1)})
    assert broker.portfolio.open_lots("A")[0].initial_risk == 20
    fill(broker, "sell", 1, 3, 130)
    fill(broker, "sell", 1, 4, 140)
    reporter = TradeReconstructionMixin()
    legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
    assert len(legs) == 2
    assert len({leg["lot_id"] for leg in legs}) == 1
    assert all(leg["position_id"] for leg in legs)
    assert sum(leg["qty"] for leg in legs) == 2
    assert sum(leg["initial_risk"] for leg in legs) == 20
    assert sum(leg["net_pnl"] for leg in legs) == pytest.approx(sum(event.realized_pnl for event in broker.close_events))
    assert sum(leg["net_pnl"] for leg in legs) == pytest.approx(broker.portfolio.cash - 10000)
    assert reporter._aggregate_round_trips(legs)[0]["initial_risk"] == 20


def test_one_exit_maps_all_partial_entry_fragments_to_same_lot():
    broker = Broker(Portfolio(10000), commission_rate=0)
    broker.submit_order("A", "buy", 2, 100, timestamp=bar(0).name, stop_loss=90, approved_risk_amount=20)
    broker.process_orders({"A": bar(1, 100, 1)})
    broker.process_orders({"A": bar(2, 110, 1)})
    fill(broker, "sell", 2, 3, 120)
    legs = TradeReconstructionMixin()._reconstruct_closed_trades(pd.DataFrame(broker.trades))
    assert len(legs) == 2 and all(leg["position_id"] for leg in legs)
    assert sum(leg["initial_risk"] for leg in legs) == 20
    assert sum(leg["net_pnl"] for leg in legs) == 30


def test_legacy_theoretical_price_is_explicitly_unavailable():
    records = pd.DataFrame([
        dict(symbol="A", side="buy", qty=1, fill_price=101, commission=1, slip=1, strategy="Legacy"),
        dict(symbol="A", side="sell", qty=1, fill_price=109, commission=1, slip=1),
    ])
    leg = TradeReconstructionMixin()._reconstruct_closed_trades(records)[0]
    assert leg["gross_pnl_theoretical"] is None
    assert leg["cost_semantics"] == "legacy_fill_price_includes_slippage"


def test_short_borrow_clock_uses_real_segments_and_remaining_quantity():
    broker = Broker(Portfolio(10000, account_mode="spot_margin"), commission_rate=0,
                    default_borrow_rate_annual=.365)
    fill(broker, "short", 2, 1)
    # Real matching settles the previous quantity before the close; no caller
    # pre-accrual is needed to preserve the last holding interval.
    fill(broker, "cover", 1, 2)
    fill(broker, "cover", 1, 3)
    assert "A" not in broker._last_borrow_time
    broker.accrue_carry({"A": bar(30)})
    fill(broker, "short", 1, 50)
    broker.accrue_carry({"A": bar(51)})
    coin_cost = sum(row.amount for row in broker.portfolio.financing_ledger if row.kind == "borrow")
    assert coin_cost == pytest.approx(.4)


def engine_reduction_trace(tmp_path, *, missing_symbol=False, cancel=False, stop=False, settle=False, reject=False):
    """Observe the real engine before end-window settlement changes the target."""
    original = deepcopy(config._config)
    made = []
    trace = []
    def make_broker(portfolio, **kwargs):
        kwargs.update(commission_rate=0, slippage=0, max_participation_rate=1,
                      use_impact_cost=False, spread_bps=0, volatility_slippage_factor=0)
        broker = Broker(portfolio, **kwargs)
        for symbol in (["A", "B"] if missing_symbol else ["A"]):
            portfolio.update_position(symbol, 10, 100, strategy_id="Probe", order_id=f"entry-{symbol}", stop_price=50)
        if stop:
            broker.submit_order("A", "sell", 10, 95, order_type="stop",
                                timestamp=bar(-1).name, exit_reason="protective_stop")
        original_execute = broker._execute_trade
        rejected = []
        def execute(order, price, timestamp, *args, **kw):
            if reject and not rejected and order.exit_reason == "DrawdownReduce":
                rejected.append(order.id)
                broker._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
                return None
            return original_execute(order, price, timestamp, *args, **kw)
        broker._execute_trade = execute
        original_process = broker.process_orders
        canceled = []
        def process(bars, *args, **kw):
            if cancel and not canceled and "A" in bars and bars["A"].name == bar(1).name:
                broker.cancel_symbol_orders("A")
                canceled.append(True)
            return original_process(bars, *args, **kw)
        broker.process_orders = process
        original_force = broker.force_liquidate
        def force(*args, **kw):
            result = original_force(*args, **kw)
            trace.append({"positions": {s: p["qty"] for s,p in portfolio.positions.items()},
                          "active": [{"symbol": o.symbol, "remaining": o.remaining_qty, "id": o.id}
                                     for o in broker.active_orders]})
            return result
        broker.force_liquidate = force
        made.append(broker)
        return broker
    try:
        config._config["drawdown_budget"] = {"enabled": False}
        days = 6 if reject else 5
        frame = pd.DataFrame([bar(i, volume=1).to_dict() for i in range(days)],
                             index=[bar(i).name for i in range(days)])
        data = {"A": frame}
        if missing_symbol:
            data["B"] = frame.iloc[1:]
        engine = BacktestEngine(initial_capital=10000, slippage=0, warmup_period=0, alignment_mode="union")
        from contextlib import nullcontext
        with patch("backtest.engine.Broker", side_effect=make_broker), \
             patch("backtest.engine.plan_risk_action", return_value=RiskActionPlan("fixed-reduce", "DrawdownReduce", .5)), \
             (nullcontext() if settle else patch.object(engine, "_close_tail_positions", return_value=None)):
            engine.run(data, strategies={"Probe": Probe()}, routing_log_path=str(tmp_path / "routing.csv"))
        return trace, made[0]
    finally:
        config._config = original


def test_ver01_original_gtc_reduction_continues_through_real_engine(tmp_path):
    trace, broker = engine_reduction_trace(tmp_path)
    assert broker.portfolio.get_position("A")["qty"] == 5
    assert [trade["qty"] for trade in broker.trades] == [1]*5
    assert len({trade["order_id"] for trade in broker.trades}) == 1
    assert not broker.active_orders
    import json, os
    if os.environ.get("R_SERIES_TRACE_FILE"):
        from pathlib import Path
        Path(os.environ["R_SERIES_TRACE_FILE"]).write_text(json.dumps({
            "scenario": "10 units, target 5, one unit per real bar",
            "force_calls": trace,
            "fills": [{"time": str(t["fill_time"]), "qty": t["qty"], "order_id": t["order_id"]} for t in broker.trades],
            "final_qty": broker.portfolio.get_position("A")["qty"],
            "active_orders": len(broker.active_orders),
        }, indent=2), encoding="utf-8")


def test_risk_action_missing_union_bar_retries_without_reducing_a_twice(tmp_path):
    trace, broker = engine_reduction_trace(tmp_path, missing_symbol=True)
    assert broker.portfolio.get_position("A")["qty"] == 5
    assert broker.portfolio.get_position("B")["qty"] == 6
    assert len({trade["order_id"] for trade in broker.trades if trade["symbol"] == "A"}) == 1
    assert broker.risk_action_targets["fixed-reduce"]["B"]["target_qty"] == 5
    broker.process_orders({"B": bar(6, volume=1)})
    assert broker.portfolio.get_position("B")["qty"] == 5


def test_canceled_reduction_retries_fixed_remaining_target(tmp_path):
    trace, broker = engine_reduction_trace(tmp_path, cancel=True)
    assert broker.portfolio.get_position("A")["qty"] == 5
    assert sum(trade["qty"] for trade in broker.trades) == 5


def test_synthetic_tail_time_does_not_refresh_real_bar_volume():
    broker = Broker(Portfolio(10000), commission_rate=0, max_participation_rate=.1)
    broker.portfolio.update_position("A", 2, 100)
    fill(broker, "sell", 1, 1, volume=10)
    broker.submit_order("A", "sell", 1, 100, timestamp=bar(1).name)
    synthetic = bar(1, volume=10)
    synthetic["liquidity_source_time"] = synthetic.name
    synthetic.name += pd.Timedelta(microseconds=1)
    assert broker.process_orders({"A": synthetic}) == []
    assert broker.portfolio.get_position("A")["qty"] == 1


def test_ver01_protective_stop_is_superseded_without_duplicate_reduction(tmp_path):
    trace, broker = engine_reduction_trace(tmp_path, stop=True)
    assert broker.portfolio.get_position("A")["qty"] == 5
    assert len({trade["order_id"] for trade in broker.trades}) == 1
    assert all(trade["exit_reason"] == "DrawdownReduce" for trade in broker.trades)


def test_real_engine_tail_cannot_reuse_depleted_final_bar(tmp_path):
    with pytest.raises(ValueError, match="actual liquidity"):
        engine_reduction_trace(tmp_path, settle=True)


@pytest.mark.parametrize("exit_volume", [1000., 0.])
def test_pit_member_exits_at_own_final_bar_and_uses_local_index(tmp_path, exit_volume):
    original = deepcopy(config._config)
    made = []
    strategy = Probe()
    def venue(portfolio, **kwargs):
        broker = Broker(portfolio, **kwargs)
        portfolio.update_position("A", 1, 100, strategy_id="Probe", order_id="entry-a", stop_price=90)
        made.append(broker)
        return broker
    try:
        config._config["drawdown_budget"] = {"enabled": False}
        a = pd.DataFrame([bar(i).to_dict() for i in range(2)], index=[bar(i).name for i in range(2)])
        a["scheduled_exit"] = [False, True]
        a.loc[a.index[-1], "volume"] = exit_volume
        b = pd.DataFrame([bar(i).to_dict() for i in range(5)], index=[bar(i).name for i in range(5)])
        engine = BacktestEngine(initial_capital=10000, slippage=0, warmup_period=0, alignment_mode="union")
        with patch("backtest.engine.Broker", side_effect=venue):
            if exit_volume == 0:
                with pytest.raises(ValueError, match="Insufficient real liquidity"):
                    engine.run({"A": a, "B": b}, strategies={"Probe": strategy}, routing_log_path=str(tmp_path / "pit.csv"))
                assert made[0].portfolio.get_position("A")["qty"] == 1
                assert not made[0].trades
                return
            engine.run({"A": a, "B": b}, strategies={"Probe": strategy}, routing_log_path=str(tmp_path / "pit.csv"))
        exits = [t for t in made[0].trades if t["symbol"] == "A"]
        assert len(exits) == 1
        assert exits[0]["exit_reason"] == "AnnouncedMarginDelisting"
        assert exits[0]["fill_time"] == bar(1).name
        assert strategy.callbacks[0][3] == 1
        assert made[0].portfolio.get_position("A")["qty"] == 0
    finally:
        config._config = original

def test_durable_live_projection_keeps_approved_risk_across_gap_and_replay(tmp_path):
    from core.domain import FillRecord, OrderIntent
    from core.order_store import OrderStore
    from core.live_broker.fill_projection import replay_fill_projection
    store = OrderStore(str(tmp_path / "fills.sqlite"))
    opening = OrderIntent(exchange="paper", account="test", symbol="A/USDT", timeframe="1d",
                          bar_time=bar(0).name.isoformat(), strategy_id="Probe", action="buy",
                          requested_qty=2, price=100, reference_price=100, initial_stop=90,
                          approved_risk_amount=20, sequence=0)
    closing = OrderIntent(exchange="paper", account="test", symbol="A/USDT", timeframe="1d",
                          bar_time=bar(3).name.isoformat(), strategy_id="Probe", action="sell",
                          requested_qty=2, price=130, sequence=1)
    for intent in (opening, closing):
        store.create_intent(intent, intent.bar_time)
    for index, (intent, qty, price, day) in enumerate(((opening, 1, 100, 1), (opening, 1, 120, 2), (closing, 2, 130, 4))):
        fact = FillRecord(fill_id=f"fill-{index}", client_order_id=intent.client_order_id,
                          exchange_order_id=f"venue-{intent.client_order_id}", qty=qty,
                          price=price, fee=0, fee_currency="USDT", timestamp=bar(day).name.isoformat(),
                          payload={"id": str(index)}, symbol="A/USDT", side=intent.action)
        assert store.add_fill(fact)
        assert not store.add_fill(fact)
    for intent in (opening, closing):
        store.update(intent.client_order_id, filled_qty=2, remaining_qty=0, status="filled")
    books, events, issues = replay_fill_projection(store, "USDT")
    assert not issues
    assert books["A/USDT"].net_qty == 0
    assert sum(event.initial_risk for event in events) == 20
    assert sum(event.realized_pnl for event in events) == 40
    ids = [event.close_event_id for event in events]
    store.close()
    reopened = OrderStore(str(tmp_path / "fills.sqlite"))
    _, replayed, issues = replay_fill_projection(reopened, "USDT")
    assert not issues
    assert [event.close_event_id for event in replayed] == ids
    assert sum(event.initial_risk for event in replayed) == 20
    reopened.close()


def test_report_csv_nested_lot_facts_roundtrip_without_identity_loss(tmp_path):
    broker = Broker(Portfolio(10000), commission_rate=0)
    fill(broker, "buy", 2, 1, stop_loss=90, approved_risk_amount=20)
    fill(broker, "sell", 2, 2, price=110)
    path = tmp_path / "trades.csv"
    pd.DataFrame(broker.trades).to_csv(path, index=False)
    legs = TradeReconstructionMixin()._reconstruct_closed_trades(pd.read_csv(path))
    assert len(legs) == 1 and legs[0]["position_id"]
    assert legs[0]["initial_risk"] == 20
    assert legs[0]["net_pnl"] == 20


def test_strategy_whose_lot_exits_first_completes_when_other_strategy_flattens():
    broker = Broker(Portfolio(10000), commission_rate=0)
    first, second = Probe(), Probe()
    second.name = "Second"
    fill(broker, "buy", 1, 1)
    broker.submit_order("A", "buy", 1, 100, timestamp=bar(1).name, strategy_id="Second")
    broker.process_orders({"A": bar(2)})
    fill(broker, "sell", 1, 3, price=90)
    first._consume_execution_trades("A", 3, broker.portfolio, broker)
    assert not first.callbacks
    fill(broker, "sell", 1, 4, price=110)
    for strategy in (first, second):
        strategy._consume_execution_trades("A", 4, broker.portfolio, broker)
    assert [call[1] for call in first.callbacks] == [-10]
    assert [call[1] for call in second.callbacks] == [10]
    assert first.callbacks[0][2]["timestamp"] == bar(4).name


def test_fok_cannot_be_cash_clamped_into_partial_fill():
    broker = Broker(Portfolio(100), commission_rate=.01)
    order = broker.submit_order("A", "buy", 1, 100, timestamp=bar(0).name, time_in_force="FOK")
    assert broker.process_orders({"A": bar(1)}) == []
    assert order.status is BacktestOrderStatus.REJECTED
    assert broker.portfolio.cash == 100


def test_rejected_reduction_retries_without_losing_fixed_target(tmp_path):
    trace, broker = engine_reduction_trace(tmp_path, reject=True)
    assert broker.portfolio.get_position("A")["qty"] == 5
    assert sum(trade["qty"] for trade in broker.trades) == 5
    assert all(item["positions"]["A"] >= 5 for item in trace)


def test_borrow_clocks_do_not_rewind_on_duplicate_or_out_of_order_bars():
    broker = Broker(Portfolio(10000, account_mode="spot_margin"), commission_rate=0,
                    default_borrow_rate_annual=.365)
    fill(broker, "short", 1, 1)
    broker.accrue_carry({"A": bar(3)})
    first_cost = broker.portfolio.cumulative_financing_cost
    broker.accrue_carry({"A": bar(2)})
    broker.accrue_carry({"A": bar(3)})
    assert broker.portfolio.cumulative_financing_cost == first_cost
    assert broker._last_borrow_time["A"] == bar(3).name
    assert broker._last_borrow_time[broker.QUOTE_BORROW_SYMBOL] == bar(3).name
    broker.accrue_carry({"A": bar(4)})
    assert broker.portfolio.cumulative_financing_cost == pytest.approx(.3)
