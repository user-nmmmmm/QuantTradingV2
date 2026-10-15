"""AUD-05: failed runs stay visible, catchup clients agree, and host overrides dispatch."""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock

import ccxt
import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.broker import Broker
from core.market.data_fetcher import DataFetcher
from core.trading.account_cost_contract import default_runtime_market_type
from core.trading.portfolio import Portfolio
from dashboard.experiment_compare import compare_experiments
from dashboard.visual_data import load_backtest


def _report(root, name, values, dates=None):
    folder = root / name
    folder.mkdir()
    index = pd.to_datetime(dates) if dates is not None else pd.date_range("2026-01-01", periods=len(values))
    pd.DataFrame({"equity": values}, index=index.rename("timestamp")).to_csv(folder / "equity.csv")
    return folder


@pytest.mark.parametrize("values, end, total, worst, current", [
    ([100, 120, 90, 110], 110, .1, -.25, -1 / 12),
    ([100, 120, 0], 0, -1, -1, -1),
    ([100, 120, 0, -10], -10, -1.1, -13 / 12, -13 / 12),
    ([100, 120, -10, 130], 130, .3, -13 / 12, 0),
    ([100, 120, 0, -10, 60], 60, -.4, -13 / 12, -.5),
])
def test_dashboard_preserves_wipeout_debt_and_recovery_points(tmp_path, values, end, total, worst, current):
    _report(tmp_path, "run", values)
    result = load_backtest(tmp_path, "run")
    assert [point["equity"] for point in result["points"]] == values
    assert result["period_end"] == result["points"][-1]["timestamp"]
    assert result["invalid_rows"] == 0
    metrics = result["metrics"]
    assert metrics["observations"] == len(values) and metrics["periods"] == len(values) - 1
    assert metrics["end_equity"] == end
    assert metrics["total_return"] == pytest.approx(total)
    assert metrics["max_drawdown"] == pytest.approx(worst)
    assert metrics["current_drawdown"] == pytest.approx(current)
    if end == 0:
        assert metrics["annualized_return"] == -1
    elif end < 0:
        assert metrics["annualized_return"] is None
    json.dumps(result, allow_nan=False)
    assert load_backtest(tmp_path, "run") == result  # the complete result is also safe to cache


@pytest.mark.parametrize("start", [0, -10])
def test_nonpositive_start_is_rejected_instead_of_rebasing_to_a_surviving_point(tmp_path, start):
    _report(tmp_path, "run", [start, 100, 120])
    with pytest.raises(ValueError, match="starting equity must be positive"):
        load_backtest(tmp_path, "run")


def test_nonpositive_period_and_monthly_denominators_are_undefined(tmp_path):
    _report(tmp_path, "run", [100, 0, -10, 25],
            ["2026-01-01", "2026-01-31", "2026-02-28", "2026-03-31"])
    result = load_backtest(tmp_path, "run")
    assert result["metrics"]["periods"] == 3 and result["invalid_rows"] == 0
    assert result["monthly_returns"][0]["return"] == -1
    assert [month["return"] for month in result["monthly_returns"]][1:] == [None, None]
    for name in ("period_volatility", "positive_period_ratio", "best_period_return", "worst_period_return"):
        assert result["metrics"][name] is None
    assert result["metrics"]["total_return"] == -.75
    json.dumps(result, allow_nan=False)


def test_a_positive_base_can_report_a_loss_beyond_one_hundred_percent(tmp_path):
    folder = _report(tmp_path, "run", [100, -10], ["2026-01-01", "2026-02-01"])
    (folder / "benchmark.csv").write_text("timestamp,reference\n2026-01-01,100\n2026-02-01,110\n", encoding="utf-8")
    result = load_backtest(tmp_path, "run")
    assert result["metrics"]["worst_period_return"] == pytest.approx(-1.1)
    assert result["metrics"]["positive_period_ratio"] == 0
    assert result["monthly_returns"][-1]["return"] == pytest.approx(-1.1)
    assert result["metrics"]["excess_return"] == pytest.approx(-1.2)
    assert result["metrics"]["annualized_return"] is None


def test_unrepresentable_drawdown_fails_explicitly_instead_of_emitting_nonfinite_json(tmp_path):
    _report(tmp_path, "run", [1e-300, -1e300])
    with pytest.raises(ValueError, match="drawdown.*representable"):
        load_backtest(tmp_path, "run")


def test_comparison_keeps_zero_and_negative_nav_and_the_true_trough(tmp_path):
    _report(tmp_path, "failed", [100, 120, 0, -10])
    _report(tmp_path, "healthy", [1000, 1200, 1100, 1300])
    result = compare_experiments(tmp_path, ["failed", "healthy"])
    failed = result["runs"][0]
    assert [point["nav"] for point in failed["points"]] == [100, 120, 0, -10]
    assert failed["point_count"] == 4
    assert failed["metrics"]["end_equity"] == -10
    assert failed["points"][-1]["drawdown"] == pytest.approx(-13 / 12)
    json.dumps(result, allow_nan=False)


@pytest.mark.slow
@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_generated_report_and_dashboard_agree_on_the_same_failed_run(tmp_path):
    folder = tmp_path / "run"
    generator = ReportGenerator(str(folder))
    curve = pd.DataFrame({"equity": [100., 120., 0., -10.], "cash": [100., 120., 0., -10.]},
                         index=pd.date_range("2026-01-01", periods=4, name="timestamp"))
    recorded = generator.generate([], curve, report_profile="full")
    projected = load_backtest(tmp_path, "run")["metrics"]
    assert projected["end_equity"] == recorded["EndEquity"] == -10
    assert projected["total_return"] == recorded["TotalReturn"] == pytest.approx(-1.1)
    assert projected["max_drawdown"] == recorded["MaxDrawdownPct"] == pytest.approx(-13 / 12)
    assert "-10.0000" in (folder / "report.txt").read_text(encoding="utf-8")
    assert (folder / "equity.png").is_file()


@pytest.fixture()
def fake_exchange(monkeypatch):
    made = []

    def create(options):
        exchange = MagicMock()
        exchange.market.return_value = {"contract": True}
        exchange.fetch_ohlcv.return_value = [[1704067200000, 100., 101., 99., 100., 10.]]
        made.append(exchange)
        return exchange

    constructor = MagicMock(side_effect=create)
    monkeypatch.setattr(ccxt, "binance", constructor)
    return constructor, made


@pytest.mark.parametrize("market_type", ["spot", "margin", "future", "futures", "perpetual", "swap"])
@pytest.mark.parametrize("catchup_first", [False, True])
def test_regular_and_catchup_aliases_share_the_same_client(fake_exchange, market_type, catchup_first):
    constructor, made = fake_exchange
    fetcher = DataFetcher(proxy_url=None)
    symbol = "BTC/USDT" if market_type in {"spot", "margin"} else "BTC/USDT:USDT"
    expected = "spot" if market_type in {"spot", "margin"} else "future"

    def regular():
        return fetcher._fetch_ccxt_once("binance", symbol, "1m", None, None, 2, market_type)

    def catchup():
        return fetcher.fetch_ccxt_since(symbol, "2024-01-01T00:00:00Z", market_type=market_type, limit=2)

    try:
        results = [operation() for operation in ((catchup, regular) if catchup_first else (regular, catchup))]
        assert all(len(frame) == 1 for frame in results)
        assert [call.args[0]["options"]["defaultType"] for call in constructor.call_args_list] == [expected]
        assert made[0].fetch_ohlcv.call_count == 2
        assert made[0].load_markets.call_count == (0 if expected == "spot" else 2)
    finally:
        fetcher.close()


@pytest.mark.parametrize("market_type", ["future", "futures", "perpetual", "swap"])
@pytest.mark.parametrize("path", ["regular", "catchup"])
def test_derivative_aliases_still_reject_spot_metadata(fake_exchange, market_type, path):
    constructor, _ = fake_exchange
    exchange = MagicMock()
    exchange.market.return_value = {"contract": False}
    constructor.side_effect = None
    constructor.return_value = exchange
    fetcher = DataFetcher(proxy_url=None)
    try:
        with pytest.raises(ValueError, match="spot"):
            if path == "regular":
                fetcher._fetch_ccxt_once("binance", "BTC/USDT:USDT", "1m", None, None, 2, market_type)
            else:
                fetcher.fetch_ccxt_since("BTC/USDT:USDT", "2024-01-01", market_type=market_type)
        exchange.fetch_ohlcv.assert_not_called()
    finally:
        fetcher.close()


def test_runtime_perpetual_default_reuses_the_existing_derivative_client(fake_exchange):
    constructor, made = fake_exchange
    fetcher = DataFetcher(proxy_url=None)
    try:
        fetcher._fetch_ccxt_once("binance", "BTC/USDT:USDT", "1m", None, None, 2, "future")
        fetcher.fetch_ccxt_since("BTC/USDT:USDT", "2024-01-01", market_type=default_runtime_market_type("perpetual"))
        assert constructor.call_count == 1 and made[0].fetch_ohlcv.call_count == 2
    finally:
        fetcher.close()


WRITER_HOOKS = {
    "_write_lifecycle_section": ("metrics", ["BacktestLifecycle", "FullCapitalPeriodMetrics", "ActiveStrategyPeriodMetrics"]),
    "_write_strategy_health_section": ("metrics", ["StrategyHealth"]),
    "_write_protective_stop_section": ("metrics", ["ProtectiveStops"]),
    "_write_diagnostics_section": ("metrics", ["Diagnostics"]),
    "_write_exposure_section": ("analysis", ["exposure"]),
    "_write_signal_funnel_section": ("analysis", ["signal_funnel"]),
    "_write_drawdown_events_section": ("analysis", ["drawdown_events"]),
    "_write_trade_quality_section": ("analysis", ["trade_quality"]),
    "_write_attribution_section": ("analysis", ["attribution"]),
    "_write_control_attribution": ("analysis", ["attribution"]),
    "_write_benchmark_section": ("analysis", ["benchmark_comparison"]),
}


@pytest.mark.parametrize("hook_name", WRITER_HOOKS)
@pytest.mark.parametrize("binding", ["instance", "static"])
def test_text_report_dispatches_every_retained_section_hook_on_the_subclass(tmp_path, hook_name, binding):
    calls = []

    def custom(stream, *values):
        calls.append(values)
        stream.write(f"CUSTOM {hook_name}\n")

    def method(self, stream, *values):
        return custom(stream, *values)

    class CustomReport(ReportGenerator):
        pass

    setattr(CustomReport, hook_name, staticmethod(custom) if binding == "static" else method)
    bucket, keys = WRITER_HOOKS[hook_name]
    expected = tuple({"sentinel": key} for key in keys)
    arguments = {"metrics": {}, "analysis": {}}
    arguments[bucket].update(zip(keys, expected))
    CustomReport(str(tmp_path))._save_report_text(**arguments)
    assert calls == [expected]
    assert f"CUSTOM {hook_name}" in (tmp_path / "report.txt").read_text(encoding="utf-8")


def test_subclass_section_can_call_super_without_recursing(tmp_path):
    class CustomReport(ReportGenerator):
        def _write_trade_quality_section(self, stream, quality):
            stream.write("custom prefix\n")
            super()._write_trade_quality_section(stream, quality)

    CustomReport(str(tmp_path))._save_report_text({})
    assert (tmp_path / "report.txt").read_text(encoding="utf-8").count("custom prefix") == 1


@pytest.mark.parametrize("binding", ["static", "class"])
def test_broker_order_identity_uses_the_subclasses_time_hook(binding):
    class StaticTimeBroker(Broker):
        @staticmethod
        def _event_time(value):
            return Broker._event_time(value) + timedelta(seconds=7)

    class ClassTimeBroker(Broker):
        @classmethod
        def _event_time(cls, value):
            return super()._event_time(value) + timedelta(seconds=7)

    cls = StaticTimeBroker if binding == "static" else ClassTimeBroker
    broker = cls(Portfolio(1000))
    stamp = "2026-10-15T08:00:00+08:00"
    assert cls._iso_time(stamp) == "2026-10-15T00:00:07Z"
    order = broker.submit_order("BTC/USDT", "buy", 1, price=100, timestamp=pd.Timestamp(stamp))
    assert order.intent.bar_time == "2026-10-15T00:00:07Z"
    assert order.id == order.intent.client_order_id
    assert broker.event_pipeline.events
    assert all(event.occurred_at == broker._event_time(stamp) for event in broker.event_pipeline.events)


@pytest.mark.parametrize("stamp", ["2026-10-15", "2026-10-15T08:00:00+08:00", "2026-10-15T00:00:00Z"])
def test_base_broker_keeps_its_existing_utc_serialization(stamp):
    assert Broker._iso_time(stamp) == "2026-10-15T00:00:00Z"
