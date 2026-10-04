"""Independent public evidence: durable BBO, bounded background lifecycle, PIT join."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys
from threading import Event
import time

import pandas as pd
import pytest

from analysis.execution_calibration import (
    deduplicate_request_records, execution_sample_report, join_quotes_to_execution,
    read_fill_provenance,
)
from core.order_latency import OrderLatencyRecorder
from core.quote_observations import (
    BackgroundQuoteSampler, QuoteObservationStore, read_quote_observations, utc_now,
)
from tests.test_execution_calibration import facts


def quote(identity="q", at="2020-01-01T00:00:00Z", **overrides):
    stamp = pd.Timestamp(at)
    return {"quote_id": identity, "exchange_id": "binance", "environment": "live",
        "market_type": "spot", "symbol": "BTC/USDT", "quote_currency": "USDT",
        "bid": 99.9, "ask": 100.1, "bid_quantity": 2., "ask_quantity": 3.,
        "occurred_at": stamp.isoformat(), "occurred_at_status": "exchange_timestamp",
        "observed_at": stamp.isoformat(), "available_at": stamp.isoformat(),
        "source_id": "binance:public_rest_order_book", "collector_run_id": "test-observer",
        "independent": True, "observation_kind": "public_order_book", **overrides}


def evidence(days=1):
    events, requests, _ = facts(days)
    quotes, proof = [], []
    for i, request in enumerate(requests):
        request.update(exchange_id="binance", symbol="BTC/USDT", observation_id=f"r{i}",
                       environment="live", market_type="spot")
        quotes.append(quote(f"q{i}", request["sent_at"]))
    for event in events:
        if event["event_type"] == "fill":
            p = event["payload"]
            proof.append({"account_id": event["account_id"], "exchange_id": "binance",
                "symbol": p["symbol"], "fill_id": p["fill_id"], "client_order_id": p["client_order_id"],
                "kind": "venue_trade", "occurred_at": event["occurred_at"], "qty": p["qty"], "price": p["price"]})
    context = {"a": {"exchange_id": "binance", "environment": "live", "market_type": "spot", "quote_currency": "USDT"}}
    return events, requests, quotes, proof, context


def join(events, requests, quotes, proof, context, **kwargs):
    return join_quotes_to_execution(events, requests, quotes, fill_provenance=proof,
        market_context=context, as_of="2020-02-01T00:00:00Z", **kwargs)


def test_quote_store_restart_append_only_history_and_whitelist(tmp_path):
    path = tmp_path/"quotes.sqlite3"
    store = QuoteObservationStore(path)
    for i in range(1005):
        row = quote(f"q{i}", secret="must-not-persist", info={"apiKey": "secret"})
        store.append(row)
    saved = store.read_all()
    assert len(saved) == 1005 and saved[-1]["sequence"] == 1005
    assert "secret" not in json.dumps(saved) and "info" not in saved[0]
    reopened = QuoteObservationStore(path)
    assert reopened.health()["persisted_records"] == 1005
    assert reopened.append(quote("q0"))["sequence"] == 1
    assert len(read_quote_observations(path, after_sequence=1000)) == 5
    with pytest.raises(ValueError, match="conflicting"):
        reopened.append(quote("q0", bid=99.))
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM quote_observations")


@pytest.mark.parametrize("changes", [{"bid": 101.}, {"ask": float("nan")},
    {"occurred_at": None}, {"observed_at": "2020-01-01"},
    {"available_at": "2019-12-31T00:00:00Z"}, {"independent": False}])
def test_invalid_quotes_never_become_observations(tmp_path, changes):
    store = QuoteObservationStore(tmp_path/"quotes.sqlite3")
    with pytest.raises(ValueError):
        store.append(quote(**changes))
    assert store.read_all() == []


def test_unknown_exchange_time_is_retained_without_imputation(tmp_path):
    store = QuoteObservationStore(tmp_path/"quotes.sqlite3")
    store.append(quote(occurred_at=None, occurred_at_status="unknown"))
    assert store.read_all()[0]["occurred_at"] is None


def test_database_lock_is_bounded_and_visible(tmp_path):
    path = tmp_path/"quotes.sqlite3"
    store = QuoteObservationStore(path, lock_timeout_seconds=.01)
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.append(quote())
        assert time.monotonic()-start < .5
        assert store.health()["write_failures"] == 1
    finally:
        connection.rollback()
        connection.close()


def test_background_sampling_restart_read_and_stale_disconnect_health(tmp_path):
    store = QuoteObservationStore(tmp_path/"quotes.sqlite3")
    reached = Event()
    calls = []
    def fetch(symbol):
        calls.append(symbol)
        if len(calls) >= 2:
            reached.set()
        return {"bids": [[99., 1.]], "asks": [[101., 2.]], "timestamp": None,
                "info": {"password": "not-recorded"}}
    sampler = BackgroundQuoteSampler(exchange_id="binance", symbols=["BTC/USDT", "ETH/USDT"],
        market_type="spot", store=store, fetcher=fetch, interval_seconds=.01).start()
    assert reached.wait(1.)
    sampler.stop(join_timeout_seconds=.5)
    rows = store.read_all()
    assert rows and all(row["occurred_at"] is None for row in rows)
    assert sampler.health()["lifecycle"] == "stopped"
    stale = sampler.health(as_of=utc_now()+timedelta(seconds=30))
    assert stale["status"] == "degraded"
    assert any(row["status"] == "stale" for row in stale["symbols"].values())
    assert "password" not in json.dumps(rows)


def test_stop_is_bounded_and_fetch_error_never_leaks_secret_or_blocking_result(tmp_path):
    entered, release = Event(), Event()
    def blocked(symbol):
        entered.set()
        release.wait(1.)
        raise RuntimeError("apiKey=do-not-log")
    sampler = BackgroundQuoteSampler(exchange_id="binance", symbols=["BTC/USDT"], market_type="spot",
        store=QuoteObservationStore(tmp_path/"quotes.sqlite3"), fetcher=blocked).start()
    assert entered.wait(1.)
    start = time.monotonic()
    assert sampler.stop(join_timeout_seconds=.01)["lifecycle"] == "stop_pending"
    assert time.monotonic()-start < .2
    release.set()
    health = sampler.stop(join_timeout_seconds=.5)
    assert health["lifecycle"] == "stopped"
    assert health["symbols"]["BTC/USDT"]["status"] == "disconnected"
    assert "do-not-log" not in json.dumps(health)


def test_pre_submit_join_ignores_closer_but_post_submit_quote_and_is_deterministic():
    args = evidence()
    events, requests, quotes, proof, context = args
    quotes.append(quote("post-submit", "2020-01-01T00:00:00.200Z", bid=150, ask=151))
    matched = join(*args)
    assert matched["matched_fills"] == 1
    assert matched["independent_quotes"][0]["quote_id"] == "q0"
    assert join(events[::-1], requests[::-1], quotes[::-1], proof[::-1], context) == matched
    delayed = deepcopy(quotes[0])
    delayed.update(quote_id="delayed", observed_at="2020-01-01T00:00:00.1Z",
                   available_at="2020-01-01T00:00:00.1Z")
    assert join(events, requests, [delayed], proof, context)["unmatched_reasons"] == {"no_quote_available_before_submit": 1}


def test_predecision_and_presubmit_have_distinct_availability_cutoffs():
    events, requests, quotes, proof, context = evidence()
    requests[0]["sent_at"] = "2020-01-01T00:00:00.5Z"
    quotes[0] = quote(at="2020-01-01T00:00:00.2Z")
    assert join(events, requests, quotes, proof, context)["matched_fills"] == 1
    assert join(events, requests, quotes, proof, context, prequote_at="decision")["unmatched_reasons"] == {
        "no_quote_available_before_decision": 1}


@pytest.mark.parametrize("change,reason", [
    ({"environment": "sandbox"}, "no_matching_market_quote"),
    ({"exchange_id": "okx"}, "no_matching_market_quote"),
    ({"market_type": "perpetual"}, "no_matching_market_quote"),
    ({"quote_currency": "USDC"}, "no_matching_market_quote"),
    ({"occurred_at": None, "occurred_at_status": "unknown"}, "exchange_quote_timestamp_unknown"),
    ({"occurred_at": "2019-12-31T23:59:00Z"}, "stale_at_submit"),
])
def test_market_clock_and_staleness_boundaries(change, reason):
    events, requests, quotes, proof, context = evidence()
    quotes[0].update(change)
    assert join(events, requests, quotes, proof, context)["unmatched_reasons"] == {reason: 1}


def test_partial_fill_can_outlive_prequote_and_each_fill_keeps_own_coverage():
    events, requests, quotes, proof, context = evidence()
    events[1]["payload"]["qty"] = .5
    proof[0]["qty"] = .5
    extra = deepcopy(events[1])
    extra.update(occurred_at="2020-01-01T00:00:02Z", observed_at="2020-01-01T00:00:02Z")
    extra["payload"].update(fill_id="partial-2", qty=.5)
    events.append(extra)
    proof.append({**proof[0], "fill_id": "partial-2", "occurred_at": extra["occurred_at"]})
    result = join(events, requests, quotes, proof, context)
    assert result["matched_fills"] == 1 and result["fills"] == 2
    assert result["unmatched_reasons"] == {"prequote_stale_at_fill": 1}


def test_synthetic_or_missing_fill_proof_cannot_create_venue_calibration():
    events, requests, quotes, proof, context = evidence(20)
    base = dict(raw_quotes=quotes, market_context=context, as_of="2020-02-01T00:00:00Z", source="live")
    good = execution_sample_report(events, requests, fill_provenance=proof, **base)
    assert good["real_venue_calibration"]
    for row in proof:
        row["kind"] = "synthetic_cumulative_order"
    bad = execution_sample_report(events, requests, fill_provenance=proof, **base)
    assert not bad["real_venue_calibration"] and bad["status"] == "insufficient"
    assert bad["calibration"]["status"] == "insufficient"
    assert bad["quality"]["first_fill_seconds"]["value"] is None
    assert all(row["fill_clock_evidence"] == "unverified" for row in bad["orders"])
    missing = execution_sample_report(events, requests, **base)
    assert missing["quote_join"]["unmatched_reasons"] == {"fill_provenance_missing": 20}


def test_future_quote_prefix_and_stable_request_replay_identity():
    events, requests, quotes, proof, context = evidence(20)
    records = [*requests, {**requests[0], "sequence": 1}]
    assert len(deduplicate_request_records(records)) == 20
    with pytest.raises(ValueError, match="conflicting_request"):
        deduplicate_request_records([requests[0], {**requests[0], "outcome": "error"}])
    kwargs = dict(market_context=context, fill_provenance=proof, as_of="2020-01-10T23:59:59Z", source="live")
    all_quotes = execution_sample_report(events, records, raw_quotes=quotes, **kwargs)
    before = execution_sample_report(events, records, raw_quotes=quotes[:10], **kwargs)
    assert before == all_quotes


def test_zero_fills_and_sandbox_remain_insufficient_or_nonlive():
    empty = execution_sample_report([], [], raw_quotes=[quote()], market_context={}, fill_provenance=[],
        as_of="2020-02-01T00:00:00Z", source="live")
    assert empty["status"] == "insufficient" and empty["quote_join"]["reason"] == "no_canonical_fills"
    assert not empty["real_venue_calibration"]
    events, requests, quotes, proof, context = evidence(20)
    for row in requests+quotes:
        row["environment"] = "sandbox"
    context["a"]["environment"] = "sandbox"
    output = execution_sample_report(events, requests, raw_quotes=quotes, fill_provenance=proof,
        market_context=context, as_of="2020-02-01T00:00:00Z", source="live")
    assert output["quote_join"]["status"] == "complete" and not output["real_venue_calibration"]


def test_explicit_backtest_or_cumulative_fill_cannot_override_its_origin():
    events, requests, quotes, proof, context = evidence()
    events[1]["source"] = "backtest"
    assert join(events, requests, quotes, proof, context)["unmatched_reasons"] == {
        "synthetic_or_unverified_fill_time": 1}
    events[1].pop("source")
    events[1]["payload"]["synthetic_from_order"] = True
    assert join(events, requests, quotes, proof, context)["matched_fills"] == 0


def test_ledger_provenance_read_is_readonly_and_rejects_cumulative_or_imputed_clock(tmp_path):
    path = tmp_path/"orders.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE orders(account,exchange,symbol,client_order_id)")
        conn.execute("CREATE TABLE fills(fill_id,client_order_id,timestamp,qty,price,payload)")
        conn.execute("INSERT INTO orders VALUES ('a','binance','BTC/USDT','o0')")
        for i, payload in enumerate(({"timestamp": 1577836801000}, {"synthetic_from_order": True}, {})):
            conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?)",
                (f"f{i}", "o0", "2020-01-01T00:00:01Z", 1., 100.1, json.dumps(payload)))
    initial = path.read_bytes()
    rows = read_fill_provenance(path)
    assert [r["kind"] for r in rows] == ["venue_trade", "synthetic_cumulative_order", "venue_trade_time_unverified"]
    assert path.read_bytes() == initial


def test_offline_cli_reads_stores_and_deduplicates_receipt_without_network(tmp_path, monkeypatch):
    from scripts import calibrate_execution
    events, requests, quotes, proof, context = evidence()
    request_path, quote_path = tmp_path/"requests.sqlite3", tmp_path/"quotes.sqlite3"
    recorder = OrderLatencyRecorder(persistence_path=request_path)
    recorder._record(requests[0])
    store = QuoteObservationStore(quote_path)
    store.append(quotes[0])
    source, output = tmp_path/"input.json", tmp_path/"report.json"
    source.write_text(json.dumps({"events": events, "request_records": requests,
        "fill_provenance": proof, "market_context": context,
        "source": "live", "as_of": "2020-02-01T00:00:00Z"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["calibrate_execution.py", "--input", str(source), "--output", str(output),
        "--request-store", str(request_path), "--quote-store", str(quote_path)])
    calibrate_execution.main()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["quote_join"]["matched_fills"] == 1
    assert report["coverage"]["submit_request_orders"] == 1
    assert report["real_venue_calibration"] is False  # one date cannot calibrate
