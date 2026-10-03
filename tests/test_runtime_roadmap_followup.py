"""Fault and restart evidence for staged scheduling and managed IP budgets."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Lock
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from core.market_read_batch import BoundedMarketReads
from core.request_budget import SharedRequestBudget, request_scope, RequestBudgetExpired
from live_trading.catchup import plan_catchup, replay_state_only
from live_trading.protection_schedule import ProtectionSchedule, RuntimeSchedulePolicy
from core.state import MarketState
from router.router import Router
from core.market_data import LiveMarketDataAdapter
from core.market_data_errors import MarketDataRefreshError
from core.state_store_v2 import StateStore
from core.order_latency import OrderLatencyRecorder
from live_trading.runtime_controls import RuntimeControls
from core.websocket_market import ClosedBarBuffer
from scripts.measure_sandbox_orders import probe
from core.data_fetcher import DataFetcher
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.domain import SyncResult
from config.config import config
from live_trading.engine import LiveTradingEngine
from unittest.mock import MagicMock
from datetime import datetime, timezone
from threading import get_ident


def test_deadline_returns_without_late_state_commit_or_duplicate_reads():
    release, started = Event(), Event()
    calls, guard = [], Lock()
    def fetch(symbol):
        with guard:
            calls.append(symbol)
        if symbol == "B":
            started.set()
            release.wait(2)
        return {"price": 100.}
    reads = BoundedMarketReads(2)
    try:
        before = time.monotonic()
        result = reads.read(["A", "B"], fetch, timeout_seconds=.05)
        assert started.is_set()
        assert time.monotonic() - before < .5
        assert result.values == {"A": {"price": 100.}}
        assert result.failures == {"B": "refresh_timeout"}
        again = reads.read(["B"], fetch, timeout_seconds=.02)
        assert again.failures == {"B": "fetch_in_progress"}
        assert calls.count("B") == 1
        release.set()
    finally:
        release.set()
        reads.close()
    assert "B" not in result.values


def test_batch_queue_is_bounded_and_pending_jobs_cancelled():
    release, guard = Event(), Lock()
    active, peak = [0], [0]
    def fetch(symbol):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        release.wait(2)
        with guard:
            active[0] -= 1
        return symbol
    reads = BoundedMarketReads(3)
    try:
        result = reads.read(list("ABCDEFGH"), fetch, timeout_seconds=.05)
        assert peak[0] <= 3
        assert len(result.failures) == 8
    finally:
        release.set()
        reads.close()


def history():
    return pd.DataFrame({"close": range(10)}, index=pd.date_range("2026-10-01", periods=10, freq="min"))


def test_catchup_replays_chronologically_and_bounds_each_round():
    frame = history()
    plan = plan_catchup(frame, frame.index[1], timeframe="1m", max_bars=3)
    assert plan.replay_times == tuple(frame.index[2:5])
    assert plan.live_time is None and plan.pending == 4
    restored = frame.index[4].isoformat()
    plan = plan_catchup(frame, restored, timeframe="1m", max_bars=10)
    assert plan.replay_times == tuple(frame.index[5:9])
    assert plan.live_time == frame.index[-1]
    assert not plan.gap


def test_catchup_gap_and_duplicates_cannot_emit_new_risk():
    frame = history().drop(history().index[4])
    plan = plan_catchup(frame, frame.index[1], timeframe="1m")
    assert plan.gap and plan.live_time is None
    assert plan_catchup(frame, frame.index[-1], timeframe="1m").live_time is None
    assert plan_catchup(frame, None, timeframe="1m").replay_times == ()


def test_historical_replay_has_no_execution_or_candidate_path():
    frame = history()
    class Machine:
        def get_state(self, frame, location):
            return MarketState.TREND_UP
    router = Router({}, regime_map={"SIDEWAYS": "Cash", "TREND_UP": "Trend"}, cooldown_bars=2)
    router.symbol_states["A"] = MarketState.SIDEWAYS
    engine = SimpleNamespace(state_machine=Machine(), router=router, strategies={},
        event_processor=SimpleNamespace(_last_market_states={}))
    replay_state_only(engine, "A", frame, frame.index[3])
    assert router.symbol_states["A"] is MarketState.TREND_UP
    assert router._cooldown_started_at["A"] == frame.index[3]


def test_protection_schedule_has_one_owner_and_no_duplicate_or_recursive_cycle():
    now, calls = [0.], []
    schedule = ProtectionSchedule(5., clock=lambda: now[0])
    def protect():
        calls.append(now[0])
        assert not schedule.run_if_due(protect)
    assert schedule.run_if_due(protect)
    assert not schedule.run_if_due(protect)
    now[0] = 5.
    assert schedule.run_if_due(protect)
    assert calls == [0., 5.]
    assert RuntimeSchedulePolicy().enabled is False


def test_reserve_prevents_reads_consuming_critical_capacity(tmp_path):
    now = [100.]
    def sleep(delay):
        now[0] += delay
    budget = SharedRequestBudget(tmp_path / "budget.db", window_seconds=1.,
        reserve_fraction=.2, clock=lambda: now[0], sleep=sleep)
    for _ in range(8):
        budget.wait("venue", interval_seconds=.1, priority="market")
    budget.wait("venue", interval_seconds=.1, priority="critical")
    assert now[0] < 101.
    budget.wait("venue", interval_seconds=.1, priority="market")
    assert now[0] >= 101.


def test_budget_wait_respects_request_deadline(tmp_path):
    budget = SharedRequestBudget(tmp_path / "budget.db", window_seconds=1.)
    budget.wait("venue", interval_seconds=.2)
    with request_scope(deadline=time.monotonic() + .02):
        with pytest.raises(RequestBudgetExpired):
            budget.wait("venue", interval_seconds=.2)


def test_shared_budget_serializes_weighted_mixed_load_across_processes(tmp_path):
    path = str(tmp_path / "multiprocess.db")
    code = """
import json, sys, time
from core.request_budget import SharedRequestBudget
b = SharedRequestBudget(sys.argv[1], window_seconds=2.)
out = []
for _ in range(4):
    admitted = b.wait('mixed', interval_seconds=.015, cost=2., priority=sys.argv[2])
    out.append(admitted)
print(json.dumps(out))
"""
    def run(priority):
        result = subprocess.run([sys.executable, "-c", code, path, priority],
            cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    with ThreadPoolExecutor(3) as pool:
        values = list(pool.map(run, ["market", "research", "critical"]))
    times = sorted(value for rows in values for value in rows)
    assert len(times) == 12
    assert all(b - a >= .029999 for a, b in zip(times, times[1:]))


def test_runtime_controls_timeout_preserves_old_cache_and_discards_late_result():
    release = Event()
    frame = history().assign(open=100., high=101., low=99., close=100., volume=1000.)
    class Fetcher:
        def fetch_ccxt(self, symbol, **kwargs):
            if symbol == "B":
                release.wait(2)
            return frame.copy()
    fetcher = Fetcher()
    adapter = LiveMarketDataAdapter(["A", "B"], fetcher)
    engine = SimpleNamespace(symbols=["A", "B"], market_data_adapter=adapter,
        data_map={"B": frame.copy()}, timeframe="1m", fetcher=fetcher)
    original = engine.data_map["B"]
    controls = RuntimeControls(RuntimeSchedulePolicy(enabled=True, market_timeout_seconds=.1), workers=2)
    try:
        with pytest.raises(MarketDataRefreshError):
            controls.update_data(engine)
        assert engine.data_map["B"] is original
        assert "A" in engine.data_map
        assert adapter.failed_symbols == {"B": "refresh_timeout"}
    finally:
        release.set()
        controls.close()
    assert engine.data_map["B"] is original


def test_bar_cursor_checkpoint_is_atomic_with_processing_fact(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    try:
        assert store.claim_bar("bar", "2026-10-02T00:00:00Z")
        store.complete_bar("bar", "2026-10-02T00:00:01Z",
            state_values={"catchup_cursor:A:1m": "2026-10-02T00:00:00Z"})
        assert store.status("bar") == "processed"
        assert store.get("catchup_cursor:A:1m") == "2026-10-02T00:00:00Z"
        with pytest.raises(ValueError):
            store.complete_bar("missing", "2026-10-02T00:00:02Z", state_values={"cursor": "bad"})
        assert store.get("cursor") is None
    finally:
        store.close()


def test_order_latency_distinguishes_ack_terminal_observation_and_error():
    values = iter([1., 1.1, 2., 2.3, 3., 3.5])
    recorder = OrderLatencyRecorder(clock=lambda: next(values))
    recorder.call("submit", lambda: {"status": "open", "timestamp": 123})
    recorder.call("cancel", lambda: {"status": "canceled"})
    def reject():
        raise TimeoutError("private credential must never enter recorder")
    with pytest.raises(TimeoutError):
        recorder.call("submit", reject)
    result = recorder.summary()
    assert result["records"][0]["duration_seconds"] == pytest.approx(.1)
    assert not result["records"][0]["terminal_observed"]
    assert result["records"][1]["terminal_observed"]
    assert result["statistics"]["submit"]["errors"] == 1
    assert "private credential" not in str(result)


def stream_bar(minute, *, closed=True):
    return {"e": "kline", "s": "BTCUSDT", "k": {"x": closed, "i": "1m",
        "t": 1790812800000 + minute * 60000, "o": "100", "h": "101",
        "l": "99", "c": "100", "v": "10"}}


def test_stream_rejects_unclosed_duplicate_and_out_of_order_bars():
    buffer = ClosedBarBuffer(["BTC/USDT"])
    assert not buffer.ingest(stream_bar(0, closed=False))
    assert buffer.ingest(stream_bar(0))
    assert not buffer.ingest(stream_bar(0))
    assert not buffer.ingest(stream_bar(-1))
    buffer.repair("BTC/USDT", pd.Timestamp(1790812800000, unit="ms"))
    assert len(buffer.drain()["BTC/USDT"]) == 1
    assert buffer.duplicates == 2


def test_stream_gap_overflow_and_reconnect_require_rest_repair():
    buffer = ClosedBarBuffer(["BTC/USDT"], capacity=1)
    buffer.ingest(stream_bar(0))
    buffer.repair("BTC/USDT", pd.Timestamp(1790812800000, unit="ms"))
    buffer.ingest(stream_bar(2))
    assert buffer.overflows == 1
    assert "BTC/USDT" in buffer.repair_required
    assert buffer.drain() == {}
    buffer.reconnect()
    assert "BTC/USDT" in buffer.repair_required


def test_catchup_stops_before_missing_bar_instead_of_advancing_across_gap():
    frame = history().drop(history().index[4])
    plan = plan_catchup(frame, frame.index[1], timeframe="1m")
    assert plan.replay_times == tuple(history().index[2:4])
    assert plan.live_time is None


class SandboxDouble:
    def __init__(self):
        self.creates, self.cancels = [], []
    def load_markets(self):
        return {}
    def market(self, symbol):
        return {"spot": True, "base": "BTC", "quote": "USDT", "limits": {"cost": {"min": 1.}}}
    def fetch_ticker(self, symbol):
        return {"ask": 100.}
    def fetch_balance(self, params):
        return {"free": {"USDT": 1000.}}
    def amount_to_precision(self, symbol, value):
        return str(round(value, 3))
    def price_to_precision(self, symbol, value):
        return str(round(value, 2))
    def create_order(self, symbol, kind, side, quantity, price, params):
        self.creates.append((kind, side, quantity))
        return {"id": str(len(self.creates)), "status": "open" if kind == "limit" else "closed",
                "filled": 0. if kind == "limit" else quantity, "fees": []}
    def cancel_order(self, order_id, symbol):
        self.cancels.append(order_id)
        return {"id": order_id, "status": "canceled", "filled": 0.}


def test_sandbox_probe_caps_orders_and_records_complete_roundtrip():
    exchange, recorder = SandboxDouble(), OrderLatencyRecorder()
    phases = probe(exchange, "BTC/USDT", 20., recorder)
    assert len(exchange.creates) == 3 and len(exchange.cancels) == 1
    assert phases[-1]["residual_base"] == 0.
    assert recorder.summary()["statistics"]["cancel"]["count"] == 1
    with pytest.raises(ValueError):
        probe(exchange, "BTC/USDT", 21., recorder)


def test_sandbox_probe_never_retries_an_ambiguous_write():
    exchange, recorder, phases = SandboxDouble(), OrderLatencyRecorder(), []
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise TimeoutError()
    exchange.create_order = fail
    with pytest.raises(TimeoutError):
        probe(exchange, "BTC/USDT", 20., recorder, phases=phases)
    assert len(calls) == 1
    assert phases[-1]["status"] == "unknown_or_rejected"


def test_enabled_engine_replays_without_orders_then_processes_latest_once(tmp_path):
    owner = get_ident()
    now = datetime(2026, 10, 2, 0, 5, 5, tzinfo=timezone.utc)
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 1000.},
        index=pd.date_range("2026-10-02", periods=5, freq="min"))
    operations = []
    class Fetcher:
        def fetch_ccxt(self, symbol, **kwargs):
            assert get_ident() != owner
            operations.append("read")
            return frame.copy()
    broker = MagicMock()
    broker.exchange_id, broker.account_id, broker.market_type = "binance", "test", "spot"
    broker.portfolio = Portfolio(10000.)
    def sync():
        assert get_ident() == owner
        operations.append("sync")
        return SyncResult(True, now)
    broker.sync.side_effect = sync
    broker.has_unresolved_unknown.return_value = False
    store = StateStore(str(tmp_path / "live.db"))
    engine = LiveTradingEngine(["A"], {}, broker, RiskManager(), config,
        data_fetcher=Fetcher(), clock=lambda: now, state_store=store, timeframe="1m",
        state_file=str(tmp_path / "status.json"), close_grace_seconds=0,
        runtime_policy=RuntimeSchedulePolicy(enabled=True, catchup_max_bars=1))
    engine.state_machine.get_state = MagicMock(return_value=MarketState.TREND_UP)
    engine.router.collect_candidate = MagicMock(return_value=None)
    def protect():
        assert get_ident() == owner
        operations.append("protect")
    engine._reconcile_protective_orders = protect
    store.set("catchup_cursor:A:1m", frame.index[0].isoformat())
    try:
        assert engine._tick()
        assert operations.index("protect") < operations.index("read")
        assert engine.router.collect_candidate.call_count == 0
        assert engine._tick()
        assert engine.router.collect_candidate.call_count == 0
        assert engine._tick()
        assert engine.router.collect_candidate.call_count == 1
        assert engine._tick()
        assert engine.router.collect_candidate.call_count == 1
        assert store.get("catchup_cursor:A:1m") == frame.index[-1].isoformat()
        broker.submit_order.assert_not_called()
    finally:
        engine.runtime_controls.close()
        store.close()


def test_native_cursor_backfill_is_one_bounded_page_before_live_resume(tmp_path, monkeypatch):
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 1000.},
        index=pd.date_range("2026-10-02", periods=30, freq="min"))
    fetcher = DataFetcher(proxy_url=None)
    monkeypatch.setattr(fetcher, "fetch_ccxt", lambda *a, **k: frame.iloc[20:30].copy())
    backfill = MagicMock(return_value=frame.iloc[:14].copy())
    monkeypatch.setattr(fetcher, "fetch_ccxt_since", backfill)
    store = StateStore(str(tmp_path / "state.db"))
    store.set("catchup_cursor:A:1m", frame.index[10].isoformat())
    adapter = LiveMarketDataAdapter(["A"], fetcher, lookback=10, timeframe="1m", exchange_id="binance")
    router = Router({}, regime_map={"TREND_UP": "Cash"})
    engine = SimpleNamespace(symbols=["A"], timeframe="1m", fetcher=fetcher,
        market_data_adapter=adapter, data_map={}, state_store=store, router=router, strategies={},
        state_machine=SimpleNamespace(get_state=lambda *a: MarketState.TREND_UP),
        event_processor=SimpleNamespace(_last_market_states={}), _alert=lambda *a: None)
    controls = RuntimeControls(RuntimeSchedulePolicy(enabled=True, catchup_max_bars=3))
    try:
        controls.update_data(engine)
        assert backfill.call_count == 1
        assert backfill.call_args.kwargs["limit"] == 14
        assert controls.prepare_bar(engine, "A", engine.data_map["A"], store) is None
        assert store.get("catchup_cursor:A:1m") == frame.index[13].isoformat()
        assert controls.catchup_status["A"]["gap"]
    finally:
        controls.close()
        fetcher.close()
        store.close()
