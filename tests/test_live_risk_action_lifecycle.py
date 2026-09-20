"""Durable risk actions exercised through the real live broker and order ledger."""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config.config import config
from core.risk import RiskManager
from core.risk.actions import RiskActionPlan
from core.risk.circuit_breaker import BreakerAction, RiskControlDecision
from core.state_store_v2 import StateStore
from live_trading.engine import LiveTradingEngine
from tests.test_revalidation_execution import Harness, NOW, SYMBOL, broker, enter
from tests.test_strategy_remediation import live_budget_engine


REDUCE = RiskActionPlan("portfolio-reduce-1", "DrawdownReduce", 0.5)
KEY = f"portfolio_risk_action:{REDUCE.action_id}"


def engine(broker, path):
    value = Harness(broker)
    value.state_store = StateStore(str(path))
    return value


def finish_pending(broker, record, trade_id="remaining-risk-fill"):
    payload = broker.exchange.orders[record["exchange_order_id"]]
    remaining = payload["remaining"]
    payload.update(status="closed", filled=payload["amount"], remaining=0)
    payload["trades"].append({"id": trade_id, "amount": remaining, "price": 100,
                              "datetime": NOW.isoformat(), "fee": {"cost": 0, "currency": "USDT"}})
    broker.exchange.qty -= remaining
    broker.exchange.cash += remaining * 100


def test_completed_reduce_survives_restart_without_stop_churn_or_new_position_reduction(broker, tmp_path):
    enter(broker)
    path = tmp_path / "risk.db"
    h = engine(broker, path)
    h._reconcile_protective_orders()
    assert h._reconcile_portfolio_risk_action(REDUCE).performed
    h._reconcile_protective_orders()
    checkpoint = h.state_store.get(KEY)
    assert checkpoint["status"] == "completed" and checkpoint["completed_at"]
    assert checkpoint["positions"][SYMBOL]["original_qty"] == 1
    assert checkpoint["positions"][SYMBOL]["target_qty"] == 0.5
    protection = h._venue_protective_orders()[0].order_id
    count = len(broker.exchange.requests)
    h.state_store.close()
    h = engine(broker, path)
    for _ in range(3):
        assert not h._reconcile_portfolio_risk_action(REDUCE).blocks_entries
        h._reconcile_protective_orders()
    assert len(broker.exchange.requests) == count
    assert h._venue_protective_orders()[0].order_id == protection
    broker.submit_order(SYMBOL, "sell", 0.5, reference_price=100, sequence=77)
    broker.submit_order(SYMBOL, "buy", 1, reference_price=100, stop_loss=90,
                        strategy_id="TrendBreakout", sequence=78)
    assert not h._reconcile_portfolio_risk_action(REDUCE).blocks_entries
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 1
    h.state_store.close()


def test_partial_reduce_keeps_residual_protection_and_pending_order_across_restart(broker, tmp_path):
    enter(broker)
    path = tmp_path / "risk.db"
    h = engine(broker, path)
    broker.exchange.exit_fraction = 0.5
    assert h._reconcile_portfolio_risk_action(REDUCE).pending
    h._reconcile_protective_orders()
    assert h._venue_protective_orders()[0].qty == 0.5
    pending = next(row for row in broker.order_store.list_non_terminal() if row["order_type"] == "market")
    count = len(broker.exchange.requests)
    h.state_store.close()
    h = engine(broker, path)
    assert h._reconcile_portfolio_risk_action(REDUCE).pending
    h._reconcile_protective_orders()
    assert len(broker.exchange.requests) == count
    finish_pending(broker, pending)
    progress = h._reconcile_portfolio_risk_action(REDUCE)
    assert not progress.pending and progress.blocks_entries
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0.5
    h._reconcile_protective_orders()
    assert len(broker.exchange.requests) == count
    assert h.state_store.get(KEY)["status"] == "completed"
    h.state_store.close()


def test_unknown_own_order_never_submits_another_exit(broker, tmp_path):
    enter(broker)
    h = engine(broker, tmp_path / "risk.db")
    broker.exchange.exit_fraction = 0.5
    assert h._reconcile_portfolio_risk_action(REDUCE).pending
    h._reconcile_protective_orders()
    count = len(broker.exchange.requests)
    broker.retry_max_attempts = 1
    broker.exchange.cancel_timeout = True
    assert h._reconcile_portfolio_risk_action(REDUCE).pending
    h._reconcile_protective_orders()
    assert len(broker.exchange.requests) == count
    assert broker.has_unresolved_unknown()
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0.75
    h.state_store.close()


def test_reached_target_cancels_only_its_live_remainder(broker, tmp_path):
    enter(broker)
    h = engine(broker, tmp_path / "risk.db")
    broker.exchange.exit_fraction = 0.5
    assert h._reconcile_portfolio_risk_action(REDUCE).pending
    pending = broker.order_store.list_non_terminal()[0]
    broker.exchange.exit_fraction = 1
    broker.submit_order(SYMBOL, "sell", 0.25, reference_price=100, sequence=94)
    h._reconcile_protective_orders()
    stop_id = h._venue_protective_orders()[0].order_id
    count = len(broker.exchange.requests)
    assert not h._reconcile_portfolio_risk_action(REDUCE).pending
    assert broker.order_store.get(pending["client_order_id"])["status"] == "canceled"
    assert broker.order_store.get(stop_id)["status"] == "accepted"
    assert len(broker.exchange.requests) == count
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0.5
    h.state_store.close()


def test_open_cancel_failure_leaves_preparing_checkpoint_and_protection(broker, tmp_path):
    enter(broker, 0.4)
    h = engine(broker, tmp_path / "risk.db")
    h._reconcile_protective_orders()
    stop_id = h._venue_protective_orders()[0].order_id
    broker.exchange.cancel_timeout = True
    broker.retry_max_attempts = 1
    count = len(broker.exchange.requests)
    assert h._reconcile_portfolio_risk_action(REDUCE).pending
    checkpoint = h.state_store.get(KEY)
    assert checkpoint["status"] == "preparing" and checkpoint["positions"] == {}
    assert len(broker.exchange.requests) == count
    assert broker.order_store.get(stop_id)["status"] == "accepted"
    h.state_store.close()


def test_late_open_fill_is_included_before_target_freeze(broker, tmp_path):
    entry = enter(broker, 0.4)
    h = engine(broker, tmp_path / "risk.db")
    original_cancel = broker.exchange.cancel_order

    def late_fill_then_cancel(key, symbol):
        if key == entry.exchange_order_id:
            payload = broker.exchange.orders[key]
            payload.update(filled=0.8, remaining=0.2)
            payload["trades"].append({"id": "late-entry", "amount": 0.4, "price": 100,
                                      "datetime": NOW.isoformat(), "fee": {"cost": 0, "currency": "USDT"}})
            broker.exchange.qty += 0.4
            broker.exchange.cash -= 40
        return original_cancel(key, symbol)

    with patch.object(broker.exchange, "cancel_order", side_effect=late_fill_then_cancel):
        assert not h._reconcile_portfolio_risk_action(REDUCE).pending
    original = h.state_store.get(KEY)["positions"][SYMBOL]
    assert original["original_qty"] == pytest.approx(0.8)
    assert original["target_qty"] == pytest.approx(0.4)
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(0.4)
    h.state_store.close()


@pytest.mark.parametrize("boundary", range(1, 7))
def test_restart_after_each_checkpoint_write_does_not_repeat_reduction(broker, tmp_path, boundary):
    enter(broker)
    path = tmp_path / "risk.db"
    h = engine(broker, path)
    setter = h.state_store.set
    writes = 0

    def crash_after_write(key, value):
        nonlocal writes
        setter(key, value)
        writes += 1
        if writes == boundary:
            raise RuntimeError("simulated process interruption")

    with patch.object(h.state_store, "set", side_effect=crash_after_write):
        with pytest.raises(RuntimeError, match="process interruption"):
            h._reconcile_portfolio_risk_action(REDUCE)
    h.state_store.close()
    h = engine(broker, path)
    # Recovery must still work if the breaker no longer emits the old action.
    progress = h._reconcile_portfolio_risk_action(None)
    if broker.portfolio.get_position(SYMBOL)["qty"] > 0.5:
        progress = h._reconcile_portfolio_risk_action(REDUCE)
    assert not progress.pending
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0.5
    assert sum(event.qty for event in broker.close_events) == 0.5
    assert h.state_store.get(KEY)["status"] == "completed"
    h.state_store.close()


def test_restart_after_exchange_fill_before_completion_does_not_reduce_new_position(broker, tmp_path):
    enter(broker)
    path = tmp_path / "risk.db"
    h = engine(broker, path)
    submit = broker.submit_order

    def submit_and_crash(*args, **kwargs):
        result = submit(*args, **kwargs)
        raise RuntimeError(f"process interrupted after {result.status.value}")

    with patch.object(broker, "submit_order", side_effect=submit_and_crash):
        with pytest.raises(RuntimeError, match="process interrupted"):
            h._reconcile_portfolio_risk_action(REDUCE)
    h.state_store.close()
    broker.submit_order(SYMBOL, "sell", 0.5, reference_price=100, sequence=89)
    broker.submit_order(SYMBOL, "buy", 1, reference_price=100, stop_loss=90,
                        strategy_id="TrendBreakout", sequence=90)
    h = engine(broker, path)
    h._reconcile_protective_orders()
    stop_id = h._venue_protective_orders()[0].order_id
    count = len(broker.exchange.requests)
    assert not h._reconcile_portfolio_risk_action(REDUCE).pending
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 1
    assert len(broker.exchange.requests) == count
    assert h._venue_protective_orders()[0].order_id == stop_id
    h.state_store.close()


def test_legacy_target_without_position_evidence_preserves_stop_and_blocks_entries(broker, tmp_path):
    enter(broker)
    h = engine(broker, tmp_path / "risk.db")
    h._reconcile_protective_orders()
    stop_id = h._venue_protective_orders()[0].order_id
    h.state_store.set(f"risk_targets:{REDUCE.action_id}", {SYMBOL: 0.5})
    count = len(broker.exchange.requests)
    for _ in range(2):
        assert h._reconcile_portfolio_risk_action(REDUCE).pending
    checkpoint = h.state_store.get(KEY)
    assert checkpoint["status"] == "legacy_unverifiable"
    assert "legacy_position_identity_missing" in checkpoint["recovery_reason"]
    assert h.state_store.get(f"portfolio_risk_action_legacy_backup:{REDUCE.action_id}")["targets"] == {SYMBOL: 0.5}
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 1
    assert len(broker.exchange.requests) == count
    assert h._venue_protective_orders()[0].order_id == stop_id
    h.state_store.close()


def test_legacy_target_with_attributed_fills_migrates_without_stop_churn(broker, tmp_path):
    enter(broker)
    h = engine(broker, tmp_path / "risk.db")
    assert h._submit_risk_exit(SYMBOL, REDUCE.reason, 0.5, REDUCE.action_id, 100)
    h._reconcile_protective_orders()
    count = len(broker.exchange.requests)
    h.state_store.set(f"risk_targets:{REDUCE.action_id}", {SYMBOL: 0.5})
    assert not h._reconcile_portfolio_risk_action(REDUCE).pending
    checkpoint = h.state_store.get(KEY)
    assert checkpoint["status"] == "completed"
    assert checkpoint["migration"]["evidence"] == "attributed_close_events_and_order_ledger"
    assert len(broker.exchange.requests) == count
    h.state_store.close()


def test_liquidation_continues_after_completion_for_later_inventory(broker, tmp_path):
    enter(broker)
    h = engine(broker, tmp_path / "risk.db")
    liquidation = RiskActionPlan("liquidation-1", "AccountLiquidation", 0)
    for sequence in range(3):
        assert not h._reconcile_portfolio_risk_action(liquidation).pending
        assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
        if sequence < 2:
            broker.submit_order(SYMBOL, "buy", 1, reference_price=100, stop_loss=90,
                                strategy_id="TrendBreakout", sequence=80 + sequence)
    assert sum(event.qty for event in broker.close_events) == 3
    h.state_store.close()


def test_stronger_liquidation_supersedes_partial_reduce_without_competing_orders(broker, tmp_path):
    enter(broker)
    h = engine(broker, tmp_path / "risk.db")
    broker.exchange.exit_fraction = 0.5
    assert h._reconcile_portfolio_risk_action(REDUCE).pending
    broker.exchange.exit_fraction = 1
    liquidation = RiskActionPlan("liquidation-1", "AccountLiquidation", 0)
    assert not h._reconcile_portfolio_risk_action(liquidation).pending
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert h.state_store.get(KEY)["completion_reason"] == "superseded"
    assert not broker.order_store.list_non_terminal()
    h.state_store.close()


def test_budget_snapshot_is_written_while_portfolio_action_owns_execution(broker, tmp_path):
    broker.submit_order(SYMBOL, "buy", 10, reference_price=100, stop_loss=90,
                        approved_risk_amount=100, strategy_id="TrendBreakout")
    h = live_budget_engine(broker, tmp_path / "risk.db")
    broker.exchange.exit_fraction = 0.5
    progress = h._reconcile_portfolio_risk_action(REDUCE)
    count = len(broker.exchange.requests)
    assert progress.pending
    assert not h._reconcile_drawdown_budget(execute=not progress.blocks_entries)
    row = h.state_store.get("drawdown_budget_snapshot")
    assert row["deferred_to_portfolio_risk_action"]
    assert len(broker.exchange.requests) == count
    assert h.state_store.get("drawdown_budget_action") is None
    h.state_store.close()


def test_completed_reduce_allows_new_budget_action(broker, tmp_path):
    broker.submit_order(SYMBOL, "buy", 10, reference_price=100, stop_loss=90,
                        approved_risk_amount=100, strategy_id="TrendBreakout")
    h = live_budget_engine(broker, tmp_path / "risk.db")
    assert not h._reconcile_portfolio_risk_action(REDUCE).pending
    progress = h._reconcile_portfolio_risk_action(REDUCE)
    assert not progress.blocks_entries
    assert not h._reconcile_drawdown_budget(execute=not progress.blocks_entries)
    assert any(event.exit_reason == "DrawdownBudgetReduce" for event in broker.close_events)
    assert h.state_store.get("drawdown_budget_snapshot")["action"] == "reduce"
    h.state_store.close()


def test_live_ticks_review_budget_after_reduce_and_never_open_on_risk_action_tick(broker, tmp_path):
    broker.submit_order(SYMBOL, "buy", 10, reference_price=100, stop_loss=90,
                        approved_risk_amount=100, strategy_id="TrendBreakout")
    state = StateStore(str(tmp_path / "tick.db"))
    risk = RiskManager(drawdown_budget_policy={"enabled": True})
    risk.high_water_equity = 12400
    h = LiveTradingEngine(
        symbols=[SYMBOL], strategies={}, broker=broker, risk_manager=risk,
        configuration=config, data_fetcher=MagicMock(), clock=lambda: NOW,
        state_store=state, state_file=str(tmp_path / "status.json"), close_grace_seconds=0,
    )
    h.data_map = {SYMBOL: pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [10000.0]},
        index=pd.DatetimeIndex([pd.Timestamp(NOW) - pd.Timedelta(days=1)]),
    )}
    h._update_data = MagicMock()
    h._run_reconciliation_if_due = MagicMock()
    h._maybe_export_state = MagicMock()
    h._assess_health = MagicMock(side_effect=lambda *args: setattr(h, "_operational_state", "RUNNING"))
    h.event_processor._collect_symbol_candidate = MagicMock(return_value=(None, None))
    decision = RiskControlDecision(
        BreakerAction.REDUCE, True, True, force_reduce_fraction=0.5,
        transition_id=REDUCE.action_id,
    )
    risk.check_circuit_breaker = MagicMock(return_value=decision)
    with patch.object(broker, "risk_price_facts", return_value={SYMBOL: {"price": 100, "timestamp": NOW}}):
        h._tick_once()
        assert broker.portfolio.get_position(SYMBOL)["qty"] == 5
        assert state.get("drawdown_budget_snapshot")["deferred_to_portfolio_risk_action"]
        h.event_processor._collect_symbol_candidate.assert_not_called()
        h._tick_once()
        assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(4)
        assert state.get("drawdown_budget_snapshot")["action"] == "reduce"
        h.event_processor._collect_symbol_candidate.assert_not_called()
        stop_id = h._venue_protective_orders()[0].order_id
        h._tick_once()
        h.event_processor._collect_symbol_candidate.assert_called_once()
        assert h._venue_protective_orders()[0].order_id == stop_id
    state.close()
