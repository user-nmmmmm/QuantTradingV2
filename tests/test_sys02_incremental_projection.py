"""SYS-02: authoritative suffix projection, atomic restart and legacy boundaries."""
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest

from core.domain import FillRecord, OrderIntent
from core.broker import Broker
from core.live_broker.fill_projection import replay_fill_projection, _legacy_replay_fill_projection
from core.live_broker.reconciler import OrderReconcilerMixin
from core.lots import CloseEvent
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.state_store_v2 import StateStore
from core.state import MarketState
from core.risk import RiskManager
from strategies.base import Strategy
from strategies.mean_reversion import RangeStrategy


def intent(side="buy", *, symbol="A/USDT", qty=2, sequence=0, owner="Probe"):
    return OrderIntent(exchange="paper", account="a", symbol=symbol, timeframe="1d",
                       bar_time="2024-01-01T00:00:00Z", strategy_id=owner, action=side,
                       requested_qty=qty, price=100, reference_price=100,
                       initial_stop=90 if side == "buy" else None,
                       approved_risk_amount=qty*10 if side == "buy" else None, sequence=sequence)


def add(store, command, number, qty, price, day):
    store.create_intent(command, command.bar_time)
    fill = FillRecord(fill_id=f"f-{number}", client_order_id=command.client_order_id,
                      exchange_order_id=f"v-{command.client_order_id}", qty=qty, price=price,
                      fee=qty*.1, fee_currency="USDT", timestamp=f"2024-01-{day:02d}T00:00:00Z",
                      payload={"id": str(number)}, symbol=command.symbol, side=command.action)
    assert store.add_fill(fill)
    total = sum(row["qty"] for row in store.fills_for(command.client_order_id))
    store.update(command.client_order_id, filled_qty=total, remaining_qty=command.requested_qty-total,
                 status="filled" if total == command.requested_qty else "partially_filled")
    return fill


def test_projection_reads_only_new_fill_suffix_and_matches_explicit_legacy_replay(tmp_path):
    path = str(tmp_path / "orders.sqlite")
    store = OrderStore(path)
    opening, closing = intent(), intent("sell", sequence=1)
    add(store, opening, 1, 1, 100, 2)
    books, events, issues = replay_fill_projection(store, "USDT")
    assert not issues and books[opening.symbol].net_qty == 1
    add(store, opening, 2, 1, 120, 3)
    books, events, issues = replay_fill_projection(store, "USDT")
    assert books[opening.symbol].open_lots[0].initial_risk == 20
    add(store, closing, 3, 2, 130, 4)
    books, events, issues = replay_fill_projection(store, "USDT")
    _, reference, reference_issues = _legacy_replay_fill_projection(store, "USDT")
    assert not issues and not reference_issues
    assert [asdict(row) for row in events] == [asdict(row) for row in reference]
    assert events[0].realized_pnl == pytest.approx(39.6)
    projector = store._fill_projection_cache["USDT"]
    assert projector.processed_fill_count == 3
    # No production path is allowed to rebuild via historical order/fill APIs.
    with patch.object(store, "list_with_fills", side_effect=AssertionError("full scan")), \
         patch.object(store, "fills_for", side_effect=AssertionError("full scan")):
        for _ in range(20):
            assert replay_fill_projection(store, "USDT")[1] is events
    assert projector.processed_fill_count == 3
    store.close()
    reopened = OrderStore(path)
    with patch.object(reopened, "list_with_fills", side_effect=AssertionError("full scan")), \
         patch.object(reopened, "fills_for", side_effect=AssertionError("full scan")):
        _, restored, issues = replay_fill_projection(reopened, "USDT")
    assert not issues and [asdict(row) for row in restored] == [asdict(row) for row in events]
    assert reopened._fill_projection_cache["USDT"].processed_fill_count == 0
    reopened.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_atomic_projection_restart_does_not_lose_or_duplicate_close(tmp_path, after_commit):
    path = str(tmp_path / "orders.sqlite")
    store = OrderStore(path)
    opening, closing = intent(), intent("sell", sequence=1)
    add(store, opening, 1, 2, 100, 2)
    replay_fill_projection(store, "USDT")
    add(store, closing, 2, 2, 110, 3)
    original = store.save_projection
    def interrupted(*args, **kwargs):
        if after_commit:
            original(*args, **kwargs)
        raise OSError("crash at projection commit boundary")
    with patch.object(store, "save_projection", side_effect=interrupted):
        with pytest.raises(OSError, match="commit boundary"):
            replay_fill_projection(store, "USDT")
    store.close()
    store = OrderStore(path)
    books, closes, issues = replay_fill_projection(store, "USDT")
    assert not issues and not books.get(opening.symbol, SimpleNamespace(net_qty=0)).net_qty
    assert len(closes) == 1 and closes[0].realized_pnl == pytest.approx(19.6)
    assert len(store.projection_close_events("ownership:USDT")) == 1
    store.close()


def test_changed_order_quantity_is_reconciled_without_history_rescan(tmp_path):
    store = OrderStore(str(tmp_path / "orders.sqlite"))
    command = intent()
    store.create_intent(command, command.bar_time)
    store.update(command.client_order_id, filled_qty=1)
    _, _, issues = replay_fill_projection(store, "USDT")
    assert issues == [f"trade_details_required:{command.client_order_id}"]
    add(store, command, 1, 1, 100, 2)
    books, _, issues = replay_fill_projection(store, "USDT")
    assert not issues and books[command.symbol].net_qty == 1
    store.close()


def test_conflicting_duplicate_fill_is_rejected_without_mutating_ledger(tmp_path):
    store = OrderStore(str(tmp_path / "orders.sqlite"))
    command = intent()
    record = add(store, command, 1, 1, 100, 2)
    assert not store.add_fill(record)
    with pytest.raises(ValueError, match="conflicting_fill_identity"):
        store.add_fill(replace(record, price=101))
    assert store.fills_for(command.client_order_id)[0]["price"] == 100
    store.close()


def test_projection_cursor_rejects_replaced_authoritative_fill_identity(tmp_path):
    path = str(tmp_path / "orders.sqlite")
    store = OrderStore(path)
    add(store, intent(), 1, 1, 100, 2)
    replay_fill_projection(store, "USDT")
    with store._connection:
        store._connection.execute("UPDATE fills SET fill_id='replacement' WHERE fill_id='f-1'")
    store.close()
    store = OrderStore(path)
    with pytest.raises(ValueError, match="cursor_identity_mismatch"):
        replay_fill_projection(store, "USDT")
    store.close()


def test_two_projection_instances_follow_committed_checkpoint_without_duplicates(tmp_path):
    path = str(tmp_path / "orders.sqlite")
    first = OrderStore(path)
    second = OrderStore(path)
    opening, closing = intent(), intent("sell", sequence=1)
    add(first, opening, 1, 2, 100, 2)
    replay_fill_projection(first, "USDT")
    replay_fill_projection(second, "USDT")
    assert second._fill_projection_cache["USDT"].processed_fill_count == 0
    add(second, closing, 2, 2, 110, 3)
    _, events, issues = replay_fill_projection(second, "USDT")
    _, observed, first_issues = replay_fill_projection(first, "USDT")
    assert not issues and not first_issues
    assert len(observed) == len(events) == len(first.projection_close_events("ownership:USDT")) == 1
    assert [asdict(event) for event in observed] == [asdict(event) for event in events]
    assert first._fill_projection_cache["USDT"].processed_fill_count == 1
    first.close()
    second.close()


def test_late_fill_is_explicitly_invalid_and_cannot_rewrite_delivered_close(tmp_path):
    store = OrderStore(str(tmp_path / "orders.sqlite"))
    opening, closing = intent(), intent("sell", sequence=1)
    add(store, opening, 1, 2, 100, 3)
    add(store, closing, 2, 2, 110, 4)
    _, events, _ = replay_fill_projection(store, "USDT")
    original = [asdict(row) for row in events]
    add(store, intent(sequence=2), 3, 1, 90, 2)
    _, events, issues = replay_fill_projection(store, "USDT")
    assert "out_of_order_fill:f-3" in issues
    assert [asdict(row) for row in events] == original
    store.close()


@pytest.mark.parametrize("missing", ["ownership", "cost", "conversion"])
def test_historical_incomplete_facts_are_invalid_not_normal_closed_samples(tmp_path, missing):
    store = OrderStore(str(tmp_path / "orders.sqlite"))
    opening, closing = intent(owner="" if missing == "ownership" else "Probe"), intent("sell", sequence=1)
    add(store, opening, 1, 2, 100, 2)
    add(store, closing, 2, 2, 110, 3)
    # Simulate an imported pre-evidence ledger. Its raw bytes/values remain
    # authoritative and are never rewritten into an assumed zero-cost record.
    with store._connection:
        if missing == "cost":
            store._connection.execute("DELETE FROM fill_evidence WHERE fill_id='f-1'")
        if missing == "conversion":
            store._connection.execute("UPDATE fills SET fee_currency='BNB' WHERE fill_id='f-1'")
    before = [tuple(row) for row in store._connection.execute("SELECT * FROM fills ORDER BY rowid")]
    books, events, issues = replay_fill_projection(store, "USDT")
    expected = {"ownership": "missing_entry_attribution_or_stop:", "cost": "missing_fee_evidence:", "conversion": "fee_conversion_required:"}[missing]
    assert any(issue.startswith(expected) for issue in issues)
    assert not events
    assert before == [tuple(row) for row in store._connection.execute("SELECT * FROM fills ORDER BY rowid")]
    store.close()


@pytest.mark.parametrize("fee_cost", [None, 0, "unavailable"])
def test_venue_missing_fee_is_distinct_from_explicit_zero_fee(tmp_path, fee_cost):
    store = OrderStore(str(tmp_path / "orders.sqlite"))
    command = intent()
    store.create_intent(command, command.bar_time)
    trade = {"id": "v1", "amount": 2, "price": 100, "datetime": "2024-01-02T00:00:00Z"}
    if fee_cost is not None:
        trade["fee"] = {"cost": fee_cost, "currency": "USDT"}
    reconciler = SimpleNamespace(order_store=store, trades=[], _as_float=lambda value: float(value or 0),
                                 _iso=str, _publish_fill_event=lambda *args: None)
    OrderReconcilerMixin._persist_fills(reconciler, command.client_order_id, "venue", {"trades": [trade]}, 2, 100)
    store.update(command.client_order_id, filled_qty=2)
    books, events, issues = replay_fill_projection(store, "USDT")
    if fee_cost == 0:
        assert not issues and books[command.symbol].net_qty == 2
    else:
        assert issues == [f"missing_fee_evidence:{command.client_order_id}:v1"]
        assert not books and not events
    assert store.fills_for(command.client_order_id)[0]["payload"] == trade
    store.close()


def close(number, *, symbol="A", position="p", fully=False, pnl=-10):
    return CloseEvent(str(number), position, "lot", symbol, "RangeMeanReversion", "signal",
                      1, 90, 90, pnl, "2024-01-02T00:00:00Z", fully, initial_risk=10)


class CountedEvents(list):
    touched = 0
    def __iter__(self):
        raise AssertionError("full close-event iteration")
    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.touched += len(value) if isinstance(key, slice) else 1
        return value


def test_strategy_cursor_reads_new_suffix_and_bounds_deduplication():
    events = CountedEvents(close(i, position=str(i), fully=True) for i in range(3000))
    broker = SimpleNamespace(close_events=events)
    strategy = RangeStrategy()
    strategy._consume_execution_trades("A", 2, Portfolio(), broker)
    assert strategy.observed_close_events == 3000
    assert len(strategy._consumed_close_event_ids) == len(strategy._completed_position_ids) == 2048
    first = events.touched
    for _ in range(20):
        strategy._consume_execution_trades("A", 3, Portfolio(), broker)
    assert events.touched - first <= 40
    events.append(close(3000, position="new", fully=True))
    strategy._consume_execution_trades("A", 4, Portfolio(), broker)
    assert strategy.observed_close_events == 3001


def test_strategy_partial_and_other_symbol_queue_survive_sqlite_restart(tmp_path):
    path = str(tmp_path / "state.sqlite")
    store = StateStore(path)
    strategy = RangeStrategy()
    strategy.bind_state_store(store)
    events = [close(1), close(2, symbol="B", position="b", fully=True)]
    broker = SimpleNamespace(close_events=events)
    strategy._consume_execution_trades("A", 1, Portfolio(), broker)
    assert strategy.get_trade_state("A")["consecutive_losses"] == 0
    store.close()
    store = StateStore(path)
    restored = RangeStrategy()
    restored.bind_state_store(store)
    events.append(close(3, fully=True, pnl=5))
    restored._consume_execution_trades("B", 7, Portfolio(), broker)
    restored._consume_execution_trades("A", 2, Portfolio(), broker)
    assert restored.observed_close_events == 3
    assert restored.get_trade_state("A")["consecutive_losses"] == 1
    assert restored.get_trade_state("B")["consecutive_losses"] == 1
    store.close()
    store = StateStore(path)
    again = RangeStrategy()
    again.bind_state_store(store)
    again._consume_execution_trades("A", 4, Portfolio(), broker)
    assert again.observed_close_events == 3
    assert again.get_trade_state("A")["consecutive_losses"] == 1
    store.close()


@pytest.mark.parametrize("after_commit", [False, True])
def test_strategy_checkpoint_interruption_is_recoverable(tmp_path, after_commit):
    path = str(tmp_path / "state.sqlite")
    store = StateStore(path)
    strategy = RangeStrategy()
    strategy.bind_state_store(store)
    broker = SimpleNamespace(close_events=[close(1, fully=True)])
    original = store.set
    def interrupted(key, value):
        if after_commit:
            original(key, value)
        raise OSError("strategy commit interrupted")
    with patch.object(store, "set", side_effect=interrupted):
        with pytest.raises(OSError):
            strategy._consume_execution_trades("A", 3, Portfolio(), broker)
    store.close()
    store = StateStore(path)
    restored = RangeStrategy()
    restored.bind_state_store(store)
    restored._consume_execution_trades("A", 3, Portfolio(), broker)
    assert restored.observed_close_events == 1
    assert restored.get_trade_state("A")["consecutive_losses"] == 1
    store.close()


def test_restored_cursor_rejects_replaced_or_truncated_stream(tmp_path):
    store = StateStore(str(tmp_path / "state.sqlite"))
    strategy = RangeStrategy()
    strategy.bind_state_store(store)
    strategy._consume_execution_trades("A", 1, Portfolio(), SimpleNamespace(close_events=[close(1, fully=True)]))
    restored = RangeStrategy()
    restored.bind_state_store(store)
    with pytest.raises(ValueError, match="replaced_or_truncated"):
        restored._consume_execution_trades("A", 2, Portfolio(), SimpleNamespace(close_events=[]))
    store.close()


@pytest.mark.parametrize("path", ["on_bar_entry", "candidate_entry", "on_bar_exit", "submit_exit"])
def test_real_strategy_submission_preserves_originating_signal_identity(path):
    class SignalProbe(Strategy):
        def __init__(self):
            super().__init__("SignalProbe", set(MarketState))
        def should_enter(self, *args):
            return {"action": "buy", "stop_loss": 90}
        def should_exit(self, *args):
            return {"action": "sell", "reason": "signal"}
    portfolio = Portfolio(10000)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    risk = RiskManager(max_leverage=10, max_pos_size_pct=1, min_entry_notional_pct=0)
    risk.drawdown_budget.bind(broker, {})
    strategy = SignalProbe()
    state = next(iter(MarketState))
    frame = pd.DataFrame({field: [100.] * 4 for field in ("open", "high", "low", "close")},
                         index=pd.date_range("2024-01-01", periods=4))
    frame["volume"] = 10000
    if "exit" in path:
        portfolio.update_position("A", 2, 100, strategy_id="SignalProbe", order_id="opening", stop_price=90)
    if path.startswith("on_bar"):
        strategy.on_bar("A", 3, frame, state, portfolio, broker, risk, {"A": 100})
    elif path == "candidate_entry":
        candidate = strategy.build_entry_candidate("A", 3, frame, state, portfolio)
        strategy.submit_entry_candidate(candidate, portfolio=portfolio, broker=broker,
                                        risk_manager=risk, current_prices={"A": 100})
    else:
        strategy.process_exit_only("A", 3, frame, state, portfolio, broker)
    decision = [event for event in broker.event_pipeline.events if event.event_type == "signal"][-1]
    command = broker.pending_orders[-1].intent
    assert command.signal_id == str(decision.event_id)
    assert command.causation_id == str(decision.event_id)
    assert decision.payload["signal_kind"] == ("exit" if "exit" in path else "entry")
