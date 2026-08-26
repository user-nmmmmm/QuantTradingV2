from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from backtest.engine import BacktestEngine
from core.backtest_audit import (
    cross_verify_top_trades,
    validate_audit_coverage,
    write_event_log,
)
from core.benchmarks import (
    dynamic_equal_weight_rebalanced,
    fixed_equal_weight_buy_hold,
)
from core.broker import Broker
from core.data import DataHandler
from core.events import Signal
from core.market_data import HistoricalMarketDataAdapter
from core.portfolio import Portfolio
from core.reproducibility import (
    data_identity,
    deterministic_result_digest,
    load_data_snapshots,
    save_data_snapshots,
    sha256_frame,
)
from core.universe import PointInTimeUniverse, UniverseMembership


def _frame(index, closes):
    closes = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": 10_000.0,
        },
        index=index,
    )


def test_t2_1_to_2_4_snapshot_hash_and_data_identity_are_exact(tmp_path):
    frame = DataHandler.annotate_quality(
        _frame(pd.date_range("2024-01-01", periods=3, freq="D"), [100, 101, 102])
    )
    entries = save_data_snapshots({"BTC/USDT": frame}, tmp_path)
    restored = load_data_snapshots(tmp_path, entries)
    assert sha256_frame(restored["BTC/USDT"]) == sha256_frame(frame)

    identity = data_identity(
        {"BTC/USDT": frame},
        source="ccxt",
        exchange="binance",
        market_type="spot",
        timeframe="1d",
        timezone_name="UTC",
        downloaded_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    assert identity["symbols"]["BTC/USDT"]["sha256"] == sha256_frame(frame)
    assert identity["exchange"] == "binance"


def test_t2_6_union_and_intersection_alignment_are_explicit():
    first = _frame(pd.to_datetime(["2024-01-01", "2024-01-02"]), [100, 101])
    second = _frame(pd.to_datetime(["2024-01-02", "2024-01-03"]), [200, 201])
    union = HistoricalMarketDataAdapter(
        {"A": first, "B": second}, calculate_indicators=False, alignment_mode="union"
    )
    intersection = HistoricalMarketDataAdapter(
        {"A": first, "B": second}, calculate_indicators=False,
        alignment_mode="intersection",
    )
    assert list(union.timestamps) == list(pd.date_range("2024-01-01", periods=3, freq="D"))
    assert list(intersection.timestamps) == [pd.Timestamp("2024-01-02")]
    assert set(list(union.stream())[0].bars) == {"A"}


def test_t2_7_fixed_benchmark_never_adds_late_asset():
    first = _frame(pd.date_range("2024-01-01", periods=3, freq="D"), [100, 110, 121])
    late = _frame(pd.date_range("2024-01-02", periods=2, freq="D"), [50, 100])
    result = fixed_equal_weight_buy_hold({"A": first, "LATE": late}, 1000.0)
    assert result is not None
    assert result.metadata["eligible_assets"] == ["A"]
    assert result.equity.iloc[-1] == 1210.0
    assert result.weights["LATE"].eq(0.0).all()


def test_t2_8_dynamic_benchmark_records_weights_turnover_and_cost():
    index = pd.date_range("2024-01-01", periods=2, freq="D")
    first = _frame(index, [100, 110])
    second = _frame(index, [100, 90])
    result = dynamic_equal_weight_rebalanced(
        {"A": first, "B": second}, 1000.0, cost_bps=10.0
    )
    assert result is not None
    assert result.weights.iloc[0].to_dict() == {"A": 0.5, "B": 0.5}
    assert result.turnover.iloc[0] == 1.0
    assert result.costs.iloc[0] == 1.0
    assert result.costs.sum() > 0


def test_t2_9_event_log_covers_signal_risk_order_fill_and_close(tmp_path):
    portfolio = Portfolio(initial_capital=10_000.0)
    broker = Broker(portfolio, slippage=0.0)
    signal_time = pd.Timestamp("2024-01-01", tz="UTC")
    broker.event_pipeline.publish(
        Signal(strategy_id="Test", symbol="BTC", action="buy"),
        occurred_at=signal_time.to_pydatetime(),
        symbol="BTC",
        source="test",
    )
    broker.submit_order(
        "BTC", "buy", 1.0, price=100.0, timestamp=signal_time,
        strategy_id="Test",
    )
    fill_time = signal_time + pd.Timedelta(days=1)
    broker.process_orders({"BTC": _frame(pd.DatetimeIndex([fill_time]), [100]).iloc[0]})
    broker.event_pipeline.publish(
        Signal(strategy_id="Test", symbol="BTC", action="sell"),
        occurred_at=(fill_time + pd.Timedelta(hours=1)).to_pydatetime(),
        symbol="BTC",
        source="test",
    )
    broker.submit_order(
        "BTC", "sell", 1.0, price=110.0,
        timestamp=fill_time + pd.Timedelta(hours=1), strategy_id="Test",
    )
    close_time = fill_time + pd.Timedelta(days=1)
    broker.process_orders({"BTC": _frame(pd.DatetimeIndex([close_time]), [110]).iloc[0]})

    summary = write_event_log(broker.event_pipeline.events, tmp_path / "events.jsonl")
    coverage = validate_audit_coverage(
        event_summary=summary,
        routing_log_path=None,
        routing_required=False,
        trade_count=len(broker.trades),
        close_count=len(broker.close_events),
    )
    assert coverage["status"] == "ok"
    assert {"signal", "risk_decision", "order_intent", "order", "fill", "close"}.issubset(
        summary["event_type_counts"]
    )


def test_t2_10_anomaly_flags_flow_into_fill_context():
    index = pd.date_range("2024-01-01", periods=3, freq="D")
    frame = DataHandler.annotate_quality(_frame(index, [100, 101, 150]))
    assert bool(frame.loc[index[-1], "anomaly_spike"])
    broker = Broker(Portfolio(initial_capital=10_000.0), slippage=0.0)
    broker.submit_order("BTC", "buy", 1.0, price=150.0, timestamp=index[1])
    broker.process_orders({"BTC": frame.loc[index[-1]]})
    assert broker.trades[0]["data_quality_context"]["anomaly_spike"] is True


def test_t2_11_top_winners_and_losers_use_independent_data():
    index = pd.date_range("2024-01-01", periods=2, freq="D")
    frame = _frame(index, [100, 110])
    closed = [{
        "symbol": "BTC", "lot_id": "lot-1", "net_pnl": 10.0,
        "entry_time": index[0], "exit_time": index[1],
    }]
    passed = cross_verify_top_trades(closed, {"BTC": frame}, {"BTC": frame.copy()})
    missing = cross_verify_top_trades(closed, {"BTC": frame}, None)
    assert passed["status"] == "passed"
    assert missing["status"] == "unverified"


def test_t2_12_point_in_time_universe_retains_pre_delisting_history():
    index = pd.date_range("2024-01-01", periods=5, freq="D")
    universe = PointInTimeUniverse([
        UniverseMembership(
            "OLD", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-04")
        ),
        UniverseMembership("NEW", pd.Timestamp("2024-01-03")),
    ])
    filtered = universe.apply({"OLD": _frame(index, range(5)), "NEW": _frame(index, range(5))})
    assert list(filtered["OLD"].index) == list(index[:3])
    assert list(filtered["NEW"].index) == list(index[2:])
    assert universe.active_symbols("2024-01-02") == ["OLD"]


def test_t2_13_same_input_produces_identical_trade_equity_benchmark_report_digest():
    index = pd.date_range("2024-01-01", periods=80, freq="D")
    frame = _frame(index, pd.Series(range(80), index=index) + 100.0)
    first = BacktestEngine(run_id="same", random_slip=False).run(
        {"BTC/USDT": frame}, routing_log_enabled=False
    )
    second = BacktestEngine(run_id="same", random_slip=False).run(
        {"BTC/USDT": frame}, routing_log_enabled=False
    )
    assert deterministic_result_digest(first) == deterministic_result_digest(second)
