"""Evidence-aware historical inputs and explicitly retrospective factor proxies.

A present-day API capture cannot establish what its historical revision was at
portfolio formation. Current provider coverage is not exchange membership.
"""
from __future__ import annotations

import hashlib
import json
import math
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from core.data_versions import DataVersionStore


METRICS = {"PriceUSD": "price_usd", "CapMrktCurUSD": "market_cap_usd", "SplyCur": "current_supply"}
DEFAULT_ASSETS = ("ada", "btc", "eth", "ltc", "sol", "xrp")


def public_metric_groups(catalog, *, assets=DEFAULT_ASSETS):
    """Honor explicit community entitlements, never infer historical membership."""
    allowed = {asset: set() for asset in assets}
    for row in catalog.get("data", []):
        asset = row.get("asset")
        if asset not in allowed:
            continue
        for metric in row.get("metrics", []):
            if metric.get("metric") not in METRICS:
                continue
            if any(f.get("frequency") == "1d" and f.get("community") is True for f in metric.get("frequencies", [])):
                allowed[asset].add(metric["metric"])
    groups = {}
    for asset, metrics in allowed.items():
        if metrics:
            groups.setdefault(tuple(sorted(metrics)), []).append(asset)
    return [{"assets": sorted(cohort), "metrics": list(metrics)} for metrics, cohort in sorted(groups.items())]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def timestamp(value):
    at = pd.Timestamp(value)
    if pd.isna(at) or at.tzinfo is None or at.nanosecond:
        raise ValueError("aware timestamp with at most microsecond precision required")
    return at.tz_convert("UTC").isoformat()


def coinmetrics_records(payload, *, source_url, raw_sha256, observed_at, available_at,
                        assets=DEFAULT_ASSETS, start, end_exclusive):
    """Adapt only provider values; missing fields/nulls and publication stay unknown."""
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.netloc != "community-api.coinmetrics.io" or parsed.path != "/v4/timeseries/asset-metrics":
        raise ValueError("registered community asset-metrics source required")
    if len(raw_sha256) != 64 or any(c not in "0123456789abcdef" for c in raw_sha256):
        raise ValueError("raw response SHA256 required")
    observed, available = timestamp(observed_at), timestamp(available_at)
    if pd.Timestamp(available) < pd.Timestamp(observed):
        raise ValueError("receipt availability cannot precede observation")
    start, end = pd.Timestamp(timestamp(start)), pd.Timestamp(timestamp(end_exclusive))
    if not isinstance(payload.get("data"), list):
        raise ValueError("asset-metrics data array required")
    seen, records = set(), []
    for row in payload["data"]:
        asset, point = row.get("asset"), pd.Timestamp(timestamp(row.get("time")))
        if asset not in assets or not start <= point < end or point != point.normalize():
            raise ValueError("unexpected asset, date or daily timestamp")
        key = f"coinmetrics:{asset}:{point.date().isoformat()}"
        if key in seen:
            raise ValueError("duplicate asset/date within response")
        seen.add(key)
        data = {"asset": asset, "source_time": point.isoformat(), "source_url": source_url,
                "raw_sha256": raw_sha256, "source_frequency": "1d", "provider": "coinmetrics",
                "historical_publication_at": None, "historical_revision_at": None,
                "retrospective_only": True, "membership_status": "unknown",
                "market_cap_convention": "SplyCur * PriceUSD; current supply, not free-float or CMC circulating supply",
                "metric_status": {}, "quality_issues": []}
        for metric, name in METRICS.items():
            raw = row.get(metric)
            data["metric_status"][metric] = ("unsupported" if metric not in row else "no_data" if raw is None else "present")
            data[name] = None if raw is None else float(raw)
            if data[name] is not None and (not math.isfinite(data[name]) or data[name] <= 0):
                raise ValueError("positive finite provider metric required")
        if all(data[name] is not None for name in METRICS.values()):
            error = abs(data["market_cap_usd"] / (data["price_usd"] * data["current_supply"]) - 1)
            data["cap_identity_relative_error"] = error
            if error > 1e-6:
                data["quality_issues"].append("provider_market_cap_identity_mismatch")
        # The provider documents PriceUSD as the closing price at next midnight.
        # Event close is not publication; only this capture establishes availability.
        event = point + pd.Timedelta(days=1)
        if event > pd.Timestamp(observed):
            raise ValueError("unclosed metric interval cannot be imported")
        records.append({"record_id": key, "revision_id": "capture:" + digest({"row": row, "raw": raw_sha256,
                        "observed": observed, "available": available}),
            "event_time": event.isoformat(), "observed_at": observed, "available_at": available,
            "published_at": None, "revision_at": None,
            "availability_evidence": {"kind": "local_receipt", "reference": f"{source_url}#sha256={raw_sha256}"},
            "data": data})
    return records


def freeze_factor_inputs(root, records, *, source_refs, code_refs=None):
    return DataVersionStore(root).create_snapshot("coinmetrics-community-factor-inputs", records,
        source_refs={"responses": source_refs}, code_refs=code_refs,
        metadata={"historical_pit_verified": False, "retrospective_only": True,
                  "membership_complete": False, "cap_is_volume": False})


def build_factor_proxies(records, *, assets=DEFAULT_ASSETS, start, end_exclusive, momentum_days=60):
    """Fixed-cohort diagnostics, with lagged weights and no implicit gap filling.

This entry point deliberately cannot emit a full/PIT factor. Its historical
returns use today's captured revisions and cannot be admitted as old signals.
"""
    assets = tuple(sorted(assets))
    if len(set(assets)) != len(assets) or len(assets) < 2 or momentum_days < 2:
        raise ValueError("unique asset cohort and momentum lookback >=2 required")
    index = pd.date_range(timestamp(start), pd.Timestamp(timestamp(end_exclusive)) - pd.Timedelta(days=1), freq="D")
    prices = pd.DataFrame(np.nan, index=index, columns=assets)
    caps, available = prices.copy(), pd.DataFrame(None, index=index, columns=assets)
    seen = set()
    for record in records:
        data = record["data"]
        asset, point = data["asset"], pd.Timestamp(data["source_time"])
        if asset not in assets or point not in index:
            continue
        if (asset, point) in seen:
            raise ValueError("select exactly one captured revision per asset/date before factor production")
        seen.add((asset, point))
        if data.get("quality_issues"):
            continue
        prices.loc[point, asset] = data["price_usd"]
        caps.loc[point, asset] = data["market_cap_usd"]
        available.loc[point, asset] = record["available_at"]
    returns = prices.pct_change(fill_method=None)
    lag_caps = caps.shift(1)
    momentum = prices.shift(1) / prices.shift(momentum_days + 1) - 1
    # A missing day anywhere in the formation lookback invalidates momentum.
    momentum_complete = prices.notna().rolling(momentum_days + 1).sum().shift(1).eq(momentum_days + 1).all(axis=1)
    outcomes = []
    for day in index:
        result = {"timestamp": day.isoformat(), "event_time": (day + pd.Timedelta(days=1)).isoformat(),
            "market_cap_weighted_proxy": None, "size_small_minus_big_proxy": None, "momentum_proxy": None,
            "status": "unavailable", "historical_pit_eligible": False, "universe_complete": False,
            "available_at": None, "reason": "missing_price_or_prior_market_cap", "assets": ",".join(assets)}
        good = returns.loc[day].notna().all() and lag_caps.loc[day].notna().all()
        if good:
            weights = lag_caps.loc[day] / lag_caps.loc[day].sum()
            ranking = sorted(assets, key=lambda asset: (lag_caps.loc[day, asset], asset))
            half = len(assets) // 2
            small, big = ranking[:half], ranking[-half:]
            result.update(market_cap_weighted_proxy=float((weights * returns.loc[day]).sum()),
                size_small_minus_big_proxy=float((returns.loc[day, small].mean() - returns.loc[day, big].mean()) / 2),
                status="proxy", reason="fixed_cohort_current_capture_historical_membership_unknown")
            used = available.loc[:day].tail(momentum_days + 2).stack().dropna()
            if len(used):
                result["available_at"] = max(pd.Timestamp(x) for x in used).isoformat()
            if momentum_complete.loc[day]:
                scores = momentum.loc[day]
                # Boundary ties cannot be resolved using arbitrary asset ordering.
                ranked = sorted(assets, key=lambda asset: (scores[asset], asset))
                if scores[ranked[half-1]] == scores[ranked[-half]]:
                    result["momentum_proxy"] = 0.0
                else:
                    result["momentum_proxy"] = float((returns.loc[day, ranked[-half:]].mean() - returns.loc[day, ranked[:half]].mean()) / 2)
        outcomes.append(result)
    frame = pd.DataFrame(outcomes)
    summary = {"schema": "retrospective-factor-production/v1", "assets": list(assets),
        "rows": len(frame), "market_proxy_rows": int(frame.market_cap_weighted_proxy.notna().sum()),
        "size_proxy_rows": int(frame.size_small_minus_big_proxy.notna().sum()),
        "momentum_proxy_rows": int(frame.momentum_proxy.notna().sum()),
        "unavailable_rows": int((frame.status == "unavailable").sum()),
        "historical_pit_verified": False, "full_factor_status": "unavailable", "survivorship_bias_resolved": False,
        "conventions": {"returns": "Coin Metrics PriceUSD close-to-close simple USD returns",
                        "market_weights": "prior daily current-supply capitalization, unit gross",
                        "size": "prior-cap bottom half minus top half, each leg gross 0.5",
                        "odd_cohort": "median asset excluded from both long/short legs, included in market proxy",
                        "momentum": f"prior {momentum_days}-day return sort, each leg gross 0.5; boundary ties zero",
                        "execution": "diagnostic return series; no executable shorting or costs assumed"}}
    return frame, summary


def lifecycle_evidence_records(manifest, *, raw_sha256, observed_at, source_reference):
    """Retain scoped legacy facts; do not promote margin/withdrawal events to delistings."""
    observed = timestamp(observed_at)
    records = []
    for section in ("existing_events", "current_instruments", "selected_events_non_exhaustive"):
        for i, row in enumerate(manifest.get(section, [])):
            source = row.get("source", {})
            data = {"section": section, "symbol": row.get("symbol"), "venue": row.get("venue"),
                "event": row.get("event"), "status": row.get("status"), "current_state": row.get("current_state"),
                "source_url": source.get("url"), "source_body_sha256": source.get("body_sha256"),
                "announcement_published_at": row.get("announcement_published_at", row.get("announced_at")),
                "effective_at": row.get("effective_at"), "configured_facts": row.get("configured_facts"),
                "historical_version_availability": "unknown", "membership_eligible": None,
                "scope": "current_metadata_only" if section == "current_instruments" else "non_exhaustive_event_corroboration",
                "spot_trading_halt_proven": row.get("spot_trading_halt_proven", False)}
            records.append({"record_id": f"legacy-lifecycle:{section}:{i}", "revision_id": "capture:" + digest(data),
                "event_time": observed, "observed_at": observed, "available_at": observed,
                "availability_evidence": {"kind": "local_receipt", "reference": f"{source_reference}#sha256={raw_sha256}"},
                "data": data})
    return records
