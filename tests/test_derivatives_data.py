"""Engineering fixtures, not evidence of live funding or strategy returns."""
from dataclasses import asdict, replace
import json
from unittest.mock import patch

import pandas as pd
import pytest

from core.accounts import AccountMode
from core.broker import Broker
from core.derivatives_data import (
    DerivativesBundle, LinearContractSpec, load_derivatives_bundle,
    merge_derivative_features, replay_funding_settlements, validate_settlements,
)
from core.portfolio import Portfolio


def spec(**kwargs):
    values = dict(venue="binance", contract_id="BTCUSDT", symbol="BTC/USDT:USDT",
                  base_currency="BTC", quote_currency="USDT", settlement_currency="USDT",
                  contract_multiplier=1.0, linear=True, market_type="perpetual",
                  funding_interval_hours=8)
    return LinearContractSpec(**(values | kwargs))


def settlements(rates=(.01, .02, -.01)):
    times = pd.date_range("2024-01-01T08:00:00Z", periods=len(rates), freq="8h")
    return pd.DataFrame({"contract_id": "BTCUSDT", "settlement_time": times,
                         "available_at": times + pd.Timedelta(minutes=1),
                         "funding_rate": rates, "mark_price": 100.0})


def positions(times=(), qtys=()):
    return pd.DataFrame({"timestamp": list(times), "position_contracts": list(qtys),
                         "contract_id": ["BTCUSDT"] * len(times)})


def replay(events=None, actual=None, **kwargs):
    return replay_funding_settlements(spec(), positions() if events is None else events,
                                     settlements() if actual is None else actual,
                                     start="2024-01-01T00:00:00Z", end="2024-01-02T00:00:00Z", **kwargs)


@pytest.mark.parametrize("qty,rate,expense", [(2, .01, 2), (-2, .01, -2), (2, -.01, -2), (-2, -.01, 2)])
def test_signed_funding_for_long_short_and_positive_negative_rates(qty, rate, expense):
    result = replay(initial_contracts=qty, actual=settlements((rate, rate, rate)))
    assert [row["amount"] for row in result["ledger"]] == pytest.approx([expense] * 3)
    assert result["net_funding_expense"] == pytest.approx(expense * 3)
    assert result["cash_change"] == pytest.approx(-expense * 3)
    assert result["equity_change_from_funding"] == result["cash_change"]


def test_daily_replay_consumes_all_three_8h_settlements_with_event_positions():
    events = positions(["2024-01-01T07:59Z", "2024-01-01T08:00Z", "2024-01-01T17:00Z"], [2, -3, 0])
    result = replay(events)
    assert [row["position_contracts"] for row in result["ledger"]] == [2, -3, 0]
    assert [row["amount"] for row in result["ledger"]] == pytest.approx([2, -6, 0])
    assert result["net_funding_expense"] == pytest.approx(-4)
    assert result["coverage"]["replayed_settlements"] == 3
    assert len({row["event_id"] for row in result["ledger"]}) == 3


def test_same_time_close_and_open_take_effect_after_settlement():
    close = positions(["2024-01-01T08:00Z"], [0])
    assert [row["position_contracts"] for row in replay(close, initial_contracts=2)["ledger"]] == [2, 0, 0]
    opening = positions(["2024-01-01T08:00Z"], [2])
    assert [row["position_contracts"] for row in replay(opening)["ledger"]] == [0, 2, 2]


def test_multiplier_is_explicit_contract_to_base_conversion():
    result = replay_funding_settlements(spec(contract_multiplier=.01), positions(), settlements(),
                                      start="2024-01-01", end="2024-01-02", initial_contracts=200)
    assert result["ledger"][0]["base_quantity"] == 2
    assert result["ledger"][0]["amount"] == pytest.approx(2)


def test_replay_is_deterministic_without_input_mutation_and_range_can_be_chunked():
    actual, events = settlements(), positions(["2024-01-01T12:00Z"], [2])
    saved = actual.copy(deep=True)
    whole = replay(events, actual, initial_contracts=1)
    assert replay(events, actual, initial_contracts=1) == whole
    pd.testing.assert_frame_equal(actual, saved)
    first = replay_funding_settlements(spec(), positions(), actual, start="2024-01-01",
                                      end="2024-01-01T08:00Z", initial_contracts=1)
    second = replay_funding_settlements(spec(), events, actual, start="2024-01-01T08:00Z",
                                       end="2024-01-02", initial_contracts=1)
    assert first["ledger"] + second["ledger"] == whole["ledger"]


@pytest.mark.parametrize("bad", ["gap", "duplicate", "nan", "identity", "early_available", "off_grid"])
def test_unknown_or_ambiguous_costs_fail_closed(bad):
    actual = settlements()
    if bad == "gap":
        actual = actual.drop(index=1)
    elif bad == "duplicate":
        actual = pd.concat([actual, actual.iloc[[0]]])
    elif bad == "nan":
        actual.loc[1, "funding_rate"] = float("nan")
    elif bad == "identity":
        actual.loc[1, "contract_id"] = "ETHUSDT"
    elif bad == "early_available":
        actual.loc[1, "available_at"] = pd.Timestamp("2024-01-01T15:00Z")
    else:
        actual.loc[1, "settlement_time"] = pd.Timestamp("2024-01-01T15:00Z")
    with pytest.raises(ValueError):
        replay(actual=actual)


@pytest.mark.parametrize("changes", [{"linear": False}, {"settlement_currency": "BTC"},
                                   {"market_type": "future"}, {"contract_multiplier": 0},
                                   {"symbol": "BTC/USDT"}, {"funding_interval_hours": float("nan")}])
def test_unsupported_or_incomplete_contracts_are_rejected(changes):
    with pytest.raises(ValueError):
        spec(**changes)


def write_inputs(tmp_path, observations=True):
    contract = tmp_path / "contract.json"
    actual = tmp_path / "funding.csv"
    obs = tmp_path / "observations.csv"
    contract.write_text(json.dumps(asdict(spec())), encoding="utf-8")
    settlements().to_csv(actual, index=False)
    pd.DataFrame({"contract_id": ["BTCUSDT", "BTCUSDT"],
                  "observed_at": ["2024-01-01T07:00Z", "2024-01-01T09:00Z"],
                  "available_at": ["2024-01-01T08:30Z", "2024-01-01T09:30Z"],
                  "open_interest": [1000, 2000], "mark_price": [100, 101],
                  "index_price": [99, 100], "predicted_funding_rate": [.004, .009]}).to_csv(obs, index=False)
    return str(contract), str(actual), str(obs) if observations else None


def bars(times):
    return pd.DataFrame({"open": 100., "high": 102., "low": 99., "close": 101., "volume": 1000.},
                         index=pd.to_datetime(times))


def test_features_use_publication_time_and_never_overwrite_cost_rate(tmp_path):
    bundle = load_derivatives_bundle(*write_inputs(tmp_path))
    frame = bars(["2024-01-01T08:00Z", "2024-01-01T08:15Z", "2024-01-01T08:30Z", "2024-01-01T10:00Z"])
    result = merge_derivative_features(frame, bundle)
    assert "funding_rate" not in result
    assert pd.isna(result["last_settled_funding_rate"].iloc[0])
    assert result["last_settled_funding_rate"].iloc[1] == .01
    assert result["open_interest"].iloc[:2].isna().all()
    assert result["open_interest"].iloc[2:].tolist() == [1000, 2000]
    assert result["predicted_funding_rate"].iloc[2:].tolist() == [.004, .009]
    assert result["derivative_index_price"].iloc[2:].tolist() == [99, 100]
    assert result.attrs["derivatives"]["provenance"]["funding"]["sha256"]
    assert result["funding_settlement_mode"].eq("event_replay_required").all()


def test_future_mutation_does_not_change_earlier_features_and_stale_is_missing(tmp_path):
    bundle = load_derivatives_bundle(*write_inputs(tmp_path))
    frame = bars(["2024-01-01T08:30Z", "2024-01-01T08:45Z", "2024-01-03T00:00Z"])
    first = merge_derivative_features(frame, bundle, max_age="2h")
    later = bundle.observations.copy()
    later.loc[1, "open_interest"] = 999999
    mutated = replace(bundle, observations=later)
    second = merge_derivative_features(frame, mutated, max_age="2h")
    pd.testing.assert_frame_equal(first.iloc[:2], second.iloc[:2])
    assert pd.isna(first["open_interest"].iloc[-1])
    assert first["open_interest_stale"].iloc[-1]


def test_empty_funding_preserves_unknown_features_and_replay_refuses_costs(tmp_path):
    files = write_inputs(tmp_path, observations=False)
    settlements().iloc[:0].to_csv(files[1], index=False)
    bundle = load_derivatives_bundle(*files)
    result = merge_derivative_features(bars(["2024-01-01T08:00Z"]), bundle)
    assert result["last_settled_funding_rate"].isna().all()
    assert bundle.quality["funding_status"] == "missing"
    with pytest.raises(ValueError, match="missing funding"):
        replay(actual=bundle.settlements)


def test_observation_clock_and_contract_identity_are_checked(tmp_path):
    files = write_inputs(tmp_path)
    observations = pd.read_csv(files[2])
    observations.loc[0, "available_at"] = "2024-01-01T06:00Z"
    observations.to_csv(files[2], index=False)
    with pytest.raises(ValueError, match="available before"):
        load_derivatives_bundle(*files)
    with pytest.raises(ValueError, match="contract identity"):
        spec().validate_request("BTC/USDT", "binance", "perpetual")


def test_main_passes_market_type_and_explicit_symbol_to_fetcher(tmp_path):
    from main import get_data
    contract, actual, obs = write_inputs(tmp_path)
    with patch("main.DataFetcher") as fetcher:
        fetcher.return_value.fetch_ccxt.return_value = bars(["2024-01-01T10:00Z"])
        result = get_data("BTC/USDT:USDT", "2024-01-01", "2024-01-02", source="ccxt",
                          market_type="perpetual", derivatives_contract_file=contract,
                          derivatives_funding_file=actual, derivatives_observations_file=obs)
        call = fetcher.return_value.fetch_ccxt.call_args
        assert call.args[0] == "BTC/USDT:USDT"
        assert call.kwargs["market_type"] == "perpetual"
        assert result["open_interest"].iloc[0] == 2000
    with pytest.raises(ValueError, match="require market_type"):
        get_data("BTC/USDT:USDT", "2024-01-01", "2024-01-02", derivatives_contract_file=contract)
    with pytest.raises(ValueError, match="contract identity"):
        get_data("ETH/USDT:USDT", "2024-01-01", "2024-01-02", market_type="perpetual", derivatives_contract_file=contract)


def test_bar_engine_cannot_apply_daily_end_position_to_three_settlements():
    portfolio = Portfolio(1000, account_mode=AccountMode.PERPETUAL)
    portfolio.update_position("BTC", 2, 100)
    broker = Broker(portfolio, timeframe="1d")
    bar = pd.Series({"close": 100, "funding_rate": .01}, name=pd.Timestamp("2024-01-01T08:00Z"))
    with pytest.raises(ValueError, match="exceeds funding interval"):
        broker.accrue_carry({"BTC": bar})
    assert portfolio.financing_ledger == []


def test_legacy_funding_cannot_replay_older_bucket_twice():
    portfolio = Portfolio(1000, account_mode=AccountMode.PERPETUAL)
    portfolio.update_position("BTC", 2, 100)
    broker = Broker(portfolio)
    def one(at):
        return {"BTC": pd.Series({"close": 100, "funding_rate": .01}, name=pd.Timestamp(at))}
    assert len(broker.accrue_carry(one("2024-01-01T16:00Z"))) == 1
    assert broker.accrue_carry(one("2024-01-01T08:00Z")) == []
    assert broker.accrue_carry(one("2024-01-01T16:00Z")) == []
    assert portfolio.cumulative_financing_cost == pytest.approx(2)
