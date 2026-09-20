"""Synthetic inputs test account-source wiring; none are real account evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from core.account_source import AccountSourceError, PinnedAccountExport
from core.domain import FillRecord, OrderIntent, OrderStatus
from core.live_broker import LiveBroker
from core.order_store import OrderStore
from core.portfolio import Portfolio


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
START = (NOW - timedelta(days=1)).isoformat()
FILL_AT = (NOW - timedelta(hours=1)).isoformat()
IDENTITY = {"exchange": "binance", "environment": "sandbox", "account": "fixture-account",
            "market_type": "spot", "base_currency": "USDT"}


@pytest.fixture
def account(tmp_path):
    store = OrderStore(str(tmp_path / "orders.db"))
    intent = OrderIntent("binance", "fixture-account", "BTC/USDT", "1h", FILL_AT,
                         "FixtureStrategy", "buy", 0, 2, reference_price=100, initial_stop=90,
                         approved_risk_amount=20)
    store.create_intent(intent, FILL_AT)
    store.add_fill(FillRecord("fixture-fill", intent.client_order_id, "venue-order", 2, 100,
                             fee=2, fee_currency="USDT", timestamp=FILL_AT,
                             payload={"id": "fixture-fill"}, symbol="BTC/USDT", side="buy"))
    store.transition(intent.client_order_id, OrderStatus.FILLED, FILL_AT,
                     exchange_order_id="venue-order", filled_qty=2, remaining_qty=0)
    venue = MagicMock()
    venue.fetch_balance.return_value = {
        "free": {"USDT": 876, "BTC": 2}, "used": {"USDT": 10, "BTC": 0},
        "total": {"USDT": 886, "BTC": 2}}
    with patch("core.live_broker.ccxt.binance", return_value=venue):
        broker = LiveBroker(Portfolio(999999), account_id="fixture-account", order_store=store,
                            clock=lambda: NOW, retry_max_attempts=1)
    data = {
        "schema_version": 1, "source_id": "fixture-export", "evidence_kind": "synthetic_fixture",
        "identity": deepcopy(IDENTITY), "captured_at": NOW.isoformat(),
        "coverage": {"from": START, "through": NOW.isoformat(), "complete": True, "scope": "entire_account"},
        "opening_capital": {"at": START, "amount": 1000, "currency": "USDT",
                            "source_id": "fixture-opening-statement", "positions": []},
        "financing": [{"record_id": "finance-1", "at": FILL_AT, "amount": 2,
                       "currency": "USDT", "source_id": "fixture-financing"}],
        "snapshot": {
            "captured_at": NOW.isoformat(), "identity": deepcopy(IDENTITY),
            "cash": {"free": 876, "locked": 10, "total": 886},
            "positions": [{"record_id": "BTC/USDT", "qty": 2, "mark_price": 110,
                           "price_source": "fixture-mark", "price_at": NOW.isoformat(),
                           "price_currency": "USDT", "max_price_age_seconds": 90}],
            "orders": [{"record_id": intent.client_order_id, "exchange_order_id": "venue-order",
                        "symbol": "BTC/USDT", "side": "buy", "status": "filled",
                        "requested_qty": 2, "filled_qty": 2, "remaining_qty": 0}],
            "fills": [{"record_id": "fixture-fill", "order_id": intent.client_order_id,
                       "symbol": "BTC/USDT", "side": "buy", "qty": 2, "price": 100, "at": FILL_AT,
                       "fee_amount": 2, "fee_currency": "USDT", "fee_base_amount": 2,
                       "fee_conversion_rate": 1, "fee_conversion_source": "quote_currency",
                       "fee_conversion_at": FILL_AT}],
            "cashflows": [
                {"record_id": "deposit", "kind": "deposit", "at": FILL_AT, "amount": 100,
                 "currency": "USDT", "source_id": "fixture-transfer-export"},
                {"record_id": "withdrawal", "kind": "withdrawal", "at": FILL_AT, "amount": -10,
                 "currency": "USDT", "source_id": "fixture-transfer-export"}],
            "equity": 1106,
            "capital_bridge": {"initial_capital": 1000, "net_cashflows": 90,
                               "realized_gross_pnl": 0, "unrealized_gross_pnl": 20,
                               "costs": 4, "financing_costs": 2}}}
    yield broker, venue, data
    broker.close()


def configure(broker, data, tmp_path, **kwargs):
    path = tmp_path / "source.json"
    raw = json.dumps(data, allow_nan=False, sort_keys=True).encode()
    path.write_bytes(raw)
    source = PinnedAccountExport(path, sha256=hashlib.sha256(raw).hexdigest(),
                                source_id="fixture-export", evidence_kind=data["evidence_kind"], **kwargs)
    broker.configure_account_source(source, environment="sandbox")
    return source


def test_full_source_projects_authoritative_facts_without_initial_capital_guess(account, tmp_path):
    broker, venue, data = account
    configure(broker, data, tmp_path)
    before = broker.order_store.list_all()
    report = broker.reconcile_full_account()
    assert report["ok"], report
    assert report["equity_bridges"]["expected"]["capital_cashflow_pnl_less_costs"] == 1106
    assert not report["production_account_source_verified"]
    assert not report["allows_new_risk"]
    assert report["source"]["evidence_kind"] == "synthetic_fixture"
    assert broker.order_store.list_all() == before
    assert broker.portfolio.initial_capital == 999999
    venue.fetch_balance.assert_not_called()
    venue.create_order.assert_not_called()


def test_configured_sync_calls_full_account_gate_and_compares_raw_balance(account, tmp_path):
    broker, venue, data = account
    configure(broker, data, tmp_path)
    assert broker.sync()
    assert broker.account_reconciliation_report["ok"]
    assert broker.account_reconciliation_report["venue_balance_observation_compared"]
    venue.fetch_balance.return_value["free"]["USDT"] += 1
    venue.fetch_balance.return_value["total"]["USDT"] += 1
    assert not broker.sync()
    assert "observed_account_cash_differs_from_export" in broker.account_reconciliation_report["issues"]
    assert not broker.account_reconciliation_report["allows_new_risk"]


def test_missing_source_keeps_legacy_balance_sync_but_never_grants_full_account_risk(account):
    broker, _, _ = account
    assert broker.sync()
    assert not broker.account_reconciliation_report["ok"]
    assert not broker.reconcile_full_account()["allows_new_risk"]


@pytest.mark.parametrize("fault", ["opening", "opening_inventory", "opening_currency", "identity",
    "coverage", "scope", "cashflow_currency", "cashflow_time", "cashflow_opening_boundary", "cashflow_direction", "cashflow_omitted",
    "duplicate_cashflow", "missing_fee", "fee_currency", "fee_conversion", "fee_time", "missing_mark",
    "stale_mark", "future_mark", "mark_currency", "stale_source", "future_source", "extra_position",
    "extra_order", "fee_delta", "unknown_business_field"])
def test_incomplete_mismatched_and_stale_sources_fail_closed(account, tmp_path, fault):
    broker, _, data = account
    snapshot = data["snapshot"]
    if fault == "opening":
        del data["opening_capital"]
    elif fault == "opening_inventory":
        data["opening_capital"]["positions"] = [{"symbol": "BTC/USDT", "qty": 1}]
    elif fault == "opening_currency":
        data["opening_capital"]["currency"] = "USD"
    elif fault == "identity":
        data["identity"]["account"] = "other-account"
    elif fault == "coverage":
        data["coverage"]["from"] = FILL_AT
    elif fault == "scope":
        data["coverage"]["scope"] = "strategy_only"
    elif fault == "cashflow_currency":
        snapshot["cashflows"][0]["currency"] = "BTC"
    elif fault == "cashflow_time":
        snapshot["cashflows"][0]["at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif fault == "cashflow_opening_boundary":
        snapshot["cashflows"][0]["at"] = START
    elif fault == "cashflow_direction":
        snapshot["cashflows"][0]["amount"] = -100
    elif fault == "cashflow_omitted":
        snapshot["cashflows"].pop()
    elif fault == "duplicate_cashflow":
        snapshot["cashflows"].append(deepcopy(snapshot["cashflows"][0]))
    elif fault == "missing_fee":
        del snapshot["fills"][0]["fee_conversion_source"]
    elif fault == "fee_currency":
        snapshot["fills"][0]["fee_currency"] = "USD"
    elif fault == "fee_conversion":
        snapshot["fills"][0]["fee_conversion_rate"] = 2
    elif fault == "fee_time":
        snapshot["fills"][0]["fee_conversion_at"] = START
    elif fault == "missing_mark":
        del snapshot["positions"][0]["mark_price"]
    elif fault == "stale_mark":
        snapshot["positions"][0]["price_at"] = (NOW - timedelta(seconds=91)).isoformat()
    elif fault == "future_mark":
        snapshot["positions"][0]["price_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif fault == "mark_currency":
        snapshot["positions"][0]["price_currency"] = "USD"
    elif fault in {"stale_source", "future_source"}:
        delta = -91 if fault == "stale_source" else 1
        data["captured_at"] = snapshot["captured_at"] = (NOW + timedelta(seconds=delta)).isoformat()
    elif fault == "extra_position":
        snapshot["positions"].append(dict(snapshot["positions"][0], record_id="ETH/USDT"))
    elif fault == "extra_order":
        snapshot["orders"].append(dict(snapshot["orders"][0], record_id="unapproved-venue-order"))
    elif fault == "fee_delta":
        snapshot["fills"][0]["fee_amount"] = snapshot["fills"][0]["fee_base_amount"] = 3
    elif fault == "unknown_business_field":
        snapshot["cash"]["unclassified_debt"] = 1
    configure(broker, data, tmp_path)
    report = broker.reconcile_full_account()
    assert not report["ok"], (fault, report)
    assert not report["allows_new_risk"]
    json.dumps(report, allow_nan=False)


def test_file_tamper_revokes_previous_report(account, tmp_path):
    broker, _, data = account
    source = configure(broker, data, tmp_path)
    assert broker.reconcile_full_account()["ok"]
    source.path.write_bytes(source.path.read_bytes() + b" ")
    report = broker.reconcile_full_account()
    assert report["issues"] == ["source_hash_mismatch"]
    assert not report["allows_new_risk"]


def test_expired_source_revokes_success_even_without_another_venue_call(account, tmp_path):
    broker, venue, data = account
    configure(broker, data, tmp_path)
    assert broker.reconcile_full_account()["ok"]
    assert not broker.reconcile_full_account(checked_at=NOW + timedelta(seconds=91))["ok"]
    venue.fetch_balance.assert_not_called()


def test_source_normalizes_equivalent_utc_timestamps_and_numeric_exports(account, tmp_path):
    broker, _, data = account
    fill = data["snapshot"]["fills"][0]
    fill["at"] = fill["at"].replace("+00:00", "Z")
    fill["fee_conversion_at"] = fill["fee_conversion_at"].replace("+00:00", "Z")
    for key in ("qty", "price", "fee_amount", "fee_base_amount", "fee_conversion_rate"):
        fill[key] = str(fill[key])
    configure(broker, data, tmp_path)
    assert broker.reconcile_full_account()["ok"]


def test_local_fee_without_recorded_evidence_is_rejected(account, tmp_path):
    broker, _, data = account
    configure(broker, data, tmp_path)
    original = broker.order_store.projection_delta
    def missing_fee_proof(*args):
        fills, orders, quantities, cursor = original(*args)
        for row in fills:
            row["fee_evidence"] = "missing"
        return fills, orders, quantities, cursor
    with patch.object(broker.order_store, "projection_delta", side_effect=missing_fee_proof):
        assert not broker.reconcile_full_account()["ok"]


def test_snapshot_unknown_extra_position_is_detected_during_sync(account, tmp_path):
    broker, venue, data = account
    configure(broker, data, tmp_path)
    for key, amount in (("free", 1), ("used", 0), ("total", 1)):
        venue.fetch_balance.return_value[key]["ETH"] = amount
    assert not broker.sync()
    assert "observed_account_positions_differ_from_export" in broker.account_reconciliation_report["issues"]


@pytest.mark.parametrize("kind,verifier_result,verified", [
    ("synthetic_fixture", True, False), ("independent_export", False, False),
    ("independent_export", True, True)])
def test_provenance_is_trusted_external_injection_not_export_flags(account, tmp_path, kind, verifier_result, verified):
    # All inputs remain synthetic. This exercises the verifier contract only.
    broker, _, data = account
    data["evidence_kind"] = kind
    data["production_account_source_verified"] = True
    verifier = MagicMock(return_value=verifier_result)
    source = configure(broker, data, tmp_path, provenance_verifier=verifier)
    report = broker.reconcile_full_account()
    assert report["ok"]
    assert report["production_account_source_verified"] is verified
    assert report["allows_new_risk"] is verified
    if kind == "independent_export":
        verifier.assert_called_once_with(source.sha256, IDENTITY)
    else:
        verifier.assert_not_called()


def test_independent_label_without_verifier_does_not_authorize_production(account, tmp_path):
    broker, _, data = account
    data["evidence_kind"] = "independent_export"
    configure(broker, data, tmp_path)
    assert broker.reconcile_full_account()["ok"]
    assert not broker.account_reconciliation_report["allows_new_risk"]


def test_failed_balance_sync_invalidates_previous_success(account, tmp_path):
    broker, venue, data = account
    configure(broker, data, tmp_path)
    assert broker.sync()
    venue.fetch_balance.return_value = None
    assert not broker.sync()
    assert broker.account_reconciliation_report["issues"] == ["account_balance_sync_failed"]


def test_pinned_source_rejects_duplicate_json_keys(account, tmp_path):
    broker, _, _ = account
    raw = b'{"schema_version":1,"schema_version":1}'
    path = tmp_path / "duplicated.json"
    path.write_bytes(raw)
    source = PinnedAccountExport(path, sha256=hashlib.sha256(raw).hexdigest(), source_id="fixture-export")
    with pytest.raises(AccountSourceError, match="duplicate_json_key"):
        source.read(identity=IDENTITY, checked_at=NOW)


def test_margin_uses_net_cash_and_liabilities_from_observed_account(account, tmp_path):
    broker, venue, data = account
    broker.market_type = "margin"
    data["identity"]["market_type"] = data["snapshot"]["identity"]["market_type"] = "spot_margin"
    venue.fetch_balance.return_value = {"info": {"userAssets": [
        {"asset": "USDT", "free": 978, "locked": 10, "borrowed": 100, "interest": 2, "netAsset": 886},
        {"asset": "BTC", "free": 2, "locked": 0, "borrowed": 0, "interest": 0, "netAsset": 2}]}}
    configure(broker, data, tmp_path)
    assert broker.sync(), broker.account_reconciliation_report
    assert broker.account_reconciliation_report["ok"]
    assert broker.account_reconciliation_report["equity_bridges"]["actual"]["cash_plus_net_position_value"] == 1106
