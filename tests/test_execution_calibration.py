from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from analysis.execution_calibration import execution_sample_report
from core.order_latency import OrderLatencyRecorder, record_order_call


def facts(days=20):
    events, requests, quotes = [], [], []
    for i, at in enumerate(pd.date_range("2020-01-01", periods=days, tz="UTC")):
        cid, fid = f"o{i}", f"f{i}"
        common = {"account_id": "a", "observed_at": (at + pd.Timedelta(seconds=2)).isoformat()}
        events += [{**common, "event_type": "order_intent", "occurred_at": at.isoformat(),
            "payload": {"client_order_id": cid, "symbol": "BTC/USDT", "quote_currency": "USDT",
                "requested_qty": 1., "reference_price": 100., "action": "buy"}},
            {**common, "event_type": "fill", "occurred_at": (at + pd.Timedelta(seconds=1)).isoformat(),
             "payload": {"client_order_id": cid, "fill_id": fid, "symbol": "BTC/USDT",
                "quote_currency": "USDT", "qty": 1., "price": 100.1, "liquidity": "taker"}},
            {**common, "event_type": "order", "occurred_at": (at + pd.Timedelta(seconds=1)).isoformat(),
             "payload": {"client_order_id": cid, "status": "filled", "requested_qty": 1., "filled_qty": 1.}}]
        requests.append({"account_id": "a", "client_order_id": cid, "operation": "submit",
            "sent_at": at.isoformat(), "received_at": (at + pd.Timedelta(seconds=.5)).isoformat(), "outcome": "ack"})
        quotes.append({"account_id": "a", "fill_id": fid, "symbol": "BTC/USDT", "quote_currency": "USDT",
            "occurred_at": at.isoformat(), "available_at": at.isoformat(), "source_id": "independent",
            "reference_id": f"q{i}", "independent": True, "bid": 99.9, "ask": 100.1})
    return events, requests, quotes


def report(events, requests, quotes, **kwargs):
    return execution_sample_report(events, requests, independent_quotes=quotes,
        as_of="2020-02-01T00:00:00Z", **kwargs)


def test_receipt_join_chronological_cost_holdout_and_simulation_boundary():
    events, requests, quotes = facts()
    out = report(events, requests, quotes)
    assert out["coverage"]["orders"] == 20
    assert out["orders"][0]["request_receipt_seconds"] == .5
    assert out["orders"][0]["first_fill_seconds"] == 1.
    assert out["calibration"]["fitted_train_cost_bps"] == pytest.approx(10.)
    assert out["calibration"]["test_mean_error_bps"] == pytest.approx(0.)
    assert out["calibration"]["test_days"] == 6
    assert not out["real_venue_calibration"] and not out["production_approved"]


def test_live_claim_requires_independent_quotes_and_receipt_coverage():
    events, requests, quotes = facts()
    assert report(events, requests, quotes, source="live")["real_venue_calibration"]
    assert not report(events, [], quotes, source="live")["real_venue_calibration"]
    assert not report(events, requests, None, source="live")["real_venue_calibration"]


def test_future_prediction_and_conflicting_submits_invalid():
    events, requests, quotes = facts()
    prediction = {"a/o0": {"bps": 10., "available_at": "2020-01-02T00:00:00Z"}}
    assert report(events, requests, quotes, predictions=prediction)["status"] == "invalid"
    requests.append({**requests[0], "outcome": "error"})
    assert report(events, requests, quotes)["status"] == "invalid"


def test_absent_evidence_remains_insufficient():
    out = execution_sample_report([], as_of="2020-01-01T00:00:00Z", source="live")
    assert out["status"] == "insufficient" and not out["real_venue_calibration"]


def test_future_independent_facts_do_not_change_past_report():
    events, requests, quotes = facts()
    kwargs = dict(as_of="2020-01-10T23:59:59Z", source="live")
    all_facts = execution_sample_report(events, requests, independent_quotes=quotes, **kwargs)
    known_only = execution_sample_report(events[:30], requests[:10], independent_quotes=quotes[:10], **kwargs)
    assert all_facts == known_only
    future_terminal = [{"available_at": "2020-01-30T00:00:00Z", "client_order_id": "future"}]
    with_future = execution_sample_report(events, requests, independent_quotes=quotes,
        terminal_valuations=future_terminal, **kwargs)
    with_empty = execution_sample_report(events, requests, independent_quotes=quotes,
        terminal_valuations=[], **kwargs)
    assert with_future == with_empty
    late_observed = {**quotes[0], "fill_id": "unknown", "observed_at": "2020-01-20T00:00:00Z"}
    assert execution_sample_report(events, requests, independent_quotes=[*quotes, late_observed], **kwargs) == all_facts
    known_bad = {**late_observed, "observed_at": "2020-01-02T00:00:00Z"}
    assert execution_sample_report(events, requests, independent_quotes=[*quotes, known_bad], **kwargs)["status"] == "invalid"


def test_delayed_training_fills_cannot_leak_across_cost_calibration_split():
    events, requests, quotes = facts()
    for event in events:
        if event["payload"]["client_order_id"] in {f"o{i}" for i in range(5)}:
            event["observed_at"] = "2020-01-25T00:00:00Z"
    out = report(events, requests, quotes, source="live")
    assert out["calibration"]["status"] == "insufficient"
    assert out["calibration"]["mature_train_days"] == 9
    assert not out["real_venue_calibration"]


def test_recorder_context_is_whitelisted_and_not_forwarded():
    owner = SimpleNamespace(account_id="a", exchange_id="venue", order_latency=OrderLatencyRecorder(max_records=1))
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        return {"id": "exchange1", "status": "open"}
    for _ in range(2):
        record_order_call(owner, "submit", request, symbol="BTC/USDT", telemetry_context={
            "client_order_id": "o", "secret": "do-not-record", "decision_at": datetime.now(timezone.utc)})
    row = owner.order_latency.summary()
    assert row["dropped_records"] == 1
    assert row["records"][0]["client_order_id"] == "o"
    assert "secret" not in str(row) and calls == [{"symbol": "BTC/USDT"}] * 2
