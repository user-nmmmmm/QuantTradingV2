"""Roadmap FIX-04/05/06/07/09: causal and fail-closed input boundaries."""
import json
import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from analysis.walk_forward import WalkForwardConfig, _concat_unique, _run_window, candidate_warmup, run_walk_forward
from core.data_fetcher import DataFetcher, IncompleteDataError, timeframe_delta
from core.factors.support_resistance import SupportResistanceFactors
from core.factors.volume import VolumeFactors
from core.universe import PointInTimeUniverse, UniverseMembership, normalize_symbol
from scripts import fetch_binance_data as download
from scripts import run_backtest_matrix as matrix
from scripts.run_expanded_universe_backtest import fetch_symbol


def prices(index):
    return pd.DataFrame({"open": 10., "high": 12., "low": 9., "close": 11., "volume": 100.}, index=index)


@pytest.mark.parametrize("timeframe", ["5m", "15m"])
def test_cross_year_download_bounds_pages_and_covers_every_bar(timeframe):
    fetcher = MagicMock()
    def fetch(symbol, **kwargs):
        index = pd.date_range(kwargs["start_date"], pd.Timestamp(kwargs["end_date"]) + pd.Timedelta(days=1),
                              freq=timeframe_delta(timeframe), inclusive="left")
        assert len(index) < 10000
        frame = prices(index)
        frame.attrs.update(provider="binance", timeframe=timeframe, market_type="spot", symbol=symbol)
        return frame
    fetcher.fetch_ccxt.side_effect = fetch
    frame = download.fetch_ohlcv(fetcher, "BTC/USDT", timeframe, "2023-12-01", "2024-05-01", "binance")
    assert fetcher.fetch_ccxt.call_count > 1
    assert download.validate_coverage(frame, "2023-12-01", "2024-05-01", timeframe)["status"] == "complete"
    assert not frame.index.has_duplicates


@pytest.mark.parametrize("provider,period,termination", [("yahoo", "1d", "requested_end"),
                                                        ("binance", "1d", "requested_end"),
                                                        ("binance", "5m", "safety_limit")])
def test_source_period_and_truncation_are_rejected(provider, period, termination):
    frame = prices(pd.date_range("2024-01-01", periods=3, freq="5min"))
    frame.attrs.update(provider=provider, timeframe=period, pagination_termination=termination,
                       market_type="spot", symbol="BTC/USDT")
    fetcher = MagicMock()
    fetcher.fetch_ccxt.return_value = frame
    with pytest.raises(ValueError):
        download.fetch_ohlcv(fetcher, "BTC/USDT", "5m", "2024-01-01", "2024-01-01", "binance")


def test_missing_tail_or_gap_is_not_complete():
    frame = prices(pd.date_range("2024-01-01", periods=24, freq="h"))
    for broken in [frame.iloc[:-1], frame.drop(frame.index[3])]:
        with pytest.raises(ValueError, match="Incomplete"):
            download.validate_coverage(broken, "2024-01-01", "2024-01-01", "1h")


def test_matrix_stops_all_backtests_when_refresh_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(matrix.subprocess, "run", MagicMock(side_effect=__import__("subprocess").CalledProcessError(1, "download")))
    run = MagicMock()
    monkeypatch.setattr(matrix, "run_one", run)
    assert matrix.main(["--symbols", "BTC/USDT", "--timeframes", "5m", "--windows", "recent"]) == 1
    run.assert_not_called()
    failure = next(tmp_path.rglob("refresh_failure.json"))
    assert json.loads(failure.read_text())["backtests_started"] == 0


def test_matrix_refuses_legacy_cache_even_when_download_exits_success(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    directory = tmp_path / "data/binance/1d"
    directory.mkdir(parents=True)
    (directory / "_manifest.json").write_text(json.dumps({"exchange": "binance", "timeframe": "1d", "symbols": {}}))
    monkeypatch.setattr(matrix.subprocess, "run", MagicMock(return_value=SimpleNamespace(returncode=0)))
    with pytest.raises(ValueError, match="unverified"):
        matrix.fetch_for_timeframe(["BTC/USDT"], "1d", "2024-01-01")


def test_verified_4h_cache_is_repeatable_without_network_and_detects_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix, "REPO_ROOT", tmp_path)
    directory = tmp_path / "data/binance/4h"
    directory.mkdir(parents=True)
    path = directory / "BTC_USDT.csv"
    frame = prices(pd.date_range("2024-01-01", periods=12, freq="4h"))
    frame.to_csv(path)
    record = {"provider": "binance", "timeframe": "4h", "file": path.name, "market_type": "spot", "symbol": "BTC-USDT",
              "requested_start": "2024-01-01", "requested_end": "2024-01-02",
              "first": frame.index.min().isoformat(), "last": frame.index.max().isoformat(),
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "coverage": download.validate_coverage(frame, "2024-01-01", "2024-01-02", "4h")}
    manifest = {"schema_version": "binance-cache/v2", "provider": "binance", "timeframe": "4h", "market_type": "spot",
                "failures": [], "symbols": {"BTC/USDT": record}}
    (directory / "_manifest.json").write_text(json.dumps(manifest))
    network = MagicMock(side_effect=AssertionError("unexpected network"))
    monkeypatch.setattr(matrix.subprocess, "run", network)
    assert matrix.verify_cache(["BTC/USDT"], "4h") == matrix.verify_cache(["BTC/USDT"], "4h")
    assert matrix.verify_cache(["BTC/USDT"], "4h", start="2024-01-01", end="2024-01-02") == manifest
    with pytest.raises(ValueError, match="requested start"):
        matrix.verify_cache(["BTC/USDT"], "4h", start="2023-01-01", end="2024-01-02")
    with pytest.raises(ValueError, match="requested end"):
        matrix.verify_cache(["BTC/USDT"], "4h", start="2024-01-01", end="2024-01-03")
    backtest = MagicMock()
    monkeypatch.setattr(matrix, "run_one", backtest)
    assert matrix.main(["--skip-fetch", "--symbols", "BTC/USDT", "--timeframes", "4h", "--windows", "recent"]) == 1
    backtest.assert_not_called()
    network.assert_not_called()
    path.write_text("changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        matrix.verify_cache(["BTC/USDT"], "4h")


@pytest.mark.parametrize("wrong_field,wrong_value", [("provider", "yahoo"), ("symbol", "ETH-USDT"),
                                                     ("timeframe", "4h"), ("market_type", "perpetual")])
def test_wrong_identity_old_cache_cannot_be_relabelled_by_incremental_refresh(tmp_path, monkeypatch, wrong_field, wrong_value):
    directory = tmp_path / "1d"
    directory.mkdir()
    path = directory / "BTC_USDT.csv"
    old = prices(pd.date_range("2024-01-01", periods=2))
    old["close"] = 9999.
    old.to_csv(path)
    original_bytes = path.read_bytes()
    record = {"provider": "binance", "timeframe": "1d", "market_type": "spot", "symbol": "BTC-USDT",
              "file": path.name, "sha256": hashlib.sha256(original_bytes).hexdigest(), "coverage": {"status": "complete"}}
    record[wrong_field] = wrong_value
    manifest = {"schema_version": "binance-cache/v2", "provider": "binance", "timeframe": "1d",
                "market_type": "spot", "symbols": {"BTC/USDT": record}}
    (directory / "_manifest.json").write_text(json.dumps(manifest))
    fetch = MagicMock(return_value=prices(pd.date_range("2024-01-02", periods=1)))
    monkeypatch.setattr(download, "fetch_ohlcv", fetch)
    assert download.main(["--symbols", "BTC/USDT", "--timeframe", "1d", "--start", "2024-01-01",
                          "--end", "2024-01-02", "--out-root", str(tmp_path)]) == 1
    assert fetch.call_args.args[3] == "2024-01-01", "unverified old data must not advance the refresh cursor"
    assert path.read_bytes() == original_bytes


def test_force_refresh_bypasses_even_trusted_old_data(tmp_path, monkeypatch):
    directory = tmp_path / "1d"
    directory.mkdir()
    path = directory / "BTC_USDT.csv"
    old = prices(pd.date_range("2024-01-01", periods=2))
    old["close"] = 9999.
    old.to_csv(path)
    record = {"provider": "binance", "timeframe": "1d", "market_type": "spot", "symbol": "BTC-USDT",
              "file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "coverage": {"status": "complete"}}
    (directory / "_manifest.json").write_text(json.dumps({"schema_version": "binance-cache/v2", "provider": "binance",
        "timeframe": "1d", "market_type": "spot", "symbols": {"BTC/USDT": record}}))
    fetch = MagicMock(return_value=prices(pd.date_range("2024-01-01", periods=2)))
    monkeypatch.setattr(download, "fetch_ohlcv", fetch)
    assert download.main(["--no-incremental", "--symbols", "BTC/USDT", "--timeframe", "1d",
                          "--start", "2024-01-01", "--end", "2024-01-02", "--out-root", str(tmp_path)]) == 0
    assert fetch.call_args.args[3] == "2024-01-01"
    assert list(pd.read_csv(path)["close"]) == [11., 11.]


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_invalid_page_limit_rejected_before_network(limit):
    with patch("ccxt.binance") as constructor:
        with pytest.raises(ValueError, match="positive integer"):
            DataFetcher(proxy_url=None).fetch_ccxt("BTC/USDT", limit=limit)
        constructor.assert_not_called()


def candle_pages(index):
    rows = [[int(timestamp.timestamp() * 1000), 10., 12., 9., 11., 100.] for timestamp in index]
    return [rows[offset:offset+1000] for offset in range(0, len(rows), 1000)]


def test_ordinary_cli_rejects_truncated_ccxt_download_without_source_fallback():
    import main
    index = pd.date_range("2024-01-01", periods=10000, freq="min", tz="UTC")
    with patch("ccxt.binance") as constructor, patch.object(DataFetcher, "_fallback_to_yahoo_crypto") as fallback:
        constructor.return_value.fetch_ohlcv.side_effect = candle_pages(index)
        with pytest.raises(IncompleteDataError, match="10000"):
            main.get_data("BTC/USDT", "2024-01-01", "2024-01-10", "ccxt", days=1000, timeframe="1m")
        fallback.assert_not_called()


def test_explicit_non_strict_fetch_keeps_truncation_diagnostic_and_exact_limit_is_complete():
    index = pd.date_range("2024-01-01", periods=10000, freq="min", tz="UTC")
    fetcher = DataFetcher(proxy_url=None, data_timezone="UTC")
    with patch("ccxt.binance") as constructor:
        constructor.return_value.fetch_ohlcv.side_effect = candle_pages(index)
        partial = fetcher.fetch_ccxt("BTC/USDT", "1m", "2024-01-01", "2024-01-10", exchange_id="binance", strict=False)
        assert len(partial) == 10000 and partial.attrs["pagination_termination"] == "safety_limit"
        exact = pd.date_range("1990-01-01", periods=10000, freq="D", tz="UTC")
        constructor.return_value.fetch_ohlcv.side_effect = candle_pages(exact)
        complete = fetcher.fetch_ccxt("BTC/USDT", "1d", "1990-01-01", exact[-1].strftime("%Y-%m-%d"), exchange_id="binance")
        assert len(complete) == 10000 and complete.attrs["pagination_termination"] == "requested_end"


def test_matrix_json_has_priority_and_legacy_infinity_parses(tmp_path):
    (tmp_path / "report.txt").write_text("Profit Factor (PF) : inf\nSharpe Ratio (Sharpe) : nan\nTotal Return (Return) : 1.2e-3\n")
    assert np.isinf(matrix.parse_report(tmp_path)["profit_factor"])
    assert np.isnan(matrix.parse_report(tmp_path)["sharpe"])
    assert matrix.parse_report(tmp_path)["total_return"] == .0012
    (tmp_path / "metrics.json").write_text(json.dumps({"metrics": {"ProfitFactor": None, "TotalReturn": .4}}))
    assert matrix.parse_report(tmp_path) == {"profit_factor": None, "total_return": .4}
    assert matrix.display_metric(None, ".2f") == "N/A"


def oi_rows(hours):
    base = pd.Timestamp("2024-01-01")
    return [{"timestamp": int((base + pd.Timedelta(hours=hour)).timestamp() * 1000), "openInterestAmount": 0.}
            for hour in hours]


@pytest.mark.parametrize("failure", [None, "repeat", "empty", "error", "limited_history"])
def test_open_interest_pages_and_reports_actual_coverage(failure):
    first = oi_rows(range(0, 12))
    second = oi_rows(range(12, 24))
    pages = [first, second] if failure is None else {
        "repeat": [first, first], "empty": [first, []], "error": [first, RuntimeError("unavailable")],
        "limited_history": [oi_rows(range(12, 24))],
    }[failure]
    with patch("ccxt.binance") as constructor:
        exchange = constructor.return_value
        exchange.has = {"fetchOpenInterestHistory": True}
        exchange.fetchOpenInterestHistory.side_effect = pages
        frame = DataFetcher(proxy_url=None, data_timezone="UTC").fetch_open_interest_history(
            "BTC/USDT:USDT", start_date="2024-01-01", end_date="2024-01-01", limit=12)
    assert frame.attrs["coverage"]["status"] == ("complete" if failure is None else "incomplete")
    assert not frame.index.has_duplicates
    assert (frame.open_interest == 0).all()
    if failure is None:
        assert len(frame) == 24
        assert exchange.fetchOpenInterestHistory.call_args.kwargs["since"] == first[-1]["timestamp"] + 1


def test_unclosed_daily_request_is_refused_before_network(tmp_path):
    with patch("scripts.run_expanded_universe_backtest.requests.Session") as session:
        with pytest.raises(ValueError, match="unclosed"):
            fetch_symbol("BTC/USDT", tmp_path, "2026-08-01", "2026-08-31", as_of="2026-08-31T23:59:59Z")
        session.assert_not_called()


def test_just_closed_daily_bar_retains_close_time(tmp_path):
    index = pd.date_range("2026-08-01", periods=31, tz="UTC")
    rows = [[int(t.timestamp()*1000), "10", "12", "9", "11", "100", int((t+pd.Timedelta(days=1)).timestamp()*1000)-1]
            for t in index]
    with patch("scripts.run_expanded_universe_backtest.requests.Session") as session:
        session.return_value.__enter__.return_value.get.return_value.json.return_value = rows
        session.return_value.__enter__.return_value.get.return_value.url = "https://data-api.binance.vision/api/v3/klines"
        entry = fetch_symbol("BTC/USDT", tmp_path, "2026-08-01", "2026-08-31", as_of="2026-09-01T00:00:00Z")
    saved = pd.read_csv(tmp_path / entry["file"])
    assert len(saved) == 31 and "close_time" in saved
    assert pd.Timestamp(saved.iloc[-1].close_time) == pd.Timestamp("2026-08-31T23:59:59.999")


@pytest.mark.parametrize("factor,column", [(SupportResistanceFactors.SWING_HIGH, "high"),
                                          (SupportResistanceFactors.SWING_LOW, "low")])
def test_swing_only_available_after_confirmation_and_prefix_invariant(factor, column):
    values = [1., 2., 6., 3., 1., 7., 2., 1.]
    frame = pd.DataFrame({column: values if column == "high" else [-x for x in values]})
    full = factor(frame, order=2)
    assert full.iloc[:4].isna().all()
    assert full.iloc[4] == (6 if column == "high" else -6)
    for stop in range(1, len(frame)):
        pd.testing.assert_series_equal(factor(frame.iloc[:stop], order=2), full.iloc[:stop])


@pytest.mark.parametrize("volume", [0., np.nan, -1.])
def test_invalid_volume_never_manufactures_poc(volume):
    frame = prices(pd.date_range("2024-01-01", periods=4))
    frame["volume"] = volume
    assert all(series.isna().all() for series in VolumeFactors.VOLUME_PROFILE(frame, window=3, bins=2))


def test_positive_volume_profile_matches_two_bucket_hand_calculation():
    frame = prices(pd.date_range("2024-01-01", periods=2))
    frame["low"], frame["high"] = 0., 2.
    poc, vah, val = VolumeFactors.VOLUME_PROFILE(frame, window=2, bins=2)
    assert (poc.iloc[-1], vah.iloc[-1], val.iloc[-1]) == (.5, 2., 0.)


def test_pit_aliases_exit_on_last_eligible_bar_and_keep_market_identity():
    universe = PointInTimeUniverse([UniverseMembership("btc/usdt", pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-05"))])
    output = universe.apply({"BTC-USDT": prices(pd.date_range("2024-01-01", periods=7))})["BTC-USDT"]
    assert list(output.index) == list(pd.date_range("2024-01-02", "2024-01-04"))
    assert output.scheduled_exit.sum() == 1 and output.scheduled_exit.iloc[-1]
    assert normalize_symbol("BTC/USDT:USDT") != normalize_symbol("BTC/USDT")


def test_pit_conflicting_aliases_and_missing_final_exit_bar_refused():
    with pytest.raises(ValueError, match="duplicate"):
        PointInTimeUniverse([UniverseMembership(key, pd.Timestamp("2024-01-01")) for key in ["BTC/USDT", "BTC-USDT"]])
    universe = PointInTimeUniverse([UniverseMembership("BTC/USDT", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-05"))])
    with pytest.raises(ValueError, match="Missing final"):
        universe.apply({"BTC-USDT": prices(pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-06", "2024-01-07"]))})


def test_walk_forward_rejects_overlap_and_duplicate_return_samples():
    with pytest.raises(ValueError, match="step"):
        WalkForwardConfig(train_size=120, validation_size=30, test_size=60, step=30)
    part = pd.Series([.1, .2], index=pd.date_range("2024-01-01", periods=2))
    with pytest.raises(ValueError, match="unique"):
        _concat_unique([part, part])


def test_candidate_100_bar_warmup_gives_test_60_bars_without_prefix_returns(monkeypatch):
    frame = prices(pd.date_range("2024-01-01", periods=260, tz="UTC"))
    seen = []
    class Engine:
        def __init__(self, **kwargs):
            assert kwargs["warmup_period"] == 100
        def run(self, data, **kwargs):
            seen.append(data["BTC-USDT"])
            return {"equity_curve": pd.DataFrame({"equity": np.arange(len(data["BTC-USDT"])) + 1000.}, index=data["BTC-USDT"].index), "trades": []}
    monkeypatch.setattr("analysis.walk_forward.BacktestEngine", Engine)
    factory = lambda: {"long": SimpleNamespace(entry_window=100, exit_window=20)}
    assert candidate_warmup(factory, 30) == 100
    config = WalkForwardConfig(train_size=120, validation_size=30, test_size=60)
    result = _run_window({"BTC-USDT": frame}, factory, start=frame.index[150], end=frame.index[209],
                         warmup_start=frame.index[50], config=config, warmup_period=100)
    assert len(seen[0]) == 160
    assert len(result["returns"]) == 60
    assert result["returns"].index.min() == frame.index[150].tz_localize(None)


def test_insufficient_candidate_history_is_reported_without_engine_run(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr("analysis.walk_forward.BacktestEngine", engine)
    factory = lambda: {"long": SimpleNamespace(entry_window=100)}
    result = run_walk_forward({"BTC-USDT": prices(pd.date_range("2024-01-01", periods=150))},
                              {"long": factory}, WalkForwardConfig(train_size=40, validation_size=30, test_size=60))
    assert result["skipped_windows"][0]["required_bars"] == 100
    engine.assert_not_called()
