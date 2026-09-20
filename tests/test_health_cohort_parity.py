"""Health observation parity and conservative, recoverable checkpoint migration."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.protective_stops import ResidentStopSimulator
from core.broker import Broker
from core.domain import OrderStatus
from core.live_broker.fill_projection import replay_fill_projection
from core.portfolio import Portfolio
from core.runtime import MarketDataSlice
from core.strategy_health import (
    HealthStatus, StrategyHealthMachine, StrategyHealthPolicy, health_cohort_id,
)
from live_trading.tick_orchestrator import TickOrchestratorMixin
from strategies.trend_breakout import TrendBreakoutStrategy


def _ingest(machine, index, *, timestamp="2024-01-02T00:00:00Z", reason="protective_stop",
            action=None, pnl=-10.0):
    return machine.ingest_close(
        close_event_id=f"fill-{index}", symbol=f"SYM{index}/USDT", realized_pnl=pnl,
        initial_risk=20.0, timestamp=timestamp, risk_action_id=action, exit_reason=reason,
    )


class _Store:
    def __init__(self, values=None, fail_at=None):
        self.values = deepcopy(values or {})
        self.writes = []
        self.fail_at = fail_at

    def get(self, key):
        return deepcopy(self.values.get(key))

    def set(self, key, value):
        if self.fail_at == len(self.writes):
            raise OSError("simulated interruption")
        self.writes.append(key)
        self.values[key] = deepcopy(value)


class _FilledVenue:
    """Venue facts for the actual live intent and fill-projection path."""
    def __init__(self, portfolio):
        self.portfolio = portfolio
        self.order_store = self
        self.records = []
        self.fills = {}
        self.close_events = []
        self.sequences = {}

    def record(self, symbol, side, qty, price, timestamp, intent):
        key = f"venue-{len(self.records)}"
        self.records.append({
            "client_order_id": key, "symbol": symbol, "side": side,
            "order_type": intent.get("order_type", "market"), "price": price,
            "requested_qty": qty, "filled_qty": qty, "remaining_qty": 0.0,
            "status": OrderStatus.FILLED.value, "intent": deepcopy(intent),
        })
        self.fills[key] = [{
            "fill_id": f"trade-{key}", "qty": qty, "price": price, "fee": 0.0,
            "fee_currency": "USDT", "timestamp": timestamp,
            "payload": {"id": str(len(self.records))},
        }]

    def list_non_terminal(self):
        return []

    def list_all(self):
        return list(self.records)

    def list_with_fills(self):
        return self.records

    def fills_for(self, order_id):
        return self.fills[order_id]

    def action_sequence(self, key):
        return self.sequences.setdefault(key, len(self.sequences) + 1)

    def submit_order(self, symbol, side, qty, **kwargs):
        self.record(symbol, side, qty, kwargs["trigger_price"],
                    kwargs["timestamp"].isoformat(), kwargs)
        _, self.close_events, issues = replay_fill_projection(self, "USDT")
        assert issues == []
        return SimpleNamespace(accepted=True, status=OrderStatus.FILLED)


class _LiveStops(TickOrchestratorMixin):
    def __init__(self, broker, strategy):
        self.broker = broker
        self.strategies = {strategy.name: strategy}
        self._operational_state = "RUNNING"

    def _now(self):
        return datetime(2024, 1, 2, tzinfo=timezone.utc)

    def _alert(self, *args):
        raise AssertionError(args)


def test_real_backtest_stops_and_live_intent_fill_projection_have_same_health():
    symbols = ("BTC/USDT", "ETH/USDT", "SOL/USDT")
    backtest_strategy, live_strategy = TrendBreakoutStrategy(), TrendBreakoutStrategy()
    backtest = Broker(Portfolio(initial_capital=100_000), commission_rate=0, slippage=0)
    live = _FilledVenue(Portfolio(initial_capital=100_000))
    bars = {symbol: pd.Series({"open": 100., "high": 102., "low": 85.,
                              "close": 95., "volume": 1_000_000.},
                             name=pd.Timestamp("2024-01-02T00:00:00Z"))
            for symbol in symbols}
    for symbol in symbols:
        backtest.submit_order(symbol, "buy", 1, price=100, strategy_id="TrendBreakout",
                              stop_loss=90, timestamp=pd.Timestamp("2024-01-01T00:00:00Z"))
        live.portfolio.update_position(symbol, 1, 100, strategy_id="TrendBreakout",
                                       order_id=f"entry-{symbol}", stop_price=90)
        live.record(symbol, "buy", 1, 100, "2024-01-01T00:00:00+00:00",
                    {"strategy_id": "TrendBreakout", "initial_stop": 90})
        backtest_strategy.context[symbol] = {"effective_stop": 90}
        live_strategy.context[symbol] = {"effective_stop": 90}
    backtest.process_orders(bars)
    ResidentStopSimulator(backtest, {"TrendBreakout": backtest_strategy}).step(
        MarketDataSlice(pd.Timestamp("2024-01-02T00:00:00Z"), bars, {}), bar_index=1,
    )
    _LiveStops(live, live_strategy)._reconcile_protective_orders()
    assert len(backtest.close_events) == len(live.close_events) == 3
    assert all(event.risk_action_id is None for event in backtest.close_events)
    source_ids = {event.risk_action_id for event in live.close_events}
    assert len(source_ids) == 3 and None not in source_ids
    for strategy, broker in ((backtest_strategy, backtest), (live_strategy, live)):
        for symbol in symbols:
            strategy._consume_execution_trades(symbol, 1, broker.portfolio, broker)
            strategy._consume_execution_trades(symbol, 2, broker.portfolio, broker)
        strategy.health.evaluate("2024-01-02T12:00:00Z")
        assert len(strategy.health.cohorts) == 1
        assert strategy.health.status is HealthStatus.ACTIVE
        assert strategy.health.risk_multiplier == 1
    left, right = backtest_strategy.health.cohorts[0], live_strategy.health.cohorts[0]
    assert left.cohort_id == right.cohort_id
    assert left.net_pnl == pytest.approx(right.net_pnl)
    assert left.initial_risk == right.initial_risk == 30
    assert left.trade_count == right.trade_count == 3
    assert set(right.source_risk_action_ids) == source_ids
    assert source_ids == {event.risk_action_id for event in live.close_events}


def test_three_days_remain_three_losses_and_utc_dates_are_used():
    machine = StrategyHealthMachine("TrendBreakout")
    for day in (1, 2, 3):
        _ingest(machine, day, timestamp=f"2024-01-{day + 1:02d}T01:00:00+08:00",
                action=f"order-{day}")
    assert [cohort.exit_session for cohort in machine.cohorts] == [
        "2024-01-01", "2024-01-02", "2024-01-03",
    ]
    assert machine.evaluate("2024-01-04") is HealthStatus.COOLDOWN


def test_partials_replacements_and_replayed_fills_preserve_raw_order_audit():
    machine = StrategyHealthMachine("TrendBreakout")
    for index, action in ((1, "old-stop"), (2, "old-stop"), (3, "replacement")):
        _ingest(machine, index, action=action)
    _ingest(machine, 2, action="old-stop")
    restored = StrategyHealthMachine("TrendBreakout")
    restored.load(machine.to_dict())
    assert _ingest(restored, 3, action="replacement") is None
    cohort = restored.cohorts[0]
    assert cohort.trade_count == 3 and cohort.net_pnl == -30 and cohort.initial_risk == 60
    assert cohort.source_risk_action_ids == ["old-stop", "replacement"]


@pytest.mark.parametrize("reason,controller", [
    ("DrawdownReduce", "account_risk"), ("StateSwitch", "router"), ("EndOfBacktest", "system"),
])
def test_non_strategy_controller_action_identity_unchanged(reason, controller):
    machine = StrategyHealthMachine("TrendBreakout")
    for index, action in enumerate(("action-A", "action-A", "action-B")):
        _ingest(machine, index, reason=reason, action=action)
    assert len(machine.cohorts) == 2
    assert machine.cohorts[0].cohort_id == health_cohort_id(
        "TrendBreakout", "2024-01-02", controller, "action-A",
    )


def _legacy_checkpoint(status="cooldown", schema="strategy_health/v2"):
    machine = StrategyHealthMachine("TrendBreakout")
    rows = []
    for index in range(3):
        one = StrategyHealthMachine("TrendBreakout")
        cohort = _ingest(one, index, action=f"stop-{index}").to_dict()
        cohort["cohort_id"] = f"TrendBreakout:2024-01-02:strategy:stop-{index}"
        cohort["risk_action_id"] = f"stop-{index}"
        cohort.pop("source_risk_action_ids")
        rows.append(cohort)
    data = machine.to_dict()
    data.pop("cohort_key_version")
    data.update({
        "schema": schema, "status": status, "cohorts": rows,
        "consumed_close_event_ids": ["fill-0", "fill-1", "fill-2", "pruned-fill"],
        "streak_baseline_cohort_ids": [rows[1]["cohort_id"], "pruned-cohort"],
        "trigger_event_id": rows[2]["cohort_id"],
        "cooldown_started_at": "2024-01-02T00:00:00+00:00",
        "cooldown_until": "2024-02-01T00:00:00+00:00",
        "probation_started_at": "2024-01-02T00:00:00+00:00",
        "failed_probation_cycles": 2, "risk_multiplier": 0.0,
        "manual_lock_reason": "operator lock" if status == "manual_lock" else None,
        "transitions": [{"at": "2024-01-02", "to": status, "reason": "old-history"}],
        "migration_audit": [{"migration": "older-migration"}],
        "legacy_migration_history": [{"preserve": "opaque prior audit"}],
    })
    return data


@pytest.mark.parametrize("schema", ["strategy_health/v2", "strategy_health/v3"])
@pytest.mark.parametrize("status", ["cooldown", "probation", "manual_lock"])
def test_migration_conserves_money_and_preserves_controls_and_old_evidence(schema, status):
    data = _legacy_checkpoint(status, schema)
    untouched = deepcopy(data)
    machine = StrategyHealthMachine("TrendBreakout")
    machine.load(data)
    assert data == untouched
    assert len(machine.cohorts) == 1
    cohort = machine.cohorts[0]
    assert (cohort.net_pnl, cohort.initial_risk, cohort.trade_count, cohort.r) == (-30, 60, 3, -0.5)
    assert cohort.source_risk_action_ids == ["stop-0", "stop-1", "stop-2"]
    saved = machine.to_dict()
    for key in ("status", "cooldown_started_at", "cooldown_until", "failed_probation_cycles",
                "manual_lock_reason", "transitions", "consumed_close_event_ids",
                "legacy_migration_history"):
        assert saved[key] == data[key]
    assert saved["streak_baseline_cohort_ids"] == sorted([cohort.cohort_id, "pruned-cohort"])
    assert saved["trigger_event_id"] == cohort.cohort_id
    assert machine.probation_closed_cohorts == 0
    assert saved["migration_audit"][0] == data["migration_audit"][0]
    assert saved["cohort_key_version"] == 2
    restored = StrategyHealthMachine("TrendBreakout")
    restored.load(saved)
    assert restored.to_dict() == saved
    assert _ingest(restored, 1, action="stop-1") is None


def test_lifecycle_migration_establishes_baseline_before_key_merging():
    policy = StrategyHealthPolicy(
        repeated_failure_action="extended_cooldown", unified_recovery=True,
        recovery_stages=(0.1, 0.25, 0.5, 1.0),
    )
    data = _legacy_checkpoint("probation")
    data["risk_multiplier"] = 0.25
    data["streak_baseline_cohort_ids"] = []
    machine = StrategyHealthMachine("TrendBreakout", policy)
    machine.load(data)
    assert machine.risk_multiplier == 0.25
    assert machine.probation_started_at is None
    assert machine._streak_baseline_cohort_ids == {machine.cohorts[0].cohort_id}
    assert machine.trigger_reason == "legacy_probation_migrated_new_evidence_required"


@pytest.mark.parametrize("fail_at", [0, 1, 2])
def test_migration_persist_can_restart_at_every_write_boundary(fail_at):
    original = _legacy_checkpoint()
    key = "strategy_health:TrendBreakout"
    store = _Store({key: original}, fail_at=fail_at)
    machine = StrategyHealthMachine("TrendBreakout")
    machine.load(store.get(key))
    with pytest.raises(OSError):
        machine.persist(store, key)
    assert store.get(key) == original
    store.fail_at = None
    restarted = StrategyHealthMachine("TrendBreakout")
    restarted.load(store.get(key))
    restarted.persist(store, key)
    archive_keys = [item for item in store.values if item != key]
    assert len(archive_keys) == 2
    backup = next(item for item in archive_keys if item.endswith(":backup"))
    assert store.get(backup) == original
    assert store.writes.index(backup) < store.writes.index(key)
    saved = store.get(key)
    restarted.load(saved)
    restarted.persist(store, key)
    assert store.get(key) == saved
    assert len(store.values) == 3


def test_strategy_binding_uses_durable_migration_backup():
    key = "strategy_health:TrendBreakout"
    original = _legacy_checkpoint("manual_lock")
    store = _Store({key: original})
    strategy = TrendBreakoutStrategy()
    strategy.bind_state_store(store)
    assert store.writes == []
    assert strategy.check_health("2025-01-01") is False
    assert store.get(key)["status"] == "manual_lock"
    assert len(store.values) == 3


def test_unknown_cohort_version_does_not_reset_loaded_machine():
    machine = StrategyHealthMachine("TrendBreakout")
    machine.manual_lock("stay locked")
    data = _legacy_checkpoint()
    data["cohort_key_version"] = 999
    with pytest.raises(ValueError, match="Unsupported"):
        machine.load(data)
    assert machine.status is HealthStatus.MANUAL_LOCK
