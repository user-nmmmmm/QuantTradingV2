"""Hand-calculated marked-equity and independent execution fact contracts."""

import copy
import json

import numpy as np
import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.metrics.attribution import calculate_attribution, calculate_group_drawdown_contribution
from core.metrics.execution import calculate_execution_quality


def group_facts():
    index = pd.date_range("2026-01-01", periods=4, tz="UTC")
    groups = pd.DataFrame({"trend": [60., 72., 100., 82.],
                           "cash_and_other": [40., 48., 58., 60.2]}, index=index)
    flows = pd.DataFrame({"trend": [0., 0., 40., 0.],
                          "cash_and_other": [0., 0., 10., 0.]}, index=index)
    return groups.sum(axis=1), groups, flows, flows.sum(axis=1)


def test_group_drawdown_links_actual_marked_paths_and_removes_external_deposit():
    account, groups, flows, external = group_facts()
    copies = [value.copy(deep=True) for value in (account, groups, flows, external)]
    result = calculate_group_drawdown_contribution(account, groups, flows, external)
    assert result["status"] == "ok"
    assert result["value"] == pytest.approx(-.19)
    assert [row["flow_neutral_nav"] for row in result["paths"]] == pytest.approx([1, 1.2, 1.08, .972])
    assert result["peak"] == account.index[1].isoformat()
    assert result["trough"] == account.index[3].isoformat()
    assert result["by_group"]["trend"]["contribution"] == pytest.approx(-.1 - .9 * 18 / 158)
    assert result["by_group"]["cash_and_other"]["contribution"] == pytest.approx(.9 * 2.2 / 158)
    assert sum(row["contribution"] for row in result["by_group"].values()) == pytest.approx(result["value"])
    for value, before in zip((account, groups, flows, external), copies):
        if isinstance(value, pd.DataFrame):
            pd.testing.assert_frame_equal(value, before)
        else:
            pd.testing.assert_series_equal(value, before)
    # Arbitrary closed PnL cannot affect marked-path attribution.
    attributed = calculate_attribution([{"net_pnl": 9_000_000}], account_equity=account,
                                      group_equity=groups, group_cashflows=flows, external_cashflows=external)
    assert attributed["group_drawdown_contribution"] == result


def test_internal_capital_transfer_is_not_group_profit_or_drawdown():
    index = pd.date_range("2026-01-01", periods=3)
    groups = pd.DataFrame({"a": [80, 30, 20], "b": [20, 70, 80]}, index=index)
    flows = pd.DataFrame({"a": [0, -50, -10], "b": [0, 50, 10]}, index=index)
    result = calculate_group_drawdown_contribution(groups.sum(axis=1), groups, flows, flows.sum(axis=1))
    assert result["value"] == 0
    assert all(row["contribution"] == 0 for row in result["by_group"].values())
    assert all(row["flow_neutral_nav"] == 1 for row in result["paths"])


@pytest.mark.parametrize("bad", ["clock", "duplicate_clock", "unsorted", "nan", "sum", "flow_sum",
                                  "opening_flow", "duplicate_group", "zero_capital", "column_order"])
def test_group_attribution_refuses_bad_facts(bad):
    account, groups, flows, external = group_facts()
    if bad == "clock":
        flows.index += pd.Timedelta(seconds=1)
    elif bad == "duplicate_clock":
        groups.index = [groups.index[0]] * len(groups)
    elif bad == "unsorted":
        groups = groups.iloc[::-1]
    elif bad == "nan":
        groups.iloc[2, 0] = np.nan
    elif bad == "sum":
        account.iloc[2] += 1
    elif bad == "flow_sum":
        external.iloc[2] += 1
    elif bad == "opening_flow":
        flows.iloc[0, 0] = 1
        external.iloc[0] = 1
    elif bad == "duplicate_group":
        groups.columns = ["a", "a"]
        flows.columns = ["a", "a"]
    elif bad == "zero_capital":
        groups.iloc[2] = 0
        account.iloc[2] = 0
    else:
        flows = flows[flows.columns[::-1]]
    result = calculate_group_drawdown_contribution(account, groups, flows, external)
    assert result["status"] == "invalid_input"
    assert result["value"] is None and result["by_group"] == {} and result["reason"]


def test_missing_cashflow_facts_cannot_be_inferred_as_zero():
    account, groups, flows, external = group_facts()
    assert calculate_group_drawdown_contribution(account, groups)["status"] == "not_modeled"
    assert calculate_group_drawdown_contribution(account, groups, flows, external,
                                                cashflow_timing="unknown")["status"] == "not_modeled"
    assert calculate_group_drawdown_contribution(*(x.iloc[:1] for x in (account, groups, flows, external)))["status"] == "insufficient_data"


def stamp(seconds):
    return pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(seconds=seconds)


def execution_facts(side="buy"):
    buy = side in {"buy", "cover"}
    def event(kind, seconds, **payload):
        return {"event_type": kind, "account_id": "paper", "occurred_at": stamp(seconds),
                "payload": {"client_order_id": "o1", **payload}}
    events = [event("order_intent", 0, action=side, requested_qty=10, reference_price=100,
                    symbol="BTC/USDT", quote_currency="USDT"),
              event("fill", 10, fill_id="f1", qty=4, price=101 if buy else 99),
              event("order", 20, status="canceled", requested_qty=10, filled_qty=4)]
    provenance = {"account_id": "paper", "symbol": "BTC/USDT", "quote_currency": "USDT",
                  "independent": True, "source_id": "venue-public-capture"}
    terminal = [{**provenance, "client_order_id": "o1", "price": 103 if buy else 97,
                 "reference_id": "terminal-snapshot-1", "occurred_at": stamp(30), "available_at": stamp(31)}]
    quotes = [{**provenance, "fill_id": "f1", "bid": 99, "ask": 101,
               "reference_id": "quote-1", "occurred_at": stamp(9), "available_at": stamp(9.5)}]
    return events, terminal, quotes


@pytest.mark.parametrize("side", ["buy", "cover", "sell", "short"])
def test_execution_costs_have_signed_units_independent_sources_and_replay_idempotence(side):
    events, terminal, quotes = execution_facts(side)
    result = calculate_execution_quality(events, terminal_valuations=terminal,
                                         independent_quotes=quotes, as_of=stamp(40))
    assert result["status"] == "ok"
    opportunity = result["opportunity_cost"]
    assert opportunity["status"] == "ok"
    assert opportunity["value"] == 300 and opportunity["unit"] == "bps"
    assert opportunity["amount"] == 18 and opportunity["quote_currency"] == "USDT"
    assert opportunity["reference_notional"] == 600
    assert opportunity["observations"][0]["unfilled_qty"] == 6
    assert result["independent_quote_shortfall_bps"]["value"] == 100
    assert result["independent_executable_quote_shortfall_bps"]["value"] == 0
    assert "not causal" in result["independent_quote_shortfall_bps"]["policy"]
    assert result == calculate_execution_quality(events * 2, terminal_valuations=terminal * 2,
                                                 independent_quotes=quotes * 2, as_of=stamp(40))
    json.dumps(result, allow_nan=False)


def test_opportunity_cost_can_be_negative_and_full_fill_is_true_zero():
    events, terminal, _ = execution_facts()
    terminal[0]["price"] = 95
    result = calculate_execution_quality(events, terminal_valuations=terminal, as_of=stamp(40))
    assert result["opportunity_cost"]["value"] == -500
    events[1]["payload"]["qty"] = 10
    events[2]["payload"].update(filled_qty=10, status="filled")
    assert calculate_execution_quality(events, terminal_valuations=[], as_of=stamp(40))["opportunity_cost"]["value"] == 0


@pytest.mark.parametrize("bad", ["not_independent", "future_available", "stale", "crossed", "nan",
                                  "wrong_symbol", "wrong_currency", "unknown_fill", "conflicting_replay",
                                  "missing_source", "naive_time"])
def test_independent_quotes_cannot_use_bad_or_future_facts(bad):
    events, _, quotes = execution_facts()
    quote = quotes[0]
    if bad == "not_independent":
        quote["independent"] = False
    elif bad == "future_available":
        quote["available_at"] = stamp(11)
    elif bad == "stale":
        quote["occurred_at"] = stamp(8)
    elif bad == "crossed":
        quote["bid"] = 102
    elif bad == "nan":
        quote["ask"] = np.nan
    elif bad == "wrong_symbol":
        quote["symbol"] = "ETH/USDT"
    elif bad == "wrong_currency":
        quote["quote_currency"] = "USD"
    elif bad == "unknown_fill":
        quote["fill_id"] = "other"
    elif bad == "conflicting_replay":
        quotes.append({**quote, "ask": 102})
    elif bad == "missing_source":
        quote.pop("source_id")
    else:
        quote["occurred_at"] = "2026-01-01"
    result = calculate_execution_quality(events, independent_quotes=quotes)
    assert result["status"] == "invalid_input"
    assert result["independent_quote_shortfall_bps"]["status"] == "invalid_input"
    assert result["independent_quote_shortfall_bps"]["value"] is None
    assert result["independent_executable_quote_shortfall_bps"]["value"] is None


@pytest.mark.parametrize("bad", ["before_terminal", "future_available", "open_order", "unknown_order",
                                  "future_order", "late_fill", "conflicting_replay"])
def test_terminal_valuation_needs_completed_order_and_explicit_cutoff(bad):
    events, terminal, _ = execution_facts()
    if bad == "before_terminal":
        terminal[0]["occurred_at"] = stamp(15)
    elif bad == "future_available":
        terminal[0]["available_at"] = stamp(41)
    elif bad == "open_order":
        events[-1]["payload"]["status"] = "accepted"
    elif bad == "unknown_order":
        terminal[0]["client_order_id"] = "unknown"
    elif bad == "future_order":
        events[-1]["occurred_at"] = stamp(41)
    elif bad == "late_fill":
        events[1]["occurred_at"] = stamp(21)
    else:
        terminal.append({**terminal[0], "price": 104})
    result = calculate_execution_quality(events, terminal_valuations=terminal, as_of=stamp(40))
    assert result["opportunity_cost"]["status"] == "invalid_input"
    assert result["opportunity_cost"]["value"] is None


def test_partial_missing_or_mixed_currency_market_facts_never_report_complete_aggregate():
    events, terminal, quotes = execution_facts()
    assert calculate_execution_quality(events)["opportunity_cost"]["value"] is None
    assert calculate_execution_quality(events, terminal_valuations=terminal)["opportunity_cost"]["status"] == "not_modeled"
    assert calculate_execution_quality(events, terminal_valuations=[], as_of=stamp(40))["opportunity_cost"]["status"] == "not_modeled"
    assert calculate_execution_quality(events, independent_quotes=[])["independent_quote_shortfall_bps"]["status"] == "not_modeled"
    extra_events, extra_terminal, extra_quotes = copy.deepcopy((events, terminal, quotes))
    for event in extra_events:
        event["payload"]["client_order_id"] = "o2"
        if "fill_id" in event["payload"]:
            event["payload"]["fill_id"] = "f2"
    extra_terminal[0]["client_order_id"] = "o2"
    extra_quotes[0]["fill_id"] = "f2"
    result = calculate_execution_quality(events + extra_events, independent_quotes=quotes)
    assert result["independent_quote_shortfall_bps"]["status"] == "not_modeled"
    extra_quotes[0]["quote_currency"] = "USD"
    result = calculate_execution_quality(events + extra_events, independent_quotes=quotes + extra_quotes)
    assert result["independent_quote_shortfall_bps"]["status"] == "invalid_input"


def test_independent_quote_currency_must_match_canonical_fill_when_intent_lacks_currency():
    events, terminal, quotes = execution_facts()
    events[0]["payload"].pop("quote_currency")
    events[1]["payload"]["quote_currency"] = "USD"
    result = calculate_execution_quality(events, independent_quotes=quotes)
    assert result["independent_quote_shortfall_bps"]["status"] == "invalid_input"
    assert "does not match fill" in result["independent_quote_shortfall_bps"]["reason"]
    result = calculate_execution_quality(events, terminal_valuations=terminal, as_of=stamp(40))
    assert result["opportunity_cost"]["status"] == "invalid_input"


def test_execution_optional_metric_overflow_remains_null_and_json_safe():
    events, terminal, _ = execution_facts()
    terminal[0]["price"] = 1e308
    result = calculate_execution_quality(events, terminal_valuations=terminal, as_of=stamp(40))
    assert result["opportunity_cost"]["status"] == "invalid_input"
    assert result["opportunity_cost"]["value"] is None
    json.dumps(result, allow_nan=False)


def test_standard_report_exports_fact_metrics_and_typed_statuses(tmp_path, monkeypatch):
    for name in ("_plot_equity", "_plot_monthly_heatmap", "_plot_rolling_metrics", "_plot_pnl_distribution"):
        monkeypatch.setattr(ReportGenerator, name, lambda *args, **kwargs: None)
    monkeypatch.setattr("backtest.reporting.render.pdf.write_pdf_report", lambda *args, **kwargs: None)
    account, groups, flows, external = group_facts()
    events, terminal, quotes = execution_facts()
    metrics = ReportGenerator(str(tmp_path)).generate(
        [], pd.DataFrame({"equity": account, "cash": account}), event_log=events,
        group_equity=groups, group_cashflows=flows, external_cashflows=external,
        terminal_valuations=terminal, independent_quotes=quotes, execution_as_of=stamp(40))
    expected = metrics["ExtendedAnalytics"]["attribution"]["group_drawdown_contribution"]
    stored = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert stored["metrics"]["ExtendedAnalytics"]["attribution"]["group_drawdown_contribution"] == expected
    scalars = {row["name"]: row for row in stored["metric_results"]}
    assert scalars["GroupAttributedMaxDrawdownPct"]["value"] == pytest.approx(-.19)
    assert scalars["UnfilledOpportunityCostBps"]["value"] == 300
    assert scalars["IndependentQuoteShortfallBps"]["value"] == 100
    execution = json.loads((tmp_path / "execution_quality.json").read_text(encoding="utf-8"))["metrics"]
    assert execution["opportunity_cost"]["observations"][0]["reference_id"] == "terminal-snapshot-1"
