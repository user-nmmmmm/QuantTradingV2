"""Hand-built input facts for fixed cross-market review boundaries and identity."""
import hashlib
import json

import pandas as pd
import pytest

from scripts.run_strategy_review_cross_market import (
    SYMBOLS, jobs, load_inputs, resolved_parameters, select_closed_period,
)


def candles(timeframe="4h", start="2022-01-01", count=106):
    delta = pd.Timedelta(hours=4) if timeframe == "4h" else pd.Timedelta(days=1)
    index = pd.date_range(pd.Timestamp(start, tz="UTC") - 100 * delta, periods=count, freq=delta)
    return pd.DataFrame({"open": 100.0, "high": 110.0, "low": 90.0, "close": 101.0, "volume": 7.0}, index=index)


def fixture_streams(path, *, timeframe="4h"):
    frame = candles(timeframe)
    frame.index.name = "timestamp"
    for symbol in SYMBOLS:
        name = f"binance_{symbol.replace('/', '')}_{timeframe}"
        csv_path = path / f"{name}.csv"
        frame.to_csv(csv_path)
        manifest = dict(venue="binance", symbol=symbol, timeframe=timeframe, market_type="spot",
            substitution=None, timezone="UTC", identity={"protocol_sha256": "protocol"},
            csv_path=csv_path.name, csv_sha256=hashlib.sha256(csv_path.read_bytes()).hexdigest(),
            status="evidence_incomplete", gaps=[{"timestamp": "2021-09-29", "missing_bars": 1}],
            failures=[{"reason": "early warmup shortened candle"}], endpoint="official-binance-fixture")
        (path / f"{name}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return dict(name="fixture", venue="binance", timeframe=timeframe, symbols=list(SYMBOLS),
                start="2022-01-01", end_exclusive="2022-01-02")


def test_registered_matrix_has_exact_eight_fixed_jobs():
    matrix = jobs()
    assert len(matrix) == len({row["name"] for row in matrix}) == 8
    assert {(row["venue"], row["timeframe"], row["period"]) for row in matrix} == {
        (venue, timeframe, period) for venue in ("binance", "okx")
        for timeframe in ("1d", "4h") for period in ("historical", "recent_drift")}
    assert all(tuple(row["symbols"]) == SYMBOLS and row["warmup_bars"] == 100 for row in matrix)


def test_4h_split_has_100_warmup_and_six_closed_period_bars():
    frame = candles(count=107)
    selected = select_closed_period(frame, timeframe="4h", start="2022-01-01",
                                    end_exclusive="2022-01-02", asof="2022-01-02T01:00Z")
    assert len(selected) == 106
    assert selected.index[-1] == pd.Timestamp("2022-01-01T20:00Z")
    assert selected.index[100] == pd.Timestamp("2022-01-01T00:00Z")


def test_timezone_offset_is_normalized_before_closed_bar_selection():
    frame = candles(count=107)
    frame.index = frame.index.tz_convert("Asia/Singapore")
    selected = select_closed_period(frame, timeframe="4h", start="2022-01-01T08:00+08:00",
                                    end_exclusive="2022-01-02T08:00+08:00", asof="2022-01-02T08:00+08:00")
    assert str(selected.index.tz) == "UTC" and len(selected) == 106


def test_unclosed_required_bar_is_missing_evidence():
    with pytest.raises(ValueError, match="Missing evaluation bars"):
        select_closed_period(candles(), timeframe="4h", start="2022-01-01",
                             end_exclusive="2022-01-02", asof="2022-01-01T23:59:59Z")


def test_daily_boundary_includes_only_the_previous_complete_day():
    selected = select_closed_period(candles("1d", count=103), timeframe="1d", start="2022-01-01",
                                    end_exclusive="2022-01-02", asof="2022-01-02")
    assert len(selected) == 101 and selected.index[-1] == pd.Timestamp("2022-01-01T00:00Z")


@pytest.mark.parametrize("missing", [10, 103])
def test_missing_warmup_or_active_bar_is_never_filled(missing):
    frame = candles().drop(candles().index[missing])
    with pytest.raises(ValueError, match="warmup|Missing evaluation"):
        select_closed_period(frame, timeframe="4h", start="2022-01-01",
                             end_exclusive="2022-01-02", asof="2022-01-02")


@pytest.mark.parametrize("change", ["duplicate", "off_boundary", "nan", "negative_volume", "bad_ohlc"])
def test_invalid_selected_candles_are_rejected(change):
    frame = candles()
    if change == "duplicate":
        frame = pd.concat([frame, frame.tail(1)])
    elif change == "off_boundary":
        frame.index = frame.index + pd.Timedelta(minutes=1)
    elif change == "nan":
        frame.iloc[-1, 0] = float("nan")
    elif change == "negative_volume":
        frame.iloc[-1, 4] = -1
    else:
        frame.iloc[-1, 1] = 1
    with pytest.raises(ValueError):
        select_closed_period(frame, timeframe="4h", start="2022-01-01",
                             end_exclusive="2022-01-02", asof="2022-01-02")


def test_globally_incomplete_feed_is_allowed_only_when_selected_support_is_complete(tmp_path):
    job = fixture_streams(tmp_path)
    frames, evidence = load_inputs(tmp_path, job, asof="2022-01-02", protocol_hash="protocol")
    assert tuple(frames) == SYMBOLS
    assert all(row["selected_status"] == "complete" and row["source_status"] == "evidence_incomplete"
               and row["source_failures"] for row in evidence.values())


@pytest.mark.parametrize("field,value", [
    ("venue", "okx"), ("symbol", "BTC/USD"), ("market_type", "swap"),
    ("csv_path", "okx_BTCUSDT_4h.csv"), ("substitution", "okx"),
])
def test_no_venue_symbol_market_or_path_substitution(tmp_path, field, value):
    job = fixture_streams(tmp_path)
    path = tmp_path / "binance_BTCUSDT_4h.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch|substitution"):
        load_inputs(tmp_path, job, asof="2022-01-02", protocol_hash="protocol")


def test_missing_fixed_symbol_and_input_checksum_fail_closed(tmp_path):
    job = fixture_streams(tmp_path)
    path = tmp_path / "binance_LTCUSDT_4h.csv"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        load_inputs(tmp_path, job, asof="2022-01-02", protocol_hash="protocol")
    (tmp_path / "binance_LTCUSDT_4h.manifest.json").unlink()
    with pytest.raises(ValueError, match="Missing fixed stream"):
        load_inputs(tmp_path, job, asof="2022-01-02", protocol_hash="protocol")


def test_cash_spot_parameters_keep_common_costs_and_official_governance():
    base = dict(state={"stability_period": 5}, router={"cooldown_bars": 2},
        strategy_governance={"TrendBreakout": "paused_revalidation"}, execution={"slippage_bps": 5},
        account={"mode": "spot_margin"}, risk={"max_leverage": 3}, data={}, backtest={})
    result = resolved_parameters(base, dict(name="fixture", venue="okx", timeframe="4h"))
    assert base["account"]["mode"] == "spot_margin"
    assert result["account"]["mode"] == "spot" and result["account"]["initial_margin_rate"] == 1
    assert result["risk"]["max_leverage"] == 1
    assert result["execution"]["fee_schedule"]["venue"] == "okx"
    assert result["execution"]["commission_rate_maker"] == result["execution"]["commission_rate_taker"] == 0.001
    assert result["research"]["trend_breakout_parameters"] == {"entry_window": 20, "exit_window": 10}
    assert result["strategy_governance"] == base["strategy_governance"]
