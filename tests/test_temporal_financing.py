"""Financed labels need complete event evidence, with immutable cutoff history."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from core.signal_label_versions import OutcomeRevisionBook
from core.signal_meta_layer import _validate_input
from core.signal_observation_types import ObservationPolicy, fingerprint, iso
from core.signal_outcomes import ObservationCosts
from core.temporal_financing import TemporalFinancing
from core.temporal_labels import VersionedOutcomeTracker
from tests.test_temporal_labels import reader, event
from tests.test_signal_observation import candidate


def record(key, at, data, *, available=None, observed=None, revision="v1"):
    return {"record_id": key, "event_time": iso(at), "revision_id": revision,
        "available_at": iso(available or at), "observed_at": iso(observed or available or at),
        "availability_evidence": {"kind": "source_publication", "reference": "fixture:published-evidence"},
        "data": data}


def funding_records(direction="long"):
    symbol = "BTC/USDT:USDT"
    spec = dict(venue="fixture", contract_id="BTCUSDT", symbol=symbol, base_currency="BTC",
        quote_currency="USDT", settlement_currency="USDT", contract_multiplier=.001,
        linear=True, market_type="perpetual", funding_interval_hours=8)
    rows = [record("contract", "2020-01-01", {"kind": "linear_contract", "spec": spec,
        "valid_from": iso("2020-01-01"), "valid_until": iso("2020-02-01")})]
    for i, at in enumerate(pd.date_range("2020-01-02 08:00", periods=3, freq="8h")):
        rows.append(record(f"funding{i}", at, {"kind": "funding", "contract_id": "BTCUSDT",
            "settlement_time": iso(at), "funding_rate": .001, "mark_price": 100.+i}))
    rows.append(record("coverage", "2020-01-03", {"kind": "coverage", "complete": True,
        "account_mode": "perpetual", "symbol": symbol, "direction": direction,
        "start": iso("2020-01-02"), "end": iso("2020-01-03"), "currency": "USDT",
        "event_ids": ["funding0", "funding1", "funding2"], "contract_record_id": "contract"}))
    return rows


def borrow_records(direction="short", fraction=1.):
    rows = [record("permission", "2020-01-01", {"kind": "borrow_permission", "symbol": "BTC/USDT",
        "direction": direction, "base_currency": "BTC", "quote_currency": "USDT",
        "borrow_currency": "BTC" if direction == "short" else "USDT", "limit": 2000.,
        "borrow_fraction": fraction, "valid_from": iso("2020-01-01"), "valid_until": iso("2020-02-01")})]
    for i, start in enumerate(pd.date_range("2020-01-02", periods=2, freq="12h")):
        end = start+pd.Timedelta(hours=12)
        rows.append(record(f"borrow{i}", end, {"kind": "borrow", "permission_id": "permission",
            "start": iso(start), "end": iso(end), "annual_rate": .365,
            "quote_conversion_price": 100. if direction == "short" else 1., "fee_quote_per_base_unit": .01}))
    rows.append(record("coverage", "2020-01-03", {"kind": "coverage", "complete": True,
        "account_mode": "spot_margin", "symbol": "BTC/USDT", "direction": direction,
        "start": iso("2020-01-02"), "end": iso("2020-01-03"), "currency": "USDT",
        "event_ids": ["borrow0", "borrow1"], "permission_record_id": "permission"}))
    return rows


def build(records=None, *, account="perpetual", direction="long", costs=None):
    records = funding_records(direction) if records is None else records
    provider = TemporalFinancing("fixture:financing", records)
    c = candidate(symbol="BTC/USDT:USDT" if account == "perpetual" else "BTC/USDT", direction=direction)
    data = reader()
    tracker = VersionedOutcomeTracker(ObservationPolicy(enabled=True, horizons=(1,)),
        costs or ObservationCosts(account_mode=account, commission_rate=.001, slippage=.001), financing=provider)
    advance(tracker, data, "2020-01-02", c)
    tracker.add(c)
    return provider, tracker, data, c


def advance(tracker, data, at, c):
    e = event(data, at)
    e = replace(e, bars={c.symbol: e.bars["X"]}, histories={c.symbol: e.histories["X"]})
    tracker.advance(e)


def payload(tracker, c):
    return {"schema": "signal_observation/v1", "status": "complete", "policy": asdict(tracker.policy),
        "snapshot_version": c.context.snapshot_version, "strategy_versions": {c.strategy: c.signal_version},
        "candidates": [c.to_dict()], "decisions": [{"candidate_id": c.candidate_id, "veto_stage": "allowed"}],
        "outcomes": tracker.results, "outcome_revisions": tracker.revisions, "costs": asdict(tracker.costs),
        "temporal_label_protocol": tracker.protocol}


def book(tracker, c):
    p = payload(tracker, c)
    h, candidates, _, outcomes = _validate_input(p)
    return OutcomeRevisionBook(p, candidates, h, outcomes)


def revise(row, *, at="2020-01-06", **changes):
    row = deepcopy(row)
    row.update(revision_id="revision:"+at, available_at=iso(at), observed_at=iso(at))
    row["data"].update(changes)
    return row


def tombstone(row, at="2020-01-06"):
    row = revise(row, at=at)
    row.update(data={}, record_type="tombstone", retraction_reason="withdrawn by provider")
    return row


@pytest.mark.parametrize("direction,sign", [("long", 1), ("short", -1)])
def test_funding_grid_cost_sign_and_full_producer_consumer_contract(direction, sign):
    _, tracker, data, c = build(direction=direction)
    advance(tracker, data, "2020-01-03", c)
    row = tracker.results[0]
    assert row["carry"] == pytest.approx(sign*10.*.001*(100+101+102))
    assert len(row["financing_ledger"]) == 3
    assert row["net_pnl"] == pytest.approx(row["net_before_financing"]-row["carry"])
    assert row["training_eligible"]
    assert book(tracker, c).eligible(book(tracker, c).as_of("2020-01-04")["c", 1])


@pytest.mark.parametrize("late_id", ["funding0", "coverage"])
def test_late_financing_waits_for_actual_observation(late_id):
    rows = funding_records()
    next(r for r in rows if r["record_id"] == late_id)["observed_at"] = iso("2020-01-06")
    _, tracker, data, c = build(rows)
    for day in (3, 4, 5):
        advance(tracker, data, f"2020-01-{day:02d}", c)
        assert tracker.results == []
    advance(tracker, data, "2020-01-06", c)
    assert tracker.results[0]["available_at"] == iso("2020-01-06")
    assert book(tracker, c).as_of("2020-01-06") == {}


def test_funding_revision_changes_only_future_training_and_can_be_credit():
    provider, tracker, data, c = build()
    advance(tracker, data, "2020-01-03", c)
    old = tracker.revisions
    provider.ingest([revise(funding_records()[1], funding_rate=-.01)])
    advance(tracker, data, "2020-01-05", c)
    assert tracker.revisions == old
    advance(tracker, data, "2020-01-06", c)
    assert tracker.revisions[:1] == old and tracker.results[0]["carry"] < 0
    b = book(tracker, c)
    assert b.as_of("2020-01-06")["c", 1] == old[0]
    assert b.as_of("2020-01-07")["c", 1] == tracker.results[0]
    advance(tracker, data, "2020-01-07", c)
    assert len(tracker.revisions) == 2


@pytest.mark.parametrize("withdrawn", ["funding1", "coverage", "contract"])
def test_financing_withdrawal_invalidates_latest_without_old_fallback(withdrawn):
    provider, tracker, data, c = build()
    advance(tracker, data, "2020-01-03", c)
    old = tracker.revisions
    original = next(r for r in funding_records() if r["record_id"] == withdrawn)
    provider.ingest([tombstone(original)])
    advance(tracker, data, "2020-01-06", c)
    assert tracker.results[0]["status"] == "censored_financing_evidence_invalidated"
    assert not tracker.results[0]["training_eligible"]
    assert tracker.revisions[:1] == old
    b = book(tracker, c)
    assert b.eligible(b.as_of("2020-01-06")["c", 1])
    assert not b.eligible(b.as_of("2020-01-07")["c", 1])
    provider.ingest([revise(original, at="2020-01-08")])
    advance(tracker, data, "2020-01-08", c)
    # Revised contract terms weren't known at the candidate decision.
    assert tracker.results[0]["training_eligible"] is (withdrawn != "contract")


@pytest.mark.parametrize("problem", ["missing", "omitted", "unknown", "late_terms"])
def test_incomplete_funding_never_uses_default_rate(problem):
    rows = funding_records()
    if problem == "missing":
        rows = [r for r in rows if r["record_id"] != "funding1"]
    elif problem == "omitted":
        rows[-1]["data"]["event_ids"].remove("funding1")
    elif problem == "unknown":
        rows[1].update(available_at=None, availability_evidence=None)
    else:
        rows[0]["available_at"] = rows[0]["observed_at"] = iso("2020-01-03")
    _, tracker, data, c = build(rows, costs=ObservationCosts(account_mode="perpetual", funding_rate_required=False))
    advance(tracker, data, "2020-01-07", c)
    tracker.finish("2020-01-07")
    assert tracker.results[0]["status"] == "censored_unversioned_financing_evidence"
    assert not tracker.results[0]["training_eligible"] and tracker.results[0]["net_pnl"] is None


@pytest.mark.parametrize("direction,fraction", [("short", 1.), ("long", .5)])
def test_borrow_fixed_principal_exact_accrual_fee_and_consumer(direction, fraction):
    _, tracker, data, c = build(borrow_records(direction, fraction), account="spot_margin", direction=direction)
    advance(tracker, data, "2020-01-03", c)
    row = tracker.results[0]
    expected = (10*100 if direction == "short" else 10*row["entry_price"]*fraction)*.001 + .2
    assert row["carry"] == pytest.approx(expected)
    assert len(row["financing_ledger"]) == 2
    assert row["training_eligible"]
    assert book(tracker, c).eligible(book(tracker, c).as_of("2020-01-04")["c", 1])


@pytest.mark.parametrize("problem", ["gap", "overlap", "limit", "late_permission", "missing_fee"])
def test_borrow_cannot_train_without_exact_principal_permission_and_intervals(problem):
    rows = borrow_records()
    if problem == "gap":
        rows[2]["data"]["start"] = iso("2020-01-02 13:00")
    elif problem == "overlap":
        rows[2]["data"]["start"] = iso("2020-01-02 11:00")
    elif problem == "limit":
        rows[0]["data"]["limit"] = 9.
    elif problem == "late_permission":
        rows[0]["observed_at"] = iso("2020-01-03")
    else:
        del rows[1]["data"]["fee_quote_per_base_unit"]
        with pytest.raises((ValueError, KeyError), match="fee"):
            build(rows, account="spot_margin", direction="short")
        return
    _, tracker, data, c = build(rows, account="spot_margin", direction="short")
    advance(tracker, data, "2020-01-03", c)
    tracker.finish("2020-01-03")
    assert not tracker.results[0]["training_eligible"]
    assert tracker.results[0]["net_pnl"] is None


def rehash(row):
    row["content_sha256"] = fingerprint({k: v for k, v in row.items() if k not in {"content_sha256", "revision_id"}})
    row["revision_id"] = "label_"+row["content_sha256"]


@pytest.mark.parametrize("tamper", ["carry", "quantity", "net_pnl", "financing_ledger", "record_hash"])
def test_consumer_recomputes_financing_instead_of_trusting_rehashed_output(tamper):
    _, tracker, data, c = build()
    advance(tracker, data, "2020-01-03", c)
    p = payload(tracker, c)
    row = p["outcome_revisions"][0]
    if tamper == "financing_ledger":
        row[tamper][0]["amount"] += 10.
    elif tamper == "record_hash":
        row["financing_versions"][1]["record"]["data"]["funding_rate"] += .1
    else:
        row[tamper] += 10.
    rehash(row)
    p["outcomes"] = [deepcopy(row)]
    h, cs, _, os = _validate_input(p)
    with pytest.raises(ValueError, match="financ"):
        OutcomeRevisionBook(p, cs, h, os)


def test_provider_persistence_hash_conflict_and_stateless_replay(tmp_path):
    rows = funding_records()
    provider = TemporalFinancing("financing", rows, store_path=tmp_path/"store")
    before = provider.as_of("2020-01-03")
    provider.ingest([revise(rows[1], funding_rate=.1)])
    assert provider.as_of("2020-01-03") == before
    restored = TemporalFinancing("financing", store_path=tmp_path/"store")
    assert restored.as_of("2020-01-07") == provider.as_of("2020-01-07")
    path = tmp_path/"finance.json"
    path.write_text(json.dumps(provider.export()), encoding="utf-8")
    imported = TemporalFinancing.from_json(path)
    assert imported.identity["records_sha256"] == provider.identity["records_sha256"]
    changed = deepcopy(rows[1])
    changed["data"]["funding_rate"] = .9
    with pytest.raises(ValueError, match="immutable"):
        imported.ingest([changed])
    assert imported.as_of("2020-01-03") == before


def test_no_scheduled_settlement_is_proved_by_grid_and_explicit_empty_coverage():
    rows = funding_records()
    rows[0]["data"]["spec"]["funding_interval_hours"] = 48
    rows[0]["data"]["spec"]["settlement_anchor"] = iso("2020-01-02")
    rows[-1]["data"]["event_ids"] = []
    _, tracker, data, c = build([rows[0], rows[-1]])
    advance(tracker, data, "2020-01-03", c)
    assert tracker.results[0]["carry"] == 0.
    assert tracker.results[0]["financing_ledger"] == []
    assert book(tracker, c).eligible(book(tracker, c).as_of("2020-01-04")["c", 1])


def test_coverage_revision_cannot_omit_an_already_observed_settlement():
    rows = funding_records()
    provider, tracker, data, c = build(rows)
    advance(tracker, data, "2020-01-03", c)
    provider.ingest([revise(rows[-1], event_ids=["funding0", "funding2"])])
    advance(tracker, data, "2020-01-06", c)
    assert tracker.results[0]["financing_reason"] == "funding_coverage_omits_observed_events"
    assert not book(tracker, c).eligible(book(tracker, c).as_of("2020-01-07")["c", 1])


def test_consumer_rejects_rehashed_resurrection_using_pre_withdrawal_financing():
    provider, tracker, data, c = build()
    advance(tracker, data, "2020-01-03", c)
    old = tracker.results[0]
    provider.ingest([tombstone(funding_records()[1])])
    advance(tracker, data, "2020-01-06", c)
    p = payload(tracker, c)
    resurrection = deepcopy(old)
    resurrection.update(available_at=iso("2020-01-08"), observed_at=iso("2020-01-08"))
    rehash(resurrection)
    p["outcome_revisions"].append(resurrection)
    p["outcomes"] = [resurrection]
    h, cs, _, os = _validate_input(p)
    with pytest.raises(ValueError, match="fell back"):
        OutcomeRevisionBook(p, cs, h, os)


def test_financing_provider_future_versions_do_not_change_candidate_or_label_prefix():
    original_rows = funding_records()
    future_rows = original_rows+[revise(original_rows[1], funding_rate=.2)]
    _, prefix, data, c = build(original_rows)
    _, full, full_data, full_c = build(future_rows)
    for at in ("2020-01-03", "2020-01-04", "2020-01-05"):
        advance(prefix, data, at, c)
        advance(full, full_data, at, full_c)
    assert full.revisions == prefix.revisions
    advance(full, full_data, "2020-01-06", full_c)
    assert full.revisions[:1] == prefix.revisions
    assert book(full, full_c).as_of("2020-01-06") == book(prefix, c).as_of("2020-01-06")


def test_observer_financing_export_enters_consumer_without_future_provider_identity_in_candidate():
    from core.portfolio import Portfolio
    from core.risk import RiskManager
    from core.signal_observation import SignalObserver
    from core.state import MarketState
    from tests.test_signal_observation import DailyRaw

    state = SimpleNamespace(get_states=lambda frame: frame.__setitem__("market_state", MarketState.TREND_UP))
    def run(records):
        data = reader()
        provider = TemporalFinancing("financing", records)
        observer = SignalObserver(policy=ObservationPolicy(enabled=True, horizons=(1,)),
            costs=ObservationCosts(account_mode="perpetual"), strategies={"Test": DailyRaw()},
            state_machine=state, temporal_policy="strict", financing=provider)
        c = candidate(symbol="BTC/USDT:USDT")
        first = event(data, "2020-01-02")
        first = replace(first, bars={c.symbol: first.bars["X"]}, histories={c.symbol: first.histories["X"]})
        observer.advance(first)
        observer.observe(first, c.symbol, portfolio=Portfolio(10000), risk_manager=RiskManager(),
            router=SimpleNamespace(regime_map={"TREND_UP": "Test"}), audit={"reason": "order_accepted"})
        observer.settle_decisions()
        last = event(data, "2020-01-03")
        observer.advance(replace(last, bars={c.symbol: last.bars["X"]}, histories={c.symbol: last.histories["X"]}))
        observer.finish()
        p = observer.export()
        h, cs, _, os = _validate_input(p)
        b = OutcomeRevisionBook(p, cs, h, os)
        assert all(b.eligible(row) for row in b.as_of("2020-01-04").values())
        assert p["temporal_label_protocol"]["financing_identity"] == provider.identity
        return p
    rows = funding_records()
    prefix, full = run(rows), run(rows+[revise(rows[1], funding_rate=.1)])
    assert prefix["candidates"] == full["candidates"]
    assert prefix["outcomes"] == full["outcomes"]
    assert prefix["temporal_label_protocol"]["financing_identity"] != full["temporal_label_protocol"]["financing_identity"]


def test_borrow_revision_and_tombstone_preserve_exact_old_cost_ledger():
    rows = borrow_records()
    provider, tracker, data, c = build(rows, account="spot_margin", direction="short")
    advance(tracker, data, "2020-01-03", c)
    old = tracker.results[0]
    provider.ingest([revise(rows[1], annual_rate=.73)])
    advance(tracker, data, "2020-01-06", c)
    assert tracker.results[0]["carry"] == pytest.approx(old["carry"]+.5)
    provider.ingest([tombstone(rows[2], at="2020-01-08")])
    advance(tracker, data, "2020-01-08", c)
    b = book(tracker, c)
    assert b.as_of("2020-01-06")["c", 1] == old
    assert not b.eligible(b.as_of("2020-01-09")["c", 1])


def test_financing_revision_cannot_move_logical_event_or_coverage_window():
    rows = funding_records()
    provider = TemporalFinancing("financing", rows)
    original = provider.export()
    moved = revise(rows[-1], start=iso("2020-01-01"))
    with pytest.raises(ValueError, match="logical event identity"):
        provider.ingest([moved])
    moved = revise(rows[1])
    moved["event_time"] = moved["data"]["settlement_time"] = iso("2020-01-02 16:00")
    with pytest.raises(ValueError, match="logical event identity"):
        provider.ingest([moved])
    assert provider.export() == original
