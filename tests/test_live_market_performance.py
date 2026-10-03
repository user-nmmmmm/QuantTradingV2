"""Rolling live histories, partial failures and bounded public-I/O concurrency."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock
from time import sleep
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from config.config import config
from core.broker import Broker
from core.data_fetcher import DataFetcher
from core.domain import SyncResult
from core.market_data import LiveMarketDataAdapter
from core.market_data_errors import MarketDataRefreshError
from core.portfolio import Portfolio
from core.public_data_clients import PublicDataClientPool, VenueRequestPacer
from core.risk import RiskManager
from core.state import MarketState, MarketStateMachine
from core.state_store_v2 import StateStore
from live_trading.engine import LiveTradingEngine
from router.router import Router
from strategies.base import Strategy
from strategies.mean_reversion import RangeStrategy
from strategies.trend_breakout import TrendBreakoutStrategy


def bars(day=0, rows=140):
    prices = np.linspace(100, 130, rows)
    return pd.DataFrame({"open": prices, "high": prices + 1, "low": prices - 1,
                         "close": prices, "volume": 1000.},
        index=pd.date_range(pd.Timestamp("2026-01-01") + pd.Timedelta(days=day), periods=rows))


class Fetcher:
    def __init__(self):
        self.frame = bars()
        self.errors = {}
        self.active = self.peak = 0
        self.lock = Lock()

    def fetch_ccxt(self, symbol, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            sleep(0.005)
            if symbol in self.errors:
                raise self.errors[symbol]
            return self.frame.copy(deep=True)
        finally:
            with self.lock:
                self.active -= 1


def test_refresh_reuses_unchanged_view_and_invalidates_all_derived_columns():
    fetcher = Fetcher()
    adapter = LiveMarketDataAdapter(["A"], fetcher, lookback=140)
    first = adapter.refresh()["A"]
    machine = MarketStateMachine(stability_period=1)
    breakout = TrendBreakoutStrategy()
    machine.get_state(first, 139)
    breakout._ensure_indicators(first)
    first["plugin_cache"] = 1.
    assert adapter.refresh()["A"] is first
    assert adapter.refresh_metrics["reused_symbols"] == 1
    fetcher.frame = bars(1)
    fresh = adapter.refresh()["A"]
    assert "market_state" not in fresh
    assert "plugin_cache" not in fresh
    assert breakout.col_high_max not in fresh
    assert machine.get_state(fresh, 139) is MarketState.TREND_UP
    breakout._ensure_indicators(fresh)
    assert np.isfinite(fresh[breakout.col_high_max].iloc[-1])
    expected = fetcher.frame["high"].rolling(20).max().shift(1)
    pd.testing.assert_series_equal(fresh[breakout.col_high_max], expected, check_names=False, check_freq=False)
    assert "market_state" not in fetcher.frame


def test_overlapping_price_revision_invalidates_source_cache():
    fetcher = Fetcher()
    adapter = LiveMarketDataAdapter(["A"], fetcher, lookback=140)
    first = adapter.refresh()["A"]
    first["cached_signal"] = 42
    fetcher.frame.iloc[-1, fetcher.frame.columns.get_loc("close")] = 150.
    fresh = adapter.refresh()["A"]
    assert fresh["close"].iloc[-1] == 150.
    assert "cached_signal" not in fresh
    assert adapter.refresh_metrics["recomputed_symbols"] == 1


def test_parallel_fetch_is_bounded_and_commits_successes_in_symbol_order():
    fetcher = Fetcher()
    symbols = list("ABCDEFGH")
    adapter = LiveMarketDataAdapter(symbols, fetcher, max_workers=3, lookback=140)
    try:
        adapter.refresh()
        assert 1 < fetcher.peak <= 3
        assert list(adapter.data_map) == symbols
        fetcher.frame = bars(1)
        fetcher.errors["A"] = TimeoutError()
        adapter.refresh()
        assert adapter.failed_symbols == {"A": "TimeoutError"}
        assert adapter.data_map["A"].index[-1] == bars().index[-1]
        assert adapter.data_map["B"].index[-1] == bars(1).index[-1]
        fetcher.errors.clear()
        adapter.refresh()
        assert not adapter.failed_symbols
    finally:
        adapter.close()


def test_empty_response_halts_engine_new_risk_and_retains_cached_history():
    fetcher = Fetcher()
    adapter = LiveMarketDataAdapter(["A"], fetcher, lookback=140)
    adapter.refresh()
    fetcher.frame = pd.DataFrame()
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.market_data_adapter, engine.data_map = adapter, dict(adapter.data_map)
    with pytest.raises(MarketDataRefreshError) as failure:
        engine._update_data()
    assert failure.value.failures == {"A": "empty_response"}
    assert len(engine.data_map["A"]) == 140


def test_regression_remains_visible_until_provider_catches_up():
    fetcher = Fetcher()
    adapter = LiveMarketDataAdapter(["A"], fetcher, lookback=140)
    adapter.refresh()
    fetcher.frame = bars(-1)
    adapter.refresh()
    assert adapter.regressed_symbols == {"A"}
    adapter.refresh()
    assert adapter.regressed_symbols == {"A"}
    fetcher.frame = bars()
    adapter.refresh()
    assert not adapter.regressed_symbols


class ProbeStrategy(Strategy):
    def __init__(self):
        super().__init__("Probe", {MarketState.TREND_UP})
        self.entries = self.exits = 0
    def should_enter(self, *args):
        return None
    def should_exit(self, *args):
        self.exits += 1
        return None
    def build_entry_candidate(self, *args):
        self.entries += 1
        return None


def test_router_cooldown_finishes_in_rolling_windows():
    strategy, portfolio = ProbeStrategy(), Portfolio(10000)
    router = Router({"Probe": strategy},
        regime_map={"SIDEWAYS": "Cash", "TREND_UP": "Probe"}, cooldown_bars=2)
    broker, risk = Broker(portfolio), RiskManager()
    for day in range(6):
        router.collect_entry_candidate("A", 139, bars(day),
            MarketState.SIDEWAYS if day == 0 else MarketState.TREND_UP, portfolio, broker, risk)
    assert strategy.entries == 2
    assert "A" not in router.cooldowns


def test_router_short_window_cooldown_survives_duplicates_and_restart():
    strategy, portfolio = ProbeStrategy(), Portfolio(10000)
    settings = dict(regime_map={"SIDEWAYS": "Cash", "TREND_UP": "Probe"}, cooldown_bars=2)
    router = Router({"Probe": strategy}, **settings)
    broker, risk = Broker(portfolio), RiskManager()
    for day in range(4):
        for _ in range(2):
            router.collect_entry_candidate("A", 0, bars(day).iloc[-1:].copy(),
                MarketState.SIDEWAYS if day == 0 else MarketState.TREND_UP, portfolio, broker, risk)
        if day == 2:
            checkpoint = router.checkpoint()
            router = Router({"Probe": strategy}, **settings)
            router.restore_checkpoint(checkpoint)
    assert strategy.entries == 0
    for day in (4, 5):
        router.collect_entry_candidate("A", 0, bars(day).iloc[-1:].copy(),
            MarketState.TREND_UP, portfolio, broker, risk)
    assert strategy.entries == 2
    assert not router.cooldowns


def test_normal_exit_resumes_after_one_new_bar_with_rolling_history():
    strategy, portfolio = ProbeStrategy(), Portfolio(10000)
    portfolio.update_position("A", 1., 100.)
    strategy.context["A"] = {"entry_bar": 139, "stop_loss": 0.,
                             "entry_timestamp": bars().index[-1].isoformat()}
    broker = Broker(portfolio)
    for day in range(6):
        strategy.process_exit_only("A", 139, bars(day), MarketState.TREND_UP, portfolio, broker)
    assert strategy.exits == 4
    restored = ProbeStrategy()
    restored._restore_close_checkpoint(strategy._close_checkpoint())
    restored.process_exit_only("A", 139, bars(6), MarketState.TREND_UP, portfolio, broker)
    assert restored.exits == 1


def test_exit_cooldown_short_window_counts_distinct_bars_after_restart():
    strategy, portfolio = ProbeStrategy(), Portfolio(10000)
    portfolio.update_position("A", 1., 100.)
    strategy.context["A"] = {"entry_bar": 0, "stop_loss": 0.}
    broker = Broker(portfolio)
    for day in (0, 1):
        for _ in range(2):
            strategy.process_exit_only("A", 0, bars(day).iloc[-1:].copy(),
                MarketState.TREND_UP, portfolio, broker)
    assert strategy.exits == 0
    restored = ProbeStrategy()
    restored._restore_close_checkpoint(strategy._close_checkpoint())
    restored.process_exit_only("A", 0, bars(2).iloc[-1:].copy(),
        MarketState.TREND_UP, portfolio, broker)
    assert restored.exits == 1


def test_range_loss_cooldown_finishes_in_rolling_windows(monkeypatch):
    strategy = RangeStrategy()
    for _ in range(3):
        strategy.on_trade_closed("A", -1., {"timestamp": bars().index[-1]}, 139)
    raw = MagicMock(return_value=None)
    monkeypatch.setattr(strategy, "raw_entry_signal", raw)
    portfolio = Portfolio()
    for day in range(1, 25):
        strategy.should_enter("A", 139, bars(day), MarketState.SIDEWAYS, portfolio)
    assert raw.call_count == 0
    for day in (25, 26, 27):
        strategy.should_enter("A", 139, bars(day), MarketState.SIDEWAYS, portfolio)
    assert raw.call_count == 3


def test_range_short_window_loss_cooldown_counts_bars_after_restart(monkeypatch):
    strategy, portfolio = RangeStrategy(), Portfolio()
    for _ in range(3):
        strategy.on_trade_closed("A", -1., {"timestamp": bars().index[-1]}, 0)
    raw = MagicMock(return_value=None)
    monkeypatch.setattr(strategy, "raw_entry_signal", raw)
    monkeypatch.setattr(strategy, "_ensure_indicators", lambda _: None)
    for day in range(1, 25):
        for _ in range(2):
            strategy.should_enter("A", 0, bars(day).iloc[-1:].copy(), MarketState.SIDEWAYS, portfolio)
        if day == 12:
            checkpoint = strategy._close_checkpoint()
            strategy = RangeStrategy()
            strategy._restore_close_checkpoint(checkpoint)
            monkeypatch.setattr(strategy, "raw_entry_signal", raw)
            monkeypatch.setattr(strategy, "_ensure_indicators", lambda _: None)
    assert raw.call_count == 0
    for day in (25, 26, 27):
        strategy.should_enter("A", 0, bars(day).iloc[-1:].copy(), MarketState.SIDEWAYS, portfolio)
    assert raw.call_count == 3


def test_public_client_reuse_and_latest_snapshot_single_page(monkeypatch):
    import ccxt
    exchange = MagicMock()
    exchange.fetch_ohlcv.return_value = [
        [1704067200000, 100, 101, 99, 100, 10],
        [1704153600000, 100, 101, 99, 100, 10]]
    constructor = MagicMock(return_value=exchange)
    monkeypatch.setattr(ccxt, "binance", constructor)
    fetcher = DataFetcher(proxy_url=None)
    try:
        for _ in range(2):
            assert len(fetcher.fetch_ccxt("BTC/USDT", limit=2, exchange_id="binance")) == 2
        assert constructor.call_count == 1
        assert exchange.fetch_ohlcv.call_count == 2
    finally:
        fetcher.close()


def test_public_clients_are_owned_by_worker_threads(monkeypatch):
    barrier, made = Barrier(2), []
    def factory():
        client = SimpleNamespace(rateLimit=10., session=MagicMock())
        made.append(client)
        return client
    pool = PublicDataClientPool()
    def use():
        first = pool.get("test-venue", "spot", factory)
        barrier.wait(timeout=5)
        assert pool.get("test-venue", "spot", factory) is first
        return first
    with ThreadPoolExecutor(max_workers=2) as executor:
        clients = list(executor.map(lambda _: use(), range(2)))
    assert clients[0] is not clients[1]
    assert len(made) == 2
    pool.close()
    assert all(client.session.close.call_count == 1 for client in made)


def test_pacer_preserves_weighted_spacing_with_virtual_clock():
    now, sleeps = [0.], []
    def advance(delay):
        sleeps.append(delay)
        now[0] += delay
    pacer = VenueRequestPacer(clock=lambda: now[0], sleep=advance)
    for spacing in (0.01, 0.02, 0.03):
        pacer.wait(spacing)
    assert sleeps == pytest.approx([0.02, 0.03])
    with pytest.raises(ValueError):
        pacer.wait(float("nan"))


def test_public_clients_share_endpoint_weighted_pacing_across_pools(monkeypatch):
    from core import public_data_clients as module
    now, sleeps = [0.], []
    def advance(delay):
        sleeps.append(delay)
        now[0] += delay
    monkeypatch.setitem(module._PACERS, "weighted-test", VenueRequestPacer(
        clock=lambda: now[0], sleep=advance))
    pools = [PublicDataClientPool(), PublicDataClientPool()]
    try:
        clients = [pool.get("weighted-test", "spot", lambda: SimpleNamespace(
            rateLimit=10., session=MagicMock())) for pool in pools]
        clients[0].throttle(1.)
        clients[1].throttle(2.)
        clients[0].throttle(3.)
        assert sleeps == pytest.approx([0.02, 0.03])
    finally:
        for pool in pools:
            pool.close()


def test_native_live_snapshot_attempts_once_without_retry_sleep(monkeypatch):
    from core import data_fetcher as module
    fetcher = DataFetcher(proxy_url=None)
    attempt = MagicMock(side_effect=TimeoutError())
    backoff = MagicMock()
    monkeypatch.setattr(fetcher, "_fetch_ccxt_once", attempt)
    monkeypatch.setattr(module.time, "sleep", backoff)
    adapter = LiveMarketDataAdapter(["BTC/USDT"], fetcher, exchange_id="binance")
    try:
        adapter.refresh()
        assert adapter.failed_symbols == {"BTC/USDT": "empty_response"}
        assert attempt.call_count == 1
        backoff.assert_not_called()
    finally:
        fetcher.close()


@pytest.mark.parametrize("elapsed, healthy, expected", [(0.25, True, 0.75), (2., True, 0.), (0.25, False, 3.)])
def test_loop_sleep_excludes_tick_work_and_preserves_failure_backoff(monkeypatch, elapsed, healthy, expected):
    from live_trading import engine as module
    live = LiveTradingEngine.__new__(LiveTradingEngine)
    live.interval, live._next_retry_delay = 1., 3.
    live._owns_fetcher = False
    live.market_data_adapter = MagicMock()
    live._tick = MagicMock(return_value=healthy)
    calls, times = [], iter((0., elapsed))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    def stop(delay):
        calls.append(delay)
        raise KeyboardInterrupt()
    monkeypatch.setattr(module.time, "sleep", stop)
    live.run()
    assert calls == [expected]
    live.market_data_adapter.close.assert_called_once()


def test_tick_uses_post_fetch_clock_to_process_newly_closed_bar(tmp_path):
    clock = [datetime(2026, 10, 2, 23, 59, 30, tzinfo=timezone.utc)]
    frame = pd.DataFrame({"open": [100.], "high": [101.], "low": [99.],
                          "close": [100.], "volume": [1000.]},
                         index=pd.to_datetime(["2026-10-02T23:59:00Z"]))
    fetcher = MagicMock()
    def fetch(*args, **kwargs):
        clock[0] += timedelta(seconds=60)
        return frame
    fetcher.fetch_ccxt.side_effect = fetch
    broker = MagicMock()
    broker.exchange_id, broker.account_id, broker.market_type = "binance", "spot", "spot"
    broker.portfolio = Portfolio()
    broker.sync.side_effect = lambda: SyncResult(True, clock[0])
    broker.recover_open_orders.return_value = {}
    broker.has_unresolved_unknown.return_value = False
    store = StateStore(str(tmp_path / "state.db"))
    strategy = RangeStrategy()
    strategy.trade_state["A"] = {"consecutive_losses": 0, "cooldown_until": 163,
        "cooldown_started_at": "2026-10-02T23:58:00+00:00", "cooldown_bar_count": 1,
        "cooldown_last_timestamp": "2026-10-02T23:59:00+00:00"}
    live = LiveTradingEngine(["A"], {strategy.name: strategy}, broker, RiskManager(), config,
        data_fetcher=fetcher, clock=lambda: clock[0], timeframe="1m",
        state_file=str(tmp_path / "status.json"), state_store=store, close_grace_seconds=0)
    live.state_machine.get_state = MagicMock(return_value=MarketState.SIDEWAYS)
    live.router.collect_candidate = MagicMock(return_value=None)
    try:
        assert live._tick()
        assert live._current_trading_day == clock[0].date()
        assert live.health_assessment.assessed_at == clock[0]
        assert store.status("binance|spot|A|1m|2026-10-03T00:00:00+00:00") == "processed"
        saved = store.get(f"strategy_runtime:{strategy.name}")
        assert saved["trade_state"] == strategy.trade_state
        restored = RangeStrategy()
        restarted = LiveTradingEngine(["A"], {restored.name: restored}, broker, RiskManager(), config,
            data_fetcher=fetcher, clock=lambda: clock[0], timeframe="1m",
            state_file=str(tmp_path / "status.json"), state_store=store, close_grace_seconds=0)
        restarted._ensure_state_store()
        assert restored.trade_state == strategy.trade_state
    finally:
        store.close()


@pytest.mark.parametrize("workers", [0, -1, 33, True, 1.5])
def test_invalid_worker_limits_fail_early(workers):
    with pytest.raises(ValueError):
        LiveMarketDataAdapter([], Fetcher(), max_workers=workers)
