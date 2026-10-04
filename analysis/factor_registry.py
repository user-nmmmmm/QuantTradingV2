"""Versioned factor definitions: a proxy cannot silently become a full factor.

Definitions describe required inputs and timing; they do not establish that
inputs actually satisfy those requirements or that observed alpha is causal.
"""
from __future__ import annotations

import hashlib
import json
from typing import Mapping
from urllib.parse import urlparse

from core.data_versions import DataVersionStore


def validate_factor_definition(definition: Mapping) -> dict:
    result = dict(definition)
    for name in ("factor_id", "version", "description", "formula", "formula_version",
                 "availability_rule", "universe_rule"):
        if not isinstance(result.get(name), str) or not result[name].strip():
            raise ValueError(f"factor definition requires {name}")
    if result.get("status") not in {"proxy", "full", "unavailable"}:
        raise ValueError("factor status must be proxy, full, or unavailable")
    sources = result.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("factor sources are required")
    for source in sources:
        if (not isinstance(source, Mapping) or not source.get("paper_id")
                or urlparse(str(source.get("url", ""))).scheme != "https"
                or not urlparse(str(source.get("url", ""))).netloc):
            raise ValueError("each factor source requires paper_id and HTTPS url")
    inputs = result.get("inputs")
    if (not isinstance(inputs, list) or not inputs
            or any(not isinstance(item, Mapping) or not item.get("name")
                   or not item.get("timing_rule") for item in inputs)):
        raise ValueError("factor inputs require names and timing rules")
    if not isinstance(result.get("parameters"), Mapping):
        raise ValueError("factor parameters must be a mapping")
    if not isinstance(result.get("limitations"), list):
        raise ValueError("factor limitations must be a list")
    if result["status"] in {"proxy", "unavailable"} and not result["limitations"]:
        raise ValueError("proxy/unavailable definitions must state limitations")
    result.pop("definition_id", None)
    result["schema"] = "research_factor_definition/v1"
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode("utf-8")
    return {"definition_id": hashlib.sha256(raw).hexdigest(), **json.loads(raw)}


def default_proxy_definitions() -> list[dict]:
    """Explicit fixed BTC/ETH research proxies, plus the unavailable size factor."""
    sources = [{"paper_id": 8, "url": "https://www.nber.org/papers/w25882"},
               {"paper_id": 105, "url": "https://www.federalreserve.gov/econres/feds/open-source-cross-sectional-asset-pricing.htm"}]
    common = {
        "version": "1.0.0", "sources": sources,
        "universe_rule": "Fixed BTC/USDT and ETH/USDT subset; no historical full-market eligibility claim",
        "availability_rule": "Use only closed bars known before position formation; historical source availability must be audited separately",
        "inputs": [{"name": "close", "timing_rule": "return ends at t; any portfolio weights at t must be fixed before t"}],
        "status": "proxy",
        "limitations": ["Fixed two-asset subset is not the paper's market/size/momentum factor universe",
                        "Historical publication and membership evidence may be unknown"],
    }
    return [validate_factor_definition({
        **common, "factor_id": "btc_eth_equal_weight_market_proxy",
        "description": "Equal-weight average simple return of the fixed BTC/ETH subset",
        "formula_version": "paper_study.factor_proxies/v1",
        "formula": "market[t] = (r_BTC[t] + r_ETH[t]) / 2; r_i[t] = close_i[t]/close_i[t-1]-1",
        "parameters": {"weighting": "equal", "return_type": "simple", "risk_free_rate": 0.0},
    }), validate_factor_definition({
        **common, "factor_id": "btc_eth_lagged60_momentum_spread_proxy",
        "description": "Long the higher prior 60-bar-return asset, short the lower, with one-bar lag and unit gross exposure",
        "formula_version": "paper_study.factor_proxies/v1",
        "formula": "momentum_spread[t] = sign(m_BTC[t-1]-m_ETH[t-1])*(r_BTC[t]-r_ETH[t])/2; m_i[t] = close_i[t]/close_i[t-60]-1",
        "parameters": {"lookback_bars": 60, "signal_lag_bars": 1, "weighting": "equal",
                       "long_leg_weight": 0.5, "short_leg_weight": -0.5, "tied_momentum_weight": 0},
        "limitations": common["limitations"] + ["Long/short return series is a diagnostic factor, not an executable spot portfolio",
                                                  "Trading, borrowing and financing costs are not deducted"],
    }), validate_factor_definition({
        **common, "factor_id": "crypto_market_cap_size_factor", "status": "unavailable",
        "description": "Size factor from historical market capitalization and a contemporaneously eligible cryptocurrency universe",
        "formula_version": "not_implemented",
        "formula": "Unavailable; paper-specific market-cap sorts require a separate registered definition and input evidence",
        "inputs": [{"name": "historical_market_cap", "timing_rule": "publication timestamp at/before formation"},
                   {"name": "historical_membership", "timing_rule": "eligibility and announcement known at/before formation"}],
        "parameters": {"volume_is_market_cap": False},
        "limitations": ["Required market capitalization and membership histories are unavailable",
                        "Volume must not be substituted for market capitalization"],
    })]


def freeze_factor_registry(root, definitions, *, observed_at, code_refs=None,
                           config_refs=None) -> dict:
    """Persist immutable versions; changed definitions require changed version IDs."""
    records = []
    for definition in definitions:
        normalized = validate_factor_definition(definition)
        records.append({"record_id": normalized["factor_id"], "revision_id": normalized["version"],
                        "event_time": observed_at, "observed_at": observed_at,
                        "available_at": None, "data": normalized})
    return DataVersionStore(root).create_snapshot(
        "research-factor-registry", records, code_refs=code_refs, config_refs=config_refs,
        metadata={"meaning": "research definition versions; not historical factor-data availability"})


def historical_input_proxy_definitions(assets=("ada", "btc", "eth", "ltc", "sol", "xrp")) -> list[dict]:
    """Separate IDs preserve the meaning of already frozen BTC/ETH definitions."""
    common = {"version": "1.0.0", "status": "proxy",
        "formula_version": "historical_factor_inputs/v1",
        "sources": [{"paper_id": 8, "url": "https://www.nber.org/papers/w25882"},
                    {"paper_id": "provider_methodology", "url": "https://github.com/coinmetrics/docs-website/blob/master/asset-metrics/market/capmrktcurusd.md"}],
        "availability_rule": "Receipt availability only; historical factor values use retrospective captures and are not historical PIT signals",
        "universe_rule": "Explicit fixed cohort, never inferred from present catalog or current exchange listing",
        "inputs": [{"name": "PriceUSD", "timing_rule": "Provider daily close is next midnight; publication remains unknown"},
                   {"name": "CapMrktCurUSD", "timing_rule": "Use preceding daily value as weight/sort; actual capture availability retained"},
                   {"name": "SplyCur", "timing_rule": "Validate provider cap identity; current supply is not circulating/free-float supply"}],
        "parameters": {"assets": sorted(assets), "signal_lag_days": 1, "volume_is_market_cap": False},
        "limitations": ["Fixed surviving asset subset and incomplete historical membership evidence",
                        "Current-supply capitalization differs from paper circulating-market-cap conventions",
                        "Historical publication and revision availability not established",
                        "Diagnostic returns exclude execution, borrowing, and financing costs"]}
    return [validate_factor_definition({**common, "factor_id": "cm_fixed_cohort_cap_weighted_market_proxy",
        "description": "Prior-cap weighted daily market return of registered fixed cohort",
        "formula": "sum_i(Cap_i[t-1]/sum_j(Cap_j[t-1]) * (Price_i[t]/Price_i[t-1]-1)); no missing-data reweighting"}),
        validate_factor_definition({**common, "factor_id": "cm_fixed_cohort_size_proxy",
        "description": "Prior-cap bottom-half minus top-half return, unit gross exposure",
        "formula": "0.5*(mean(r_small[t])-mean(r_big[t])); prior Cap rank, equal-weight legs; odd cohort excludes median; no missing-data reweighting"}),
        validate_factor_definition({**common, "factor_id": "cm_fixed_cohort_momentum60_proxy",
        "description": "Prior 60-day return winner-minus-loser spread, unit gross exposure",
        "formula": "0.5*(mean(r_winner[t])-mean(r_loser[t])); sort Price[t-1]/Price[t-61]-1, require all intervening days; odd cohort excludes median; boundary ties zero",
        "parameters": {**common["parameters"], "lookback_days": 60}})]
