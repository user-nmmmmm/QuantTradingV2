import json

import pandas as pd
import pytest

from analysis.historical_factor_inputs import (
    coinmetrics_records, freeze_factor_inputs, build_factor_proxies, lifecycle_evidence_records, public_metric_groups,
)
from analysis.factor_registry import historical_input_proxy_definitions, default_proxy_definitions
from core.data_versions import DataVersionStore
from scripts.collect_factor_evidence import collect, PublicArchive, BASE, DOCS, sha


START, END = "2020-01-01T00:00:00Z", "2020-01-07T00:00:00Z"
OBSERVED = "2026-10-03T17:00:00Z"


def payload(assets=("btc", "eth")):
    rows = []
    for i, asset in enumerate(assets):
        for day, point in enumerate(pd.date_range(START, periods=6)):
            price = 100 * (1 + (i+1) * .01) ** day
            rows.append({"asset": asset, "time": point.isoformat(), "PriceUSD": str(price),
                         "SplyCur": str(i + 1), "CapMrktCurUSD": str(price * (i+1))})
    return {"data": rows}


def adapt(data=None):
    data = data or payload()
    return coinmetrics_records(data, source_url=BASE + "timeseries/asset-metrics?assets=btc,eth",
        raw_sha256="a" * 64, observed_at=OBSERVED, available_at=OBSERVED,
        assets=("btc", "eth"), start=START, end_exclusive=END)


def test_current_capture_cannot_be_queried_as_historical_pit(tmp_path):
    rows = adapt()
    store = DataVersionStore(tmp_path)
    frozen = freeze_factor_inputs(tmp_path, rows, source_refs=[])
    assert store.as_of(frozen["snapshot_id"], "2020-01-07T00:00:00Z") == []
    assert len(store.as_of(frozen["snapshot_id"], OBSERVED)) == 12
    assert all(r["published_at"] is None and r["revision_at"] is None for r in rows)
    assert rows[0]["event_time"] == "2020-01-02T00:00:00+00:00"
    assert all(r["data"]["membership_status"] == "unknown" for r in rows)


def test_missing_cap_is_not_replaced_with_volume_or_supply():
    data = payload()
    data["data"][0].pop("CapMrktCurUSD")
    data["data"][0]["volume"] = "99999999"
    row = adapt(data)[0]["data"]
    assert row["market_cap_usd"] is None and row["metric_status"]["CapMrktCurUSD"] == "unsupported"


@pytest.mark.parametrize("change,match", [
    ({"asset": "unknown"}, "unexpected asset"),
    ({"PriceUSD": "nan"}, "finite"),
    ({"time": "2020-01-01"}, "aware timestamp"),
    ({"time": "2020-01-01T12:00:00Z"}, "daily timestamp"),
])
def test_bad_source_values_fail_closed(change, match):
    data = payload(); data["data"][0].update(change)
    with pytest.raises(ValueError, match=match):
        adapt(data)


def test_revision_identity_changes_with_provider_revision_and_capture():
    original = adapt()
    data = payload(); data["data"][0]["PriceUSD"] = "101"
    changed = adapt(data)
    assert original[0]["record_id"] == changed[0]["record_id"]
    assert original[0]["revision_id"] != changed[0]["revision_id"]
    assert changed[0]["data"]["quality_issues"] == ["provider_market_cap_identity_mismatch"]


def test_lagged_factors_hand_calculation_prefix_and_missing_day():
    rows = adapt()
    frame, summary = build_factor_proxies(rows, assets=("btc", "eth"), start=START, end_exclusive=END, momentum_days=2)
    assert frame.market_cap_weighted_proxy.iloc[1] == pytest.approx(.01 / 3 + .02 * 2 / 3)
    assert frame.size_small_minus_big_proxy.iloc[1] == pytest.approx(-.005)
    assert frame.momentum_proxy.iloc[3] == pytest.approx(.005)
    assert summary["full_factor_status"] == "unavailable" and not frame.historical_pit_eligible.any()
    changed = payload()
    for row in changed["data"]:
        if row["time"].startswith("2020-01-06"):
            row["PriceUSD"] = str(float(row["PriceUSD"]) * 2)
            row["CapMrktCurUSD"] = str(float(row["CapMrktCurUSD"]) * 2)
    future, _ = build_factor_proxies(adapt(changed), assets=("btc", "eth"), start=START, end_exclusive=END, momentum_days=2)
    pd.testing.assert_frame_equal(frame.iloc[:5], future.iloc[:5])
    missing = [r for r in rows if not (r["data"]["asset"] == "btc" and r["data"]["source_time"].startswith("2020-01-03"))]
    gap, _ = build_factor_proxies(missing, assets=("btc", "eth"), start=START, end_exclusive=END, momentum_days=2)
    assert pd.isna(gap.market_cap_weighted_proxy.iloc[2]) and pd.isna(gap.market_cap_weighted_proxy.iloc[3])
    assert pd.isna(gap.momentum_proxy.iloc[4])


def test_legacy_lifecycle_retains_scope_and_unknown_historical_membership():
    manifest = {"current_instruments": [{"symbol": "BTC/USDT", "venue": "binance", "current_state": "TRADING"}],
                "selected_events_non_exhaustive": [{"symbol": "ETH/USDT", "event": "withdrawal suspension", "spot_trading_halt_proven": False}]}
    rows = lifecycle_evidence_records(manifest, raw_sha256="b"*64, observed_at=OBSERVED, source_reference="manifest.json")
    assert len(rows) == 2 and all(r["data"]["membership_eligible"] is None for r in rows)
    assert rows[1]["data"]["spot_trading_halt_proven"] is False


def test_new_registry_preserves_old_definitions_and_proxy_scope():
    old = default_proxy_definitions()
    new = historical_input_proxy_definitions()
    assert len(new) == 3 and all(d["status"] == "proxy" for d in new)
    assert old[-1]["status"] == "unavailable"
    assert all(not d["parameters"]["volume_is_market_cap"] for d in new)


def test_collection_produces_store_and_proxy_artifact_without_promoting_cohort(tmp_path):
    from analysis.historical_factor_inputs import DEFAULT_ASSETS
    class FakeArchive:
        calls = 0
        def fetch(self, name, url):
            self.calls += 1
            data = payload(DEFAULT_ASSETS) if name.startswith("metrics_group") else {"data": []}
            if name == "current_provider_coverage":
                data = {"data": [{"asset": asset, "metrics": [{"metric": metric, "frequencies": [{"frequency": "1d", "community": True}]} for metric in ("PriceUSD", "SplyCur", "CapMrktCurUSD")]} for asset in DEFAULT_ASSETS]}
            body = json.dumps(data).encode()
            return body, {"url": url, "body_sha256": sha(body), "observed_at": OBSERVED, "available_at": OBSERVED,
                          "status": 200, "error_type": None}
    result = collect(tmp_path / "evidence", start=START, end_exclusive=END, archive=FakeArchive())
    assert result["status"] == "limited_evidence_complete" and result["input_rows"] == 36
    assert not result["full_universe_rebuilt"] and result["full_factor_status"] == "unavailable"
    assert result["factor_production"]["market_proxy_rows"] == 5


def test_public_archive_rejects_unregistered_hosts_and_has_anonymous_auth(tmp_path):
    archive = PublicArchive(tmp_path)
    with pytest.raises(ValueError, match="unregistered"):
        archive.fetch("bad", "https://other.example/private")
    prepared = requests.Request("GET", BASE, headers={"Authorization": "not-a-real-secret"}).prepare()
    archive.session.auth(prepared)
    assert "Authorization" not in prepared.headers and archive.session.trust_env
    archive.close()


def test_entitlement_filter_never_requests_private_or_unproven_metrics():
    catalog = {"data": [{"asset": "sol", "metrics": [
        {"metric": "PriceUSD", "frequencies": [{"frequency": "1d", "community": True}]},
        {"metric": "SplyCur", "frequencies": [{"frequency": "1d", "community": False}]},
        {"metric": "CapMrktCurUSD", "frequencies": [{"frequency": "1d"}]}]}]}
    assert public_metric_groups(catalog) == [{"assets": ["sol"], "metrics": ["PriceUSD"]}]


import requests
