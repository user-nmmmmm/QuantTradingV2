"""Run the registered fixed three-asset retrospective portfolio controls.

No new model training, final-window evaluation or real orders. Output is new,
source bytes and raw CSV references are fixed, and every arm stays journaled.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time


import pandas as pd

from analysis.paper_portfolio_research import (
    covariance_risk_comparison,
    factor_attribution_general,
    run_covariance_portfolio_study,
    run_allocation_portfolio_study,
)
from analysis.research_evidence import source_identity, panel_result_binding
from analysis.search_scope import SearchScope, data_manifest
from core.foundation.reproducibility import (
    canonical_json,
    canonical_frame_csv,
    sha256_bytes,
    sha256_file,
    sha256_frame,
)
from scripts.capture_paper_research_baseline import snapshot_sources


ROOT = Path(__file__).resolve().parents[1]


def _write(path, value):
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _digest(value):
    return sha256_bytes(canonical_json(value).encode())


def _read_fixed_window(path, start, end):
    """Stop after the last declared row; never parse later prices for this study."""
    lower, upper = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    rows = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            at = pd.Timestamp(row["timestamp"])
            at = at.tz_localize("UTC") if at.tzinfo is None else at.tz_convert("UTC")
            if at < lower:
                continue
            if at > upper:
                break
            rows.append(
                {
                    "timestamp": at,
                    **{k: float(row[k]) for k in ("open", "high", "low", "close", "volume")},
                }
            )
            if at == upper:
                break
    if not rows:
        raise ValueError("No declared historical data: " + str(path))
    frame = pd.DataFrame(rows).set_index("timestamp")
    expected = pd.date_range(lower, upper, freq="D")
    if not frame.index.equals(expected):
        raise ValueError("Missing or duplicate declared historical bars: " + str(path))
    return frame


def _protocol(config, *, family, source_hashes, inputs, config_path, study_id):
    cov = family == "covariance"
    return {
        "schema": "paper-research-protocol/v1",
        "study_id": study_id,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L05"] if cov else ["L04", "L07"],
        "source_manifest_sha256": _digest(source_hashes),
        "config_sha256": sha256_file(config_path),
        "data_manifest_sha256": _digest(data_manifest(inputs)),
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": ["sample", "lw_constant_correlation"]
        if cov
        else ["provider_immediate", "provider_partial", "provider_band"],
        "benchmark_ids": ["fixed_diag_020"] if cov else ["native_constant_immediate"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": "absolute_daily_variance_forecast_error"
        if cov
        else "absolute_Broker_equity_bridge_error",
        "economic_or_precision_target": (
            {
                "target": "Report finite later-block variance errors for every fixed arm without a superiority decision",
                "rationale": "Descriptive engineering comparison; no economic improvement or inferential excess-return claim is tested",
            }
            if cov
            else {
                "threshold": 1e-7,
                "units": "quote_currency",
                "rationale": "Independent real Broker equity bridge reconciliation",
            }
        ),
        "training_selection_rule": "No training or candidate selection; all declared arms retained",
        "evaluation_windows": [
            {
                "id": "already_seen_2024",
                "role": "retrospective",
                "start": config["evaluation_start"],
                "end": config["evaluation_end"],
            }
        ],
        "cost_policy_id": "same_Broker_10bps_fee_5bps_slippage_2bps_spread_bar_impact",
        "risk_policy_id": "cash_long_only_gross090_same_starting_capital",
        "resampling": {
            "method": "not_applicable",
            "reason": "Descriptive risk and account compatibility, no registered inferential excess-return claim",
        },
        "attempt_budget": config["attempt_budgets"][family],
        "resource_budget": config["resource_budget"],
        "stop_rules": config["stop_rules"],
        "final_access_policy_id": None,
    }


def run(output, *, config_path=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    config_path = Path(config_path or ROOT / "config/paper_portfolio_research_20261027.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    expected_contract = {
        "lookback": 60,
        "risk_forecast_horizon_days": 20,
        "max_gross": 0.90,
        "forecast_horizon_days": 7,
        "forecast_cost_bps": 20.0,
        "actual_commission_rate": 0.001,
        "actual_slippage_rate": 0.0005,
        "actual_spread_bps": 2.0,
    }
    if any(config.get(k) != v for k, v in expected_contract.items()):
        raise ValueError(
            "unsupported change to the fixed study contract; register a separate study"
        )
    if config["covariance_candidates"] != [
        "sample",
        "fixed_diag_020",
        "lw_constant_correlation",
    ] or config["allocation_candidates"] != [
        "native_constant_immediate",
        "provider_immediate",
        "provider_partial",
        "provider_band",
    ]:
        raise ValueError("candidate family differs from the fixed study implementation")
    started = time.monotonic()
    frames = {}
    raw = {}
    for symbol in config["symbols"]:
        path = ROOT / "data/binance/1d" / (symbol.replace("/", "_") + ".csv")
        raw[path.relative_to(ROOT).as_posix()] = sha256_file(path)
        frames[symbol] = _read_fixed_window(path, config["input_start"], config["evaluation_end"])
    inputs = {**frames, "raw_source_file_hashes": raw}
    (output / "inputs").mkdir()
    input_snapshots = {}
    for symbol, frame in frames.items():
        destination = output / "inputs" / (symbol.replace("/", "_") + ".csv")
        destination.write_text(canonical_frame_csv(frame), encoding="utf-8", newline="")
        if sha256_file(destination) != sha256_frame(frame):
            raise ValueError("canonical input snapshot differs from registered frame")
        input_snapshots[symbol] = {
            "path": destination.relative_to(output).as_posix(),
            "sha256": sha256_file(destination),
        }
    _write(output / "input_snapshots.json", input_snapshots)
    source_hashes = source_identity(ROOT)
    snapshot_sources(ROOT, output / "source", source_hashes)
    _write(output / "source_manifest.json", source_hashes)
    _write(output / "raw_source_manifest.json", raw)
    _write(output / "config.json", config)
    bundles = {}
    for family in ("covariance", "allocation"):
        protocol = _protocol(
            config,
            family=family,
            source_hashes=source_hashes,
            inputs=inputs,
            config_path=config_path,
            study_id=output.name + "-" + family,
        )
        scope = SearchScope.register(
            protocol=protocol,
            data_map=inputs,
            output=output / (family + "-evidence"),
            source_root=ROOT,
        )
        accounts = {}

        def event(
            arm, status, value, *, family=family, protocol=protocol, accounts=accounts, scope=scope
        ):
            if time.monotonic() - started > config["resource_budget"]["wall_time_seconds"]:
                raise TimeoutError("registered engineering resource budget exceeded")
            if status == "completed":
                target = output / family / arm
                target.mkdir(parents=True, exist_ok=False)
                for name in ("equity", "fills", "decisions", "execution_audit", "financing"):
                    value[name].to_csv(target / (name + ".csv"), float_format="%.17g")
                account = {
                    **value["accounting"],
                    "return_basis": "net_account_equity_returns",
                    "capital_policy_id": "fixed_cash_10000",
                    "cost_policy_id": protocol["cost_policy_id"],
                    "risk_policy_id": protocol["risk_policy_id"],
                    "timeframe": "1d",
                    "currency": "USDT",
                    "account_trace_sha256": sha256_frame(value["equity"]),
                }
                accounts[arm] = account
                _write(target / "accounting.json", account)
                _write(target / "summary.json", value["summary"])
                scope.record(
                    candidate=arm,
                    status="completed",
                    **panel_result_binding(value["returns"], account),
                )
            elif status in ("failed", "interrupted"):
                scope.record(
                    candidate=arm, status=status, reason=f"{type(value).__name__}: {value}"
                )
            else:
                scope.record(candidate=arm, status=status)

        try:
            if family == "covariance":
                bundle = run_covariance_portfolio_study(
                    frames,
                    lookback=config["lookback"],
                    target_annual_volatility=config["target_annual_volatility"],
                    initial_capital=config["initial_capital"],
                    start=config["evaluation_start"],
                    end=config["evaluation_end"],
                    on_arm=event,
                )
            else:
                bundle = run_allocation_portfolio_study(
                    frames,
                    lookback=config["lookback"],
                    initial_capital=config["initial_capital"],
                    start=config["evaluation_start"],
                    end=config["evaluation_end"],
                    on_arm=event,
                )
        except BaseException as error:
            _write(
                output / (family + "-failure.json"),
                {"engineering": "fail", "reason": str(error), "audit": scope.audit()},
            )
            raise
        audit = scope.audit()
        if (
            bundle["engineering"] != "pass"
            or audit["status"] != "ok"
            or not audit["results_complete"]
        ):
            raise ValueError("account or source/family check failed")
        summaries = {
            arm: {"summary": v["summary"], "accounting": accounts[arm]}
            for arm, v in bundle["arms"].items()
        }
        bundles[family] = {k: v for k, v in bundle.items() if k != "arms"}
        bundles[family].update(results=summaries, scope_audit=audit)
        _write(output / (family + "-report.json"), bundles[family])
        if family == "covariance":
            covariance_accounts = bundle["arms"]
    closes = pd.DataFrame({s: f.close for s, f in frames.items()})
    returns = closes.pct_change(fill_method=None).iloc[1:]
    risk = covariance_risk_comparison(
        returns,
        lookback=config["lookback"],
        horizon=config["risk_forecast_horizon_days"],
        target_annual_volatility=config["target_annual_volatility"],
        evaluation_start=config["evaluation_start"],
    )
    _write(output / "risk-forecast.json", risk)
    lagged = closes.pct_change(config["lookback"], fill_method=None).shift(1)
    ranks = lagged.rank(axis=1, method="first")
    momentum_weights = (
        (ranks == len(config["symbols"])).astype(float) - (ranks == 1).astype(float)
    ) / 2
    dates = covariance_accounts["fixed_diag_020"]["returns"].index
    factors = pd.DataFrame(
        {
            "equal_weight_market_proxy": returns.mean(axis=1),
            "lagged_cross_section_momentum_proxy": (returns * momentum_weights).sum(axis=1),
        },
        index=returns.index,
    ).loc[dates]
    attribution = factor_attribution_general(
        covariance_accounts["fixed_diag_020"]["returns"],
        factors,
        factor_ids=config["factor_ids"],
        factor_versions=config["factor_versions"],
        iterations=500,
        block_length=20,
        seed=42,
        definition_scope="proxy",
        factor_cost_basis="gross_proxy_without_fees",
        financing_basis="long_short_proxy_financing_not_modeled",
    )
    _write(output / "factor-attribution.json", attribution)
    final = {
        "schema": "paper-portfolio-study-acceptance/v1",
        "engineering": "pass",
        "research": "insufficient",
        "source_manifest_sha256": _digest(source_hashes),
        "work_packages": ["PR25-11", "PR25-12", "PR25-13", "PR25-14"],
        "families": bundles,
        "risk_forecast": risk["summary"],
        "factor_attribution": attribution,
        "conditional_extension": {
            "PR25-15": "not_triggered_no_qualified_real_costs_or_validated_single_period_bottleneck"
        },
        "elapsed_seconds": time.monotonic() - started,
        "external_evidence": "pending",
        "live_authorization": False,
        "limitations": [
            "Fixed already-seen BTC/ETH/SOL panel; no historical all-market membership proof",
            "All account returns are original Broker results; forecast costs never posted to account",
            "Descriptive comparison, no risk-matched economic improvement claim or new strategy selection",
            "Daily bars use retrospective nominal-close timing; no historical publication/observation proof",
            "Local source/inputs and attempt budgets checked; generic process/memory enforcement remains the separate study runner",
        ],
    }
    final["artifact_hashes"] = {
        p.relative_to(output).as_posix(): sha256_file(p)
        for p in output.rglob("*")
        if p.is_file() and not p.is_relative_to(output / "source")
    }
    _write(output / "acceptance.json", final)
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    logging.getLogger("core.broker.fill_service").setLevel(logging.WARNING)
    result = run(args.output)
    print(
        json.dumps(
            {
                "engineering": result["engineering"],
                "research": result["research"],
                "elapsed_seconds": result["elapsed_seconds"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
