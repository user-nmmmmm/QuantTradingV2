import json
import sqlite3

import pytest

from analysis.execution_calibration import read_execution_ledger, execution_readiness_report


AT = "2020-01-01T00:00:00+00:00"
CUTOFF = "2020-02-01T00:00:00+00:00"


def ledger(tmp_path, *, environment=None, source=None, fill=True):
    path = tmp_path / "live_orders.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE orders(client_order_id,exchange_order_id,exchange,account,symbol,side,requested_qty,price,status,submission_attempted,intent,payload,updated_at)")
        con.execute("CREATE TABLE fills(fill_id,client_order_id,timestamp,qty,price,payload,fee,fee_currency)")
        con.execute("CREATE TABLE fill_evidence(fill_id,fee_status)")
        if environment:
            con.execute("CREATE TABLE runtime_identity(id,identity)")
            con.execute("INSERT INTO runtime_identity VALUES(1,?)", (json.dumps({"exchange": "binance", "environment": environment, "account": "spot", "market_type": "spot"}),))
        con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("o1", "venue1", "binance", "spot", "BTC/USDT", "buy", 1., 100., "filled" if fill else "rejected", int(fill),
             json.dumps({"reference_price": 100.}), json.dumps({"source": source} if source else {}), AT))
        if fill:
            con.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)", ("f1", "o1", AT, 1., 100.,
                json.dumps({"datetime": AT, "observed_at": AT, "fee": {"cost": 0., "currency": "USDT"}}), 0., "USDT"))
            con.execute("INSERT INTO fill_evidence VALUES('f1','recorded')")
    return path


def quote(at=AT, **changes):
    return {"quote_id": "q1", "exchange_id": "binance", "environment": "live", "market_type": "spot",
        "symbol": "BTC/USDT", "quote_currency": "USDT", "bid": 99., "ask": 101.,
        "occurred_at": at, "occurred_at_status": "exchange_timestamp", "observed_at": at, "available_at": at,
        "source_id": "binance:spot:sequenced_diff_depth", "collector_run_id": "run1",
        "independent": True, "observation_kind": "public_order_book", **changes}


def request(at=AT, **changes):
    return {"observation_id": "r1", "operation": "submit", "account_id": "spot", "client_order_id": "o1",
        "exchange_id": "binance", "environment": "live", "market_type": "spot", "sent_at": at,
        "received_at": at, "outcome": "ack", **changes}


@pytest.mark.parametrize("environment,source,expected", [(None, None, "unknown_environment"),
    ("sandbox", None, "sandbox"), ("live", "paper", "simulation"), ("live", None, "live")])
def test_ledger_source_cannot_be_upgraded_by_filename(tmp_path, environment, source, expected):
    path = ledger(tmp_path, environment=environment, source=source)
    before = path.read_bytes()
    rows = read_execution_ledger(path, as_of=CUTOFF)
    result = execution_readiness_report([rows], [request()], [quote()], as_of=CUTOFF)
    assert result["coverage"]["orders_by_evidence_class"] == {expected: 1}
    assert result["coverage"]["qualified_live_fills"] == (1 if expected == "live" else 0)
    assert path.read_bytes() == before
    assert not result["real_venue_calibration"]


def test_zero_fills_and_unknown_rest_time_never_calibrate(tmp_path):
    rows = read_execution_ledger(ledger(tmp_path, fill=False), as_of=CUTOFF)
    out = execution_readiness_report([rows], [], [quote(occurred_at=None, occurred_at_status="unknown")], as_of=CUTOFF)
    assert out["coverage"]["ledger_fills"] == out["coverage"]["qualified_live_fills"] == 0
    assert out["coverage"]["event_time_quotes"] == 0
    assert out["coverage"]["submission_attempted_orders"] == 0
    assert all(r["status"] == "gap" for r in out["strata"])


def test_snapshot_replay_deduplicates_and_future_updated_rows_are_unavailable(tmp_path):
    path = ledger(tmp_path, environment="live")
    rows = read_execution_ledger(path, as_of=CUTOFF)
    out = execution_readiness_report([rows, rows], [request(), request()], [quote(), quote()], as_of=CUTOFF)
    assert out["coverage"]["ledger_fills"] == 1
    assert out["coverage"]["request_observations"] == out["coverage"]["independent_quotes"] == 1
    earlier = read_execution_ledger(path, as_of="2019-12-31T23:59:59Z")
    assert earlier["orders"] == earlier["fills"] == []
    assert earlier["excluded_future_updated_orders"] == 1


def test_fresh_prequote_and_explicit_live_receipt_required(tmp_path):
    rows = read_execution_ledger(ledger(tmp_path, environment="live"), as_of=CUTOFF)
    for req, q, reason in ((request(environment="sandbox"), quote(), "missing_or_ambiguous_submit_ack"),
        (request(), quote(occurred_at=None, occurred_at_status="unknown"), "missing_fresh_event_time_prequote"),
        (request(), quote(available_at="2020-01-01T00:00:01Z"), "missing_fresh_event_time_prequote")):
        out = execution_readiness_report([rows], [req], [q], as_of=CUTOFF)
        assert out["coverage"]["qualified_live_fills"] == 0
        assert out["coverage"]["fill_exclusion_reasons"] == {reason: 1}


def test_disjoint_calendar_and_direction_size_gaps(tmp_path):
    rows = read_execution_ledger(ledger(tmp_path, environment="live"), as_of=CUTOFF)
    base_order, base_fill = rows["orders"][0], rows["fills"][0]
    orders, fills, requests, quotes = [], [], [], []
    for i in range(20):
        at = f"2020-01-{i + 1:02d}T00:00:00+00:00"
        orders.append({**base_order, "client_order_id": f"o{i}", "updated_at": at, "order_date": at[:10]})
        fills.append({**base_fill, "client_order_id": f"o{i}", "fill_id": f"f{i}", "occurred_at": at,
                      "available_at": at, "fee_available_at": at})
        requests.append(request(at, client_order_id=f"o{i}", observation_id=f"r{i}"))
        quotes.append(quote(at, quote_id=f"q{i}"))
    out = execution_readiness_report([{**rows, "orders": orders, "fills": fills}], requests, quotes, as_of=CUTOFF)
    assert len(out["calendar"]["train_days"]) == 14
    assert len(out["calendar"]["test_days"]) == 6
    assert not set(out["calendar"]["train_days"]) & set(out["calendar"]["test_days"])
    assert next(s for s in out["strata"] if s["symbol"] == "BTC/USDT" and s["side"] == "buy" and s["size_bucket"] == "under_1k")["status"] == "coverage_met"
    assert all(s["status"] == "gap" for s in out["strata"] if s["side"] == "sell")
    orders[0]["updated_at"] = "2020-01-16T00:00:00+00:00"
    late = execution_readiness_report([{**rows, "orders": orders, "fills": fills}], requests, quotes, as_of=CUTOFF)
    assert "2020-01-01" not in late["calendar"]["train_days"]


@pytest.mark.parametrize("mutation,expected", [
    ("missing_fee", "missing_explicit_fee_evidence"),
    ("unverified_zero", "missing_explicit_fee_evidence"),
    ("missing_observed", "fill_observation_time_unverified"),
    ("third_currency", "missing_point_in_time_fee_conversion"),
])
def test_price_evidence_does_not_meet_cost_coverage_without_fees_and_availability(tmp_path, mutation, expected):
    path = ledger(tmp_path, environment="live")
    with sqlite3.connect(path) as connection:
        raw = json.loads(connection.execute("SELECT payload FROM fills").fetchone()[0])
        if mutation == "missing_fee":
            raw.pop("fee")
        elif mutation == "unverified_zero":
            connection.execute("UPDATE fill_evidence SET fee_status='legacy_unverified'")
        elif mutation == "missing_observed":
            raw.pop("observed_at")
        else:
            raw["fee"] = {"cost": 1., "currency": "BNB"}
            connection.execute("UPDATE fills SET fee=1.,fee_currency='BNB'")
        connection.execute("UPDATE fills SET payload=?", (json.dumps(raw),))
    out = execution_readiness_report([read_execution_ledger(path, as_of=CUTOFF)], [request()], [quote()], as_of=CUTOFF)
    assert out["coverage"]["price_execution_qualified_fills"] == 1
    assert out["coverage"]["qualified_live_fills"] == out["coverage"]["cost_qualified_fills"] == 0
    assert out["coverage"]["fill_exclusion_reasons"] == {expected: 1}


@pytest.mark.parametrize("source", ["simulation", "paper", "backtest", "sandbox"])
def test_fill_provenance_overrides_live_order_identity(tmp_path, source):
    path = ledger(tmp_path, environment="live")
    with sqlite3.connect(path) as connection:
        raw = json.loads(connection.execute("SELECT payload FROM fills").fetchone()[0])
        raw["source"] = source
        connection.execute("UPDATE fills SET payload=?", (json.dumps(raw),))
    out = execution_readiness_report([read_execution_ledger(path, as_of=CUTOFF)], [request()], [quote()], as_of=CUTOFF)
    assert out["coverage"]["price_execution_qualified_fills"] == 0


def test_late_fill_crossing_holdout_boundary_never_matures_as_prior_day(tmp_path):
    rows = read_execution_ledger(ledger(tmp_path, environment="live"), as_of=CUTOFF)
    order, fill = rows["orders"][0], rows["fills"][0]
    sent = "2020-01-01T23:59:59.800+00:00"
    occurred = "2020-01-02T00:00:00.100+00:00"
    test_at = "2020-01-02T12:00:00+00:00"
    orders = [{**order, "client_order_id": "o0", "updated_at": sent, "order_date": sent[:10]},
              {**order, "client_order_id": "o1", "updated_at": test_at, "order_date": test_at[:10]}]
    fills = [{**fill, "client_order_id": "o0", "fill_id": "f0", "occurred_at": occurred,
              "available_at": occurred, "fee_available_at": occurred},
             {**fill, "client_order_id": "o1", "fill_id": "f1", "occurred_at": test_at,
              "available_at": test_at, "fee_available_at": test_at}]
    out = execution_readiness_report([{**rows, "orders": orders, "fills": fills}],
        [request(sent, client_order_id="o0", observation_id="r0"), request(test_at)],
        [quote(sent, quote_id="q0"), quote(test_at)], as_of=CUTOFF, minimum_train_days=1, minimum_test_days=1)
    assert out["calendar"]["test_start"] == "2020-01-02"
    assert out["calendar"]["train_days"] == []
    assert out["calendar"]["missing_train_days"] == 1
