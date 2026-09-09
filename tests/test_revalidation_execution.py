"""Real broker/boundary/store with an offline exchange that maintains order facts."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.domain import OrderStatus
from core.domain import FillRecord
from core.live_broker.safe import SafeLiveBroker
from core.live_safety import StartupSafetyPolicy
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.protective_orders import ProtectiveState
from core.risk.persistent_guard import PersistentOrderSafetyGuard
from live_trading.tick_orchestrator import TickOrchestratorMixin

NOW = datetime(2026, 6, 30, tzinfo=timezone.utc)
SYMBOL = "BTC/USDT"


class OfflineBinance:
    id = "binance"
    has = {"createMarketOrder": True, "createLimitOrder": True, "createStopLossOrder": True}
    precisionMode = 4

    def __init__(self, config):
        self.options = config.get("options", {})
        self.orders, self.requests = {}, []
        self.qty, self.cash = 0.0, 10000.0
        self.entry_fraction = 1.0
        self.exit_fraction = 1.0
        self.reject_stop = False
        self.cancel_timeout = False

    def set_sandbox_mode(self, value):
        assert value is True

    def load_markets(self, *args, **kwargs):
        return {SYMBOL: {"symbol": SYMBOL, "spot": True, "type": "spot", "active": True,
                         "precision": {"amount": 0.0001, "price": 0.01},
                         "limits": {"amount": {"min": 0.0001}},
                         "info": {"orderTypes": ["MARKET", "LIMIT", "STOP_LOSS"]}}}

    def create_order(self, **request):
        self.requests.append(request)
        key = request["params"]["clientOrderId"]
        assert key not in self.orders, "duplicate exchange submission"
        stop = "stopLossPrice" in request["params"]
        if stop and self.reject_stop:
            return {"id": key, "status": "rejected", "amount": request["amount"], "filled": 0, "remaining": request["amount"]}
        qty = 0.0 if stop else request["amount"] * (self.entry_fraction if request["side"] == "buy" else self.exit_fraction)
        self.qty += qty if request["side"] == "buy" else -qty
        self.cash -= qty * 100 * (1 if request["side"] == "buy" else -1)
        trades = [{"id": f"fill:{key}", "amount": qty, "price": 100,
                   "datetime": NOW.isoformat(), "fee": {"cost": 0, "currency": "USDT"}}] if qty else []
        result = {"id": key, "clientOrderId": key, "status": "closed" if qty == request["amount"] else "open",
                  "symbol": request["symbol"], "side": request["side"], "stopLossPrice": request["params"].get("stopLossPrice"),
                  "amount": request["amount"], "filled": qty, "remaining": request["amount"] - qty,
                  "average": 100 if qty else None, "trades": trades}
        self.orders[key] = result
        return result

    def fetch_order(self, key, symbol):
        if self.cancel_timeout:
            raise TimeoutError("offline unknown")
        return self.orders[key]

    def cancel_order(self, key, symbol):
        if self.cancel_timeout:
            raise TimeoutError("offline cancellation timeout")
        self.orders[key]["status"] = "canceled"
        return self.orders[key]

    def fetch_balance(self):
        return {"total": {"USDT": self.cash, "BTC": self.qty}}


class Harness(TickOrchestratorMixin):
    def __init__(self, broker):
        self.broker = broker
        self.strategies = {"TrendBreakout": SimpleNamespace(context={SYMBOL: {"stop_loss": 90}})}
        self._snapshot = SimpleNamespace(prices={SYMBOL: 100})
        self._operational_state = "RUNNING"
        self.alerts = []

    def _now(self):
        return NOW

    def _alert(self, *args):
        self.alerts.append(args)


@pytest.fixture
def broker(tmp_path):
    policy = StartupSafetyPolicy(True, "binance", "spot", (SYMBOL,), ("binance",), ("spot",), (SYMBOL,), "USDT", 1000, 5000)
    guard = PersistentOrderSafetyGuard(policy, str(tmp_path / "risk.db"))
    with patch("core.live_broker.ccxt.binance", OfflineBinance), patch.dict("os.environ", {}, clear=True):
        value = SafeLiveBroker(Portfolio(), guard, order_store=OrderStore(str(tmp_path / "orders.db")),
                               require_market_metadata=True, require_resident_protection=True)
    value.set_bar_context("1d", NOW)
    try:
        yield value
    finally:
        value.close()
        guard.close()


def enter(broker, fraction=1.0):
    broker.exchange.entry_fraction = fraction
    result = broker.submit_order(SYMBOL, "buy", 1, reference_price=100, stop_loss=90, strategy_id="TrendBreakout")
    assert result.accepted, result.message
    return result


def test_partial_entry_protection_is_confirmed_and_attributed(broker):
    entry = enter(broker, 0.4)
    assert entry.status is OrderStatus.PARTIALLY_FILLED
    assert broker.portfolio.open_lots(SYMBOL)[0].qty_open == pytest.approx(0.4)
    assert broker.portfolio.open_lots(SYMBOL)[0].initial_risk == pytest.approx(4)
    engine = Harness(broker)
    engine._reconcile_protective_orders()
    stop = broker.exchange.requests[-1]
    assert stop["type"] == "market" and stop["price"] is None
    assert stop["params"]["stopLossPrice"] == 90 and stop["amount"] == 0.4
    assert "reduceOnly" not in stop["params"]
    plan = engine._protective_manager().evaluate(symbol=SYMBOL, position_qty=0.4, desired_stop=90,
                                               open_protective_orders=engine._venue_protective_orders())
    assert plan.state is ProtectiveState.ARMED


def test_rejected_stop_reduces_real_position_with_reference_price(broker):
    enter(broker)
    broker.exchange.reject_stop = True
    engine = Harness(broker)
    engine._reconcile_protective_orders()
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert engine._operational_state == "DEGRADED"
    assert len(broker.close_events) == 1
    assert broker.close_events[0].opening_strategy_id == "TrendBreakout"
    assert broker.close_events[0].exit_reason == "unprotected_flatten"
    assert broker.portfolio.cash == 10000


def test_same_bar_replacements_have_distinct_durable_ids(broker):
    enter(broker)
    engine = Harness(broker)
    for stop in (90, 92, 94):
        engine.strategies["TrendBreakout"].context[SYMBOL]["stop_loss"] = stop
        engine._reconcile_protective_orders()
    ids = [req["params"]["clientOrderId"] for req in broker.exchange.requests]
    assert len(ids) == len(set(ids)) == 4
    assert len(engine._venue_protective_orders()) == 1
    engine._reconcile_protective_orders()
    assert len(broker.exchange.requests) == 4


def test_cancel_timeout_cannot_send_overlapping_exit(broker):
    enter(broker)
    engine = Harness(broker)
    engine._reconcile_protective_orders()
    broker.exchange.cancel_timeout = True
    engine.strategies["TrendBreakout"].context[SYMBOL]["stop_loss"] = 92
    broker.retry_max_attempts = 1
    engine._reconcile_protective_orders()
    assert len(broker.exchange.requests) == 2
    assert engine._operational_state == "DEGRADED"
    assert broker.has_unresolved_unknown()
    result = broker.submit_order(SYMBOL, "sell", 1, reference_price=100, strategy_id="Emergency", sequence=7)
    assert not result.accepted
    assert len(broker.exchange.requests) == 2


def test_fill_replay_does_not_apply_cash_twice(broker):
    result = enter(broker)
    before = broker.portfolio.cash
    broker.reconcile_order(result.client_order_id)
    broker._rebuild_fill_projection()
    broker._rebuild_fill_projection()
    assert broker.portfolio.cash == before
    assert sum(lot.qty_open for lot in broker.portfolio.open_lots(SYMBOL)) == 1


def test_normal_exit_cancels_protection_and_accounts_for_actual_close(broker):
    enter(broker)
    Harness(broker)._reconcile_protective_orders()
    result = broker.submit_order(SYMBOL, "sell", 1, reference_price=100, exit_reason="signal")
    assert result.status is OrderStatus.FILLED
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert len(broker.close_events) == 1


def test_partial_risk_exit_retry_reuses_pending_order_then_observes_fill(broker):
    enter(broker)
    engine = Harness(broker)
    engine._reconcile_protective_orders()
    broker.exchange.exit_fraction = 0.5
    assert not engine._submit_risk_exit(SYMBOL, "DrawdownReduce", 0.5, "risk-1", 100)
    request_count = len(broker.exchange.requests)
    assert not engine._submit_risk_exit(SYMBOL, "DrawdownReduce", 0.5, "risk-1", 100)
    assert len(broker.exchange.requests) == request_count
    pending = broker.order_store.list_non_terminal()[0]
    payload = broker.exchange.orders[pending["exchange_order_id"]]
    payload.update(status="closed", filled=0.5, remaining=0)
    payload["trades"].append({"id": "second-fill", "amount": 0.25, "price": 100,
                              "datetime": NOW.isoformat(), "fee": {"cost": 0, "currency": "USDT"}})
    broker.exchange.qty -= 0.25
    broker.exchange.cash += 25
    assert engine._submit_risk_exit(SYMBOL, "DrawdownReduce", 0.5, "risk-1", 100)
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0.5
    assert len(broker.exchange.requests) == request_count
    assert sum(e.qty for e in broker.close_events) == 0.5


@pytest.mark.parametrize("reason", ["DailyLossLimit", "AccountLiquidation", "GapRiskResize"])
def test_named_risk_action_reaches_target_and_replay_is_noop(broker, reason):
    enter(broker)
    engine = Harness(broker)
    engine._reconcile_protective_orders()
    assert engine._submit_risk_exit(SYMBOL, reason, 0, reason, 100)
    assert engine._submit_risk_exit(SYMBOL, reason, 0, reason, 100)
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert len(broker.close_events) == 1
    assert broker.close_events[0].exit_reason == reason


def test_account_flat_without_closing_fills_enters_recovery(broker):
    enter(broker)
    broker.exchange.qty = 0
    assert broker.sync()
    assert f"unowned_position:{SYMBOL}" in broker.projection_issues


def test_margin_identity_requires_liabilities_and_reconciles_net_assets(broker):
    balance = {"info": {"userAssets": [
        {"asset": "USDT", "free": "10000", "locked": "0", "borrowed": "1000", "interest": "2", "netAsset": "8998"}]}}
    cash, positions = broker._sync_margin_account(balance)
    assert cash == 8998 and not positions
    balance["info"]["userAssets"][0]["netAsset"] = "9000"
    with pytest.raises(ValueError, match="identity"):
        broker._sync_margin_account(balance)


def test_base_currency_fee_reduces_inventory_and_is_not_lost_from_lot_cost(broker):
    broker.exchange.entry_fraction = 0
    result = enter(broker, fraction=0)
    broker.order_store.add_fill(FillRecord(fill_id="fee-fill", client_order_id=result.client_order_id,
        exchange_order_id=result.exchange_order_id, qty=1, price=100, fee=.01,
        fee_currency="BTC", timestamp=NOW.isoformat(), payload={"id": "fee-fill"}, symbol=SYMBOL, side="buy"))
    broker.order_store.update(result.client_order_id, filled_qty=1, remaining_qty=0)
    broker.exchange.qty = .99
    broker.exchange.cash = 9900
    assert broker.sync()
    lot = broker.portfolio.open_lots(SYMBOL)[0]
    assert lot.qty_open == pytest.approx(.99)
    assert lot.entry_cost_total == pytest.approx(1)
