"""Local SYS acceptance produces reviewable facts, not live readiness claims."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import random
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from backtest.execution_adapter import SimulatedExecutionAdapter
from composition.factory import build_strategy_registry
from config.config import config
from core.broker import Broker
from core.domain import OrderIntent, OrderStatus
from core.events import EventCodec, Signal, TradingEventPipeline
from core.live_broker import LiveBroker
from core.market_data import HistoricalMarketDataAdapter, LiveMarketDataAdapter
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.reproducibility import canonical_json, deterministic_result_digest
from core.risk import RiskManager
from core.runtime import EventProcessor
from core.runtime_identity import RuntimeIdentity
from core.sqlite_backup import SQLiteSnapshotManager, restore_snapshot
from core.sqlite_utils import DatabaseIntegrityError
from core.state import MarketState
from core.state_store_v2 import StateStore
from core.strategy_health import HealthStatus
from live_trading.execution_adapter import RecordedExecutionAdapter
from router.router import Router
from tests.engine_baseline_harness import build_synthetic_data_map
from tests.test_signal_observation import DailyRaw, prices
from tests.test_revalidation_execution import broker as live_broker  # noqa: F401


def artifact(tmp_path, record_property, name, payload):
    path = tmp_path / name
    path.write_text(canonical_json(payload), encoding="utf-8")
    record_property("artifact", str(path))


def test_corrupt_restore_requires_identity_and_preserves_original(tmp_path, record_property):
    identity = RuntimeIdentity("fixture", "sandbox", "one", "spot")
    wrong = RuntimeIdentity("fixture", "sandbox", "two", "spot")
    path = tmp_path / "state.db"
    state = StateStore(str(path), identity=identity)
    state.set("watermark", 27)
    state.close()
    snapshot = SQLiteSnapshotManager(str(path)).create_snapshot()
    broken = b"offline deliberately damaged database"
    path.write_bytes(broken)
    with pytest.raises(DatabaseIntegrityError, match="explicit expected"):
        restore_snapshot(str(snapshot), str(path))
    with pytest.raises(DatabaseIntegrityError, match="identity mismatch"):
        restore_snapshot(str(snapshot), str(path), expected_identity=wrong)
    assert path.read_bytes() == broken
    restore_snapshot(str(snapshot), str(path), expected_identity=identity)
    state = StateStore(str(path), identity=identity)
    assert state.get("watermark") == 27
    state.close()
    copies = list(tmp_path.glob("state.db.corrupt.*"))
    assert len(copies) == 1 and copies[0].read_bytes() == broken
    artifact(tmp_path, record_property, "identity_recovery.json", {
        "missing_identity": "rejected", "wrong_identity": "rejected",
        "correct_identity": "restored", "watermark": 27, "corrupt_bytes_preserved": True,
    })


def test_failed_restore_replace_leaves_live_target_and_other_temp_unchanged(tmp_path):
    path = tmp_path / "state.db"
    state = StateStore(str(path))
    state.set("watermark", 1)
    state.close()
    snapshot = SQLiteSnapshotManager(str(path)).create_snapshot()
    state = StateStore(str(path))
    state.set("watermark", 2)
    state.close()
    other = tmp_path / ".state.db.other.restore"
    other.write_text("another recovery owns this")
    with patch("core.sqlite_backup.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            restore_snapshot(str(snapshot), str(path))
    state = StateStore(str(path))
    assert state.get("watermark") == 2
    state.close()
    assert list(tmp_path.glob("*.restore")) == [other]


def test_offline_recovery_cli_meets_frozen_local_rto_rpo(tmp_path, record_property):
    from scripts.run_offline_recovery_drill import run_drill
    report = run_drill(tmp_path / "drill", rto_seconds=30)
    assert report["passed"] and report["rpo_lost_pre_backup_records"] == 0
    assert report["before"] == report["after"]
    artifact(tmp_path, record_property, "recovery_drill.json", report)


@pytest.mark.parametrize("health_state", ["active", "manual_lock", "cooldown", "probation"])
def test_shadow_switch_does_not_change_authoritative_facts_across_health_states(tmp_path, record_property, health_state):
    data = build_synthetic_data_map(bars=100)
    def run(enabled):
        strategies = build_strategy_registry(config)
        for strategy in strategies.values():
            health = getattr(strategy, "health", None)
            if health is None:
                continue
            original = strategy.reset_runtime_state
            def reset(original=original, strategy=strategy):
                original()
                health = strategy.health
                if health_state == "manual_lock":
                    health.manual_lock("offline_operator_lock", at="2023-12-31T00:00:00Z")
                elif health_state == "cooldown":
                    health._enter_cooldown(pd.Timestamp("2023-12-31T00:00:00Z").to_pydatetime(), reason="offline_fixture")
                elif health_state == "probation":
                    health._transition(HealthStatus.PROBATION, pd.Timestamp("2023-12-31T00:00:00Z").to_pydatetime(), reason="offline_fixture")
            strategy.reset_runtime_state = reset
        random.seed(812)
        return BacktestEngine(signal_observation={"enabled": enabled, "horizons": [1, 3], "ghost_horizon": 3}).run(
            data, strategies=strategies, routing_log_enabled=False)
    off, on = run(False), run(True)
    assert deterministic_result_digest(off) == deterministic_result_digest(on)
    assert off["strategy_health"] == on["strategy_health"]
    assert off["allocation_audit"] == on["allocation_audit"]
    assert canonical_json(off["trades"]) == canonical_json(on["trades"])
    if health_state == "manual_lock":
        assert all(row["status"] == "manual_lock" for row in on["strategy_health"].values())
    artifact(tmp_path, record_property, f"shadow_{health_state}.json", {
        "initial_health": health_state, "official_digest_off": deterministic_result_digest(off),
        "official_digest_on": deterministic_result_digest(on),
        "trades_off": off["trades"], "trades_on": on["trades"],
        "equity_off": off["equity_curve"].reset_index().to_dict("records"),
        "equity_on": on["equity_curve"].reset_index().to_dict("records"),
        "health_off": off["strategy_health"], "health_on": on["strategy_health"],
        "allocation_off": off["allocation_audit"], "allocation_on": on["allocation_audit"],
        "shadow_candidate_count": len(on["signal_observation"]["candidates"]),
    })


def test_later_same_cohort_fills_do_not_rewrite_past_health_verdict(tmp_path, record_property):
    from tests.test_roadmap_admission_recovery import recovered_health
    machine, now = recovered_health()
    verdicts = deepcopy(machine.transitions)
    last = machine.cohorts[-1]
    machine.ingest_close(close_event_id="later-same-session", symbol=last.symbols[0],
        realized_pnl=-100, initial_risk=1, timestamp=last.closed_at)
    assert machine.transitions == verdicts
    machine.evaluate(now)
    assert machine.status is HealthStatus.ACTIVE
    artifact(tmp_path, record_property, "immutable_health_verdicts.json", {
        "before": verdicts, "after": machine.transitions, "raw_cohort_after": last.to_dict(),
        "past_verdict_unchanged": True,
    })


@pytest.mark.parametrize("account_mode", ["spot", "spot_margin"])
def test_actual_router_and_execution_adapters_produce_same_canonical_intent(tmp_path, record_property, account_mode):
    frame = prices(4).tz_localize("UTC")
    timestamp = frame.index[0].to_pydatetime()
    symbols = ["BTC/USDT", "ETH/USDT"]
    historical = list(HistoricalMarketDataAdapter({symbol: frame for symbol in symbols}, timeframe="1d").stream())
    fetcher = MagicMock()
    fetcher.fetch_ccxt.return_value = frame.copy()
    market = LiveMarketDataAdapter(symbols, fetcher, timeframe="1d", close_grace_seconds=0)
    live_events = market.poll(datetime(2020, 1, 6, tzinfo=timezone.utc))
    exchange = MagicMock()
    exchange.fetch_balance.return_value = {"free": {"USDT": 10000}, "total": {"USDT": 10000}}
    def accept(**request):
        return {"id": request["params"]["clientOrderId"], "status": "open", "amount": request["amount"], "filled": 0, "remaining": request["amount"]}
    exchange.create_order.side_effect = accept
    rows = []
    for mode, events in (("backtest", historical), ("live", live_events)):
        pipeline = TradingEventPipeline(run_id="shared-fixture", clock=lambda: timestamp)
        if mode == "backtest":
            broker = Broker(Portfolio(10000, account_mode=account_mode), event_pipeline=pipeline,
                exchange_id="binance", account_id="sandbox", timeframe="1d", commission_rate=0, slippage=0)
            execution = SimulatedExecutionAdapter(broker)
        else:
            with patch("core.live_broker.ccxt.binance", return_value=exchange):
                broker = LiveBroker(Portfolio(10000, account_mode=account_mode), order_store=OrderStore(str(tmp_path / "orders.db")),
                    event_pipeline=pipeline, clock=lambda: timestamp, account_id="sandbox",
                    market_type="margin" if account_mode == "spot_margin" else "spot")
            execution = RecordedExecutionAdapter(broker)
        strategy = DailyRaw()
        router = Router({strategy.name: strategy}, {state.name: strategy.name for state in MarketState})
        machine = MagicMock()
        machine.get_state.return_value = MarketState.TREND_UP
        risk = RiskManager()
        processor = EventProcessor(portfolio=broker.portfolio, execution=execution, risk_manager=risk,
            state_machine=machine, router=router, allocator=router.allocator, entry_audit_enabled=True)
        for event in events:
            if mode == "live":
                broker.set_bar_context("1d", event.timestamp)
            processor.process(event, execute_market_event=False)
        intents = [asdict(event.payload) for event in pipeline.events if isinstance(event.payload, OrderIntent)]
        signals = [dict(event.payload) for event in pipeline.events if isinstance(event.payload, Signal)]
        events_by_id = {str(event.event_id): event for event in pipeline.events}
        causal_chains = []
        for event in pipeline.events:
            if not isinstance(event.payload, OrderIntent):
                continue
            command = event.payload
            signal = events_by_id[command.signal_id]
            assert signal.event_type == "signal"
            assert command.causation_id == str(signal.event_id)
            reservation = events_by_id[str(event.causation_id)]
            decision = events_by_id[str(reservation.causation_id)]
            assert reservation.event_type == "risk_reservation"
            assert decision.event_type == "risk_decision"
            assert str(decision.causation_id) == command.signal_id
            order_events = [row for row in pipeline.events if row.event_type == "order"
                and row.payload.client_order_id == command.client_order_id]
            assert order_events and str(order_events[0].causation_id) == str(event.event_id)
            causal_chains.append({"signal": command.signal_id, "decision": str(decision.event_id),
                "reservation": str(reservation.event_id), "intent": str(event.event_id),
                "orders": [str(row.event_id) for row in order_events], "logical_order_id": command.client_order_id})
        rows.append({"mode": mode, "intents": intents, "signals": signals,
                     "causal_chains": causal_chains, "entry_audit": processor.entry_audit})
        if mode == "live":
            encoded = [EventCodec.encode(event) for event in pipeline.events]
            broker.close()
    assert rows[0]["intents"] and rows[0]["intents"] == rows[1]["intents"]
    assert canonical_json(rows[0]["signals"]) == canonical_json(rows[1]["signals"])
    replay = RecordedExecutionAdapter(portfolio=Portfolio(10000))
    for _ in range(100):
        replay.replay([EventCodec.decode(document) for document in encoded])
    assert len(replay.events) == len(encoded)
    replay_intents = [asdict(event.payload) for event in replay.events if isinstance(event.payload, OrderIntent)]
    assert replay_intents == rows[0]["intents"]
    # Execution timing is intentionally frozen at accepted/unfilled in both
    # boundaries, so position facts cannot legitimately diverge the signals.
    def business_audit(row):
        return [{key: value for key, value in item.items() if key != "submission_status"}
                for item in row["entry_audit"]]
    assert canonical_json(business_audit(rows[0])) == canonical_json(business_audit(rows[1]))
    artifact(tmp_path, record_property, "canonical_mode_comparison.json", {
        "account_mode": account_mode, "rows": rows, "replay_intents": replay_intents,
        "replay_repeats": 100, "event_documents": encoded, "allowed_differences": {
            "entry_audit.submission_status": "simulator CREATED is queued; venue ACCEPTED acknowledges the request",
        },
    })


@pytest.mark.parametrize("legacy_time", ["2020-01-01T00:00:00", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00Z"])
def test_canonical_submission_reuses_legacy_payload_across_restart_and_100_repeats(tmp_path, record_property, legacy_time):
    legacy = OrderIntent("binance", "sandbox", "BTC/USDT", "1d", legacy_time,
        "Test", "buy", 0, 2, price=100, reference_price=100,
        time_in_force=None, initial_stop=90, exit_reason="signal", approved_risk_amount=20)
    path = tmp_path / "orders.db"
    store = OrderStore(str(path))
    store.create_intent(legacy, "2020-01-01T00:00:00Z")
    store.mark_submission_attempted(legacy.client_order_id, "2020-01-01T00:00:00Z")
    store.transition(legacy.client_order_id, OrderStatus.ACCEPTED, "2020-01-01T00:00:00Z", exchange_order_id="old-exchange")
    original = store.get(legacy.client_order_id)["intent"]
    store.close()
    exchange = MagicMock()
    exchange.fetch_order.return_value = {"id": "old-exchange", "status": "open", "amount": 2, "filled": 0, "remaining": 2}
    with patch("core.live_broker.ccxt.binance", return_value=exchange):
        broker = LiveBroker(Portfolio(10000), order_store=OrderStore(str(path)), account_id="sandbox",
            clock=lambda: datetime(2020, 1, 1, tzinfo=timezone.utc))
    broker.set_bar_context("1d", "2020-01-01T00:00:00Z")
    results = [broker.submit_order("BTC/USDT", "buy", 9, price=100, strategy_id="Test",
        signal_id="later-signal", causation_id="later-signal") for _ in range(100)]
    assert {result.client_order_id for result in results} == {legacy.client_order_id}
    assert all(result.requested_qty == 2 for result in results)
    assert broker.order_store.get(legacy.client_order_id)["intent"] == original
    assert len(broker.order_store.list_all()) == 1
    exchange.create_order.assert_not_called()
    broker.close()
    artifact(tmp_path, record_property, "legacy_id_replay.json", {
        "legacy_payload": original, "repeats": 100, "logical_orders": 1,
        "new_exchange_requests": 0, "frozen_requested_qty": 2,
    })


def test_conflicting_legacy_time_identities_fail_closed(tmp_path):
    legacy = OrderIntent("binance", "sandbox", "BTC/USDT", "1d", "2020-01-01T00:00:00",
        "Test", "buy", 0, 2, price=100)
    store = OrderStore(str(tmp_path / "orders.db"))
    for intent in (legacy, replace(legacy, bar_time="2020-01-01T00:00:00Z")):
        store.create_intent(intent, "2020-01-01T00:00:00Z")
    exchange = MagicMock()
    with patch("core.live_broker.ccxt.binance", return_value=exchange):
        broker = LiveBroker(Portfolio(10000), order_store=store, account_id="sandbox")
    broker.set_bar_context("1d", "2020-01-01T00:00:00Z")
    try:
        with pytest.raises(ValueError, match="ambiguous legacy"):
            broker.submit_order("BTC/USDT", "buy", 2, price=100, strategy_id="Test")
        exchange.create_order.assert_not_called()
        assert len(store.list_all()) == 2
    finally:
        broker.close()


@pytest.mark.parametrize("boundary", [
    "intent_committed_before_attempt", "attempt_committed_before_send",
    "venue_accept_before_response", "response_before_persist",
    "fill_committed_before_status", "partial_status_committed",
])
def test_order_persistence_boundary_matrix_recovers_without_extra_risk(tmp_path, record_property, boundary):
    class ProcessCrash(BaseException):
        pass
    now = datetime(2020, 1, 1, tzinfo=timezone.utc)
    path = tmp_path / "orders.db"
    exchange = MagicMock()
    remote = {}
    timeline = []
    partial = boundary in {"fill_committed_before_status", "partial_status_committed"}
    def send(**request):
        payload = {"id": "venue-one", "clientOrderId": request["params"]["clientOrderId"],
            "status": "open", "amount": 2, "filled": .4 if partial else 0,
            "remaining": 1.6 if partial else 2, "average": 100,
            "trades": [{"id": "fill-one", "amount": .4, "price": 100, "datetime": now.isoformat(),
                        "fee": {"cost": 0, "currency": "USDT"}}] if partial else []}
        remote.update(payload)
        timeline.append("venue_accepted")
        if boundary == "venue_accept_before_response":
            raise ProcessCrash()
        return dict(payload)
    exchange.create_order.side_effect = send
    exchange.fetch_order.side_effect = lambda *args, **kwargs: dict(remote) or None
    exchange.fetch_order_by_client_order_id.side_effect = lambda *args, **kwargs: dict(remote) or None
    exchange.fetch_open_orders.return_value = []
    exchange.fetch_closed_orders.return_value = []
    exchange.fetch_balance.return_value = {"free": {"USDT": 9960}, "total": {"USDT": 9960, "BTC": .4 if partial else 0}}
    def open_broker():
        with patch("core.live_broker.ccxt.binance", return_value=exchange):
            value = LiveBroker(Portfolio(10000), order_store=OrderStore(str(path)),
                account_id="sandbox", clock=lambda: now)
        value.set_bar_context("1d", now)
        return value
    broker = open_broker()
    if boundary == "intent_committed_before_attempt":
        owner, method = broker.order_store, "create_intent"
    elif boundary == "attempt_committed_before_send":
        owner, method = broker.order_store, "mark_submission_attempted"
    elif boundary == "fill_committed_before_status":
        owner, method = broker.order_store, "add_fill"
    else:
        owner, method = broker, "_persist_exchange_payload"
    original = getattr(owner, method)
    def interrupt(*args, **kwargs):
        if boundary != "response_before_persist":
            original(*args, **kwargs)
        timeline.append(boundary)
        raise ProcessCrash()
    with patch.object(owner, method, side_effect=interrupt):
        with pytest.raises(ProcessCrash):
            broker.submit_order("BTC/USDT", "buy", 2, price=100, stop_loss=90,
                approved_risk_amount=20, strategy_id="Test")
    before = broker.order_store.list_all()
    broker.close()
    timeline.append("restart_fresh_broker_from_database")
    broker = open_broker()
    try:
        for _ in range(100):
            result = broker.submit_order("BTC/USDT", "buy", 2, price=100, stop_loss=90,
                approved_risk_amount=20, strategy_id="Test")
        after = broker.order_store.list_all()
        fills = broker.order_store.fills_for(result.client_order_id)
        assert len(after) == 1
        assert exchange.create_order.call_count == (0 if boundary == "attempt_committed_before_send" else 1)
        assert result.requested_qty == result.filled_qty + result.remaining_qty
        assert sum(fill["qty"] for fill in fills) == (.4 if partial else 0)
        if boundary == "attempt_committed_before_send":
            assert result.status is OrderStatus.UNKNOWN
            assert broker.has_unresolved_unknown()
            assert broker.pending_open_notional()["BTC/USDT"] == 200
        else:
            assert result.status is (OrderStatus.PARTIALLY_FILLED if partial else OrderStatus.ACCEPTED)
        artifact(tmp_path, record_property, f"order_boundary_{boundary}.json", {
            "boundary": boundary, "timeline": timeline, "before_restart": before,
            "after_100_repeats": after, "fills": fills,
            "exchange_create_count": exchange.create_order.call_count,
            "quantity_conserved": True, "logical_order_count": 1,
            "unknown_keeps_risk_reserved": boundary == "attempt_committed_before_send",
        })
    finally:
        broker.close()


def test_resident_management_targets_match_live_through_flat_and_new_epoch(live_broker, tmp_path, record_property):
    from backtest.protective_stops import ResidentStopSimulator
    from core.broker.matching import is_protective_stop
    from tests.test_revalidation_execution import Harness, NOW, SYMBOL
    sim = Broker(Portfolio(10000), commission_rate=0, slippage=0, exchange_id="binance", account_id="sandbox", timeframe="1d")
    strategies = {"TrendBreakout": SimpleNamespace(context={SYMBOL: {"stop_loss": 90}})}
    resident = ResidentStopSimulator(sim, strategies)
    live = Harness(live_broker)
    live.strategies = strategies
    rows = []
    step = 0
    def trade(side, qty):
        nonlocal step
        step += 1
        moment = pd.Timestamp(NOW) + pd.Timedelta(minutes=step)
        sim.submit_order(SYMBOL, side, qty, price=100, timestamp=moment, stop_loss=90 if side == "buy" else 0,
            strategy_id="TrendBreakout", sequence=step)
        bar = pd.Series({"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10000}, name=moment + pd.Timedelta(seconds=1))
        sim.process_orders({SYMBOL: bar}, order_filter=lambda order: not is_protective_stop(order))
        live_broker.submit_order(SYMBOL, side, qty, reference_price=100, stop_loss=90 if side == "buy" else 0,
            strategy_id="TrendBreakout", sequence=step)
        return bar
    def compare(stage, bar):
        resident._sync({SYMBOL: bar}, timestamp=bar.name, bar_index=step)
        live._reconcile_protective_orders()
        def target(order):
            return {key: getattr(order, key) for key in ("symbol", "side", "qty", "stop_price")}
        sim_targets = [target(order) for order in resident._resident_orders()]
        live_targets = [target(order) for order in live._venue_protective_orders()]
        assert sim_targets == live_targets
        assert sim.portfolio.get_position(SYMBOL)["qty"] == live_broker.portfolio.get_position(SYMBOL)["qty"]
        rows.append({"stage": stage, "backtest": sim_targets, "live": live_targets,
            "qty": sim.portfolio.get_position(SYMBOL)["qty"]})
    bar = trade("buy", 1)
    compare("entry_protected", bar)
    strategies["TrendBreakout"].context[SYMBOL]["stop_loss"] = 93
    compare("ratchet", bar)
    compare("partial_exit", trade("sell", .5))
    compare("authoritative_flat", trade("sell", .5))
    strategies["TrendBreakout"].context[SYMBOL]["stop_loss"] = 85
    compare("new_position_epoch", trade("buy", 1))
    assert rows[-1]["live"][0]["stop_price"] == 85
    artifact(tmp_path, record_property, "shared_management_lifecycle.json", {
        "rows": rows, "new_position_does_not_inherit_old_stop": True,
        "execution_differences": "matched historical fill vs explicit offline venue fill; targets compared after facts settle",
    })


@pytest.mark.parametrize("mode", ["spot", "spot_margin"])
@pytest.mark.parametrize("fault", ["none", "missing_order", "duplicate_fill", "fee_delta", "deposit", "missing_price", "stale_price", "extra_position", "extra_business_field"])
def test_full_account_snapshot_injection_matrix(tmp_path, record_property, mode, fault):
    from core.account_reconciliation import reconcile_account_snapshots
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    margin = mode == "spot_margin"
    expected = {
        "captured_at": now.isoformat(), "snapshot_id": "local-projection",
        "identity": {"exchange": "fixture", "environment": "sandbox", "account": "fixture-only",
                     "market_type": mode, "base_currency": "USDT"},
        "cash": {"free": 1200 if margin else 800, "locked": 0, "total": 1200 if margin else 800},
        "positions": [{"record_id": "BTC/USDT", "qty": -2 if margin else 2,
            "mark_price": 90 if margin else 110, "price_source": "fixture-venue-mark",
            "price_at": now.isoformat(), "max_price_age_seconds": 90}],
        "orders": [{"record_id": "approved-order", "requested_qty": 2, "filled_qty": 2, "remaining_qty": 0}],
        "fills": [{"record_id": "venue-fill", "order_id": "approved-order", "qty": 2,
            "fee_base_amount": 0, "fee_currency": "USDT", "fee_conversion_source": "quote_fee"}],
        "cashflows": [], "equity": 1020,
        "capital_bridge": {"initial_capital": 1000, "net_cashflows": 0, "realized_gross_pnl": 0,
                           "unrealized_gross_pnl": 20, "costs": 0, "financing_costs": 0},
    }
    actual = deepcopy(expected)
    actual["snapshot_id"] = "independent-venue-fixture"
    if fault == "missing_order":
        actual["orders"] = []
    elif fault == "duplicate_fill":
        actual["fills"].append(deepcopy(actual["fills"][0]))
    elif fault == "fee_delta":
        actual["fills"][0]["fee_base_amount"] = 1
    elif fault == "deposit":
        actual["cash"]["free"] += 100
        actual["cash"]["total"] += 100
        actual["equity"] += 100
        actual["cashflows"].append({"record_id": "unbooked-deposit", "amount": 100})
    elif fault in {"missing_price", "stale_price"}:
        if fault == "missing_price":
            actual["positions"][0].pop("mark_price")
        else:
            actual["positions"][0]["price_at"] = "2026-09-19T23:58:00Z"
    elif fault == "extra_position":
        actual["positions"].append(dict(actual["positions"][0], record_id="ETH/USDT", qty=1))
    elif fault == "extra_business_field":
        actual["cash"]["unclassified_liability"] = 5
    result = reconcile_account_snapshots(expected, actual, checked_at=now)
    assert result["ok"] is (fault == "none")
    assert result["allows_new_risk"] is (fault == "none")
    if fault == "none":
        assert result["equity_bridges"]["actual"] == {
            "reported_equity": 1020, "cash_plus_net_position_value": 1020,
            "capital_cashflow_pnl_less_costs": 1020}
    artifact(tmp_path, record_property, f"account_{mode}_{fault}.json", {
        "synthetic_fixture_only": True, "mode": mode, "fault": fault,
        "expected": expected, "actual": actual, "report": result,
    })


@pytest.mark.parametrize("balance", [None, {}, {"free": {"USDT": 50}},
    {"total": {"USDT": None}}, {"total": {"USDT": float("nan")}},
    {"total": {"USDT": 50, "BTC": -1}},
    {"free": {"USDT": 40}, "used": {"USDT": 5}, "total": {"USDT": 50}},
])
def test_unknown_cash_facts_do_not_replace_last_verified_account(balance, tmp_path):
    exchange = MagicMock()
    exchange.fetch_balance.return_value = balance
    portfolio = Portfolio(1234)
    portfolio.positions = {"BTC/USDT": {"qty": 1, "avg_price": 100}}
    with patch("core.live_broker.ccxt.binance", return_value=exchange):
        broker = LiveBroker(portfolio, order_store=OrderStore(str(tmp_path / "orders.db")))
    broker.retry_max_attempts = 1
    try:
        assert not broker.sync()
        assert portfolio.cash == 1234 and portfolio.positions["BTC/USDT"]["qty"] == 1
        assert broker.last_account_sync_at is None
    finally:
        broker.close()


@pytest.mark.parametrize("response", [None, {}, "unavailable"])
def test_unknown_derivative_position_response_is_not_authoritative_flat(tmp_path, response):
    exchange = MagicMock()
    exchange.fetch_balance.return_value = {"total": {"USDT": 1234}}
    exchange.fetch_positions.return_value = response
    portfolio = Portfolio(1234)
    portfolio.positions = {"BTC/USDT": {"qty": 1, "avg_price": 100}}
    with patch("core.live_broker.ccxt.binance", return_value=exchange):
        broker = LiveBroker(portfolio, market_type="swap", order_store=OrderStore(str(tmp_path / "orders.db")))
    broker.retry_max_attempts = 1
    try:
        assert not broker.sync()
        assert portfolio.positions["BTC/USDT"]["qty"] == 1
    finally:
        broker.close()


@pytest.mark.parametrize("kind", ["market", "stop", "limit"])
def test_canonical_default_tif_only_reaches_venue_for_resting_limit(kind):
    from core.exchange.ccxt_mapper import CCXTRequestMapper
    from core.exchange.metadata import ExchangeCapabilities
    intent = OrderIntent("binance", "sandbox", "BTC/USDT", "1d", "2020-01-01T00:00:00Z",
        "Test", "buy", 0, 2, order_type=kind, price=100, time_in_force="GTC", trigger_price=90 if kind == "stop" else None)
    request = CCXTRequestMapper().map(intent, ExchangeCapabilities("binance", "spot"))
    assert ("timeInForce" in request.params) is (kind == "limit")
    assert intent.time_in_force == "GTC"


def test_nonfinite_account_failure_report_is_strict_json_serializable():
    from core.account_reconciliation import reconcile_account_snapshots
    now = datetime(2026, 9, 20, tzinfo=timezone.utc)
    report = reconcile_account_snapshots({"equity": 100, "extra": [1]},
        {"equity": float("nan"), "extra": [float("inf")]}, checked_at=now)
    assert not report["ok"]
    assert "nonfinite_numeric_fact" in json.dumps(report, allow_nan=False)
