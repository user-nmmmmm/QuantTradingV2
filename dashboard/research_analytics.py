"""Bounded projections of recorded backtest diagnostics and closed round trips.

This module does not run strategies or infer missing pipeline stages from a
quiet equity curve. Every displayed value either names its report field or its
closed-trade calculation, and missing evidence remains unknown.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from dashboard.report_analysis import ReportCache, finite, utc_moment
from dashboard.visual_data import _csv_reader, _csv_rows, _number, _report_run, _safe_file, _signature


_MAX_METRICS_BYTES = 8 * 1024 * 1024
_MAX_CLOSED_ROWS = 100_000
_MAX_TEXT = 256
_MAX_GROUPS = 100
_CACHE = ReportCache(max_entries=8, max_bytes=8 * 1024 * 1024)
_TEXT_COLUMNS = {
    "position_id", "symbol", "strategy", "strategy_id", "entry_time", "exit_time",
    "exit_reason", "exit_strategy", "cost_semantics", "side", "direction",
}
_NUMBER_COLUMNS = {
    "qty", "net_pnl", "gross_pnl", "gross_pnl_theoretical", "commission", "slippage",
    "initial_risk", "mae", "mfe", "entry_price", "exit_price", "legs",
}
_STATUS = {"ok", "insufficient", "insufficient_data", "undefined", "not_modeled", "excluded",
           "not_recorded", "invalid_input", "partial", "descriptive", "unknown", "recorded"}


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return _number(value)
    except OverflowError:
        return None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if 0 <= value <= _MAX_CLOSED_ROWS * 1000 and math.isfinite(value) and int(value) == value:
        return int(value)
    return None


def _status(value: Any, default: str = "unknown") -> str:
    return value if isinstance(value, str) and value in _STATUS else default


def _read_metrics(path: Path) -> tuple[dict, bool]:
    if not _safe_file(path):
        return {}, False
    with path.open("rb") as handle:
        contents = handle.read(_MAX_METRICS_BYTES + 1)
    if len(contents) > _MAX_METRICS_BYTES:
        raise ValueError("diagnostics exceeds dashboard JSON byte limit")
    try:
        payload = json.loads(contents)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("invalid diagnostics JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("invalid diagnostics JSON schema")
    metrics = payload.get("metrics", payload)
    if not isinstance(metrics, dict):
        raise ValueError("invalid diagnostics metrics schema")
    return metrics, True


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    # Dividing before summing avoids overflowing ordinary means of large values.
    return finite(sum(value / len(values) for value in values))


def _sum(values: list[float]) -> float | None:
    return finite(sum(values))


def _holding_hours(row: dict) -> float | None:
    try:
        hours = (utc_moment(row.get("exit_time", "")) - utc_moment(row.get("entry_time", ""))).total_seconds() / 3600
        return hours if hours >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _closed_trades(path: Path, page: int, page_size: int) -> tuple[dict, dict]:
    table = {"available": _safe_file(path), "source": "closed_trades.csv", "page": page,
             "page_size": page_size, "total": 0, "pages": 0, "columns": [], "rows": [],
             "invalid_rows": 0, "truncated_cells": 0}
    summaries = {name: [] for name in ("net_pnl", "commission", "slippage", "mae", "mfe", "holding_hours")}
    groups = {key: defaultdict(lambda: {"count": 0, "net_pnl": 0.0})
              for key in ("exit_reason", "strategy", "symbol")}
    semantics = set()
    if table["available"]:
        reader = _csv_reader(path)
        raw_columns = reader.fieldnames or []
        if raw_columns and "net_pnl" not in raw_columns:
            raise ValueError("closed trade CSV requires net_pnl")
        table["columns"] = [key for key in raw_columns if key in _TEXT_COLUMNS | _NUMBER_COLUMNS]
        if {"entry_time", "exit_time"}.issubset(raw_columns):
            table["columns"].append("holding_hours")
        start = (page - 1) * page_size
        for row in _csv_rows(reader, _MAX_CLOSED_ROWS):
            pnl = _numeric(row.get("net_pnl"))
            if None in row or any(value is None for value in row.values()) or pnl is None:
                table["invalid_rows"] += 1
                continue
            holding = _holding_hours(row)
            for name in summaries:
                value = holding if name == "holding_hours" else _numeric(row.get(name))
                if value is not None:
                    summaries[name].append(value)
            for field, grouped in groups.items():
                label = _text(row.get(field) or (row.get("strategy_id") if field == "strategy" else None)) or "unknown"
                if label not in grouped and len(grouped) >= _MAX_GROUPS:
                    label = "other"
                grouped[label]["count"] += 1
                grouped[label]["net_pnl"] += pnl
            semantic = _text(row.get("cost_semantics"))
            if semantic:
                semantics.add(semantic)
            if start <= table["total"] < start + page_size:
                projected = {}
                for key in table["columns"]:
                    value = row.get(key)
                    if key == "holding_hours":
                        projected[key] = holding
                    elif key in _NUMBER_COLUMNS:
                        projected[key] = _numeric(value)
                    else:
                        projected[key] = _text(value)
                        table["truncated_cells"] += int(isinstance(value, str) and len(value) > _MAX_TEXT)
                table["rows"].append(projected)
            table["total"] += 1
        table["pages"] = math.ceil(table["total"] / page_size)
    for grouped in groups.values():
        for entry in grouped.values():
            entry["net_pnl"] = finite(entry["net_pnl"])
    return table, {"values": summaries, "groups": groups, "cost_semantics":
                   next(iter(semantics)) if len(semantics) == 1 else "mixed" if semantics else "not_recorded"}


def _trade_stats(metrics: dict, table: dict, summary: dict) -> tuple[dict, dict]:
    extended = _mapping(metrics.get("ExtendedAnalytics"))
    quality = _mapping(extended.get("trade_quality"))
    risk = _mapping(extended.get("r_multiple"))
    values = summary["values"]
    pnls = values["net_pnl"]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    gross_loss = _sum([-value for value in losses])
    gross_profit = _sum(wins)
    available = table["available"]
    sample = len(pnls)
    fallback = {
        "closed_trades": sample if available else None,
        "win_rate": len(wins) / sample if sample else None,
        "profit_factor": (finite(gross_profit / gross_loss)
                          if gross_loss and gross_profit is not None else None),
        "expectancy": _mean(pnls), "avg_win": _mean(wins), "avg_loss": _mean(losses),
        "net_pnl": _sum(pnls) if available else None,
        "commission": _sum(values["commission"]) if available and len(values["commission"]) == sample else None,
        "slippage": _sum(values["slippage"]) if available and len(values["slippage"]) == sample else None,
        "mean_holding_hours": _mean(values["holding_hours"]),
        "mean_mae": _mean(values["mae"]), "mean_mfe": _mean(values["mfe"]),
        "max_drawdown_days": None, "underwater_ratio": None,
    }
    fields = {
        "closed_trades": (quality.get("sample_size", metrics.get("TotalTrades")), quality.get("status"), "trade_quality.sample_size"),
        "win_rate": (quality.get("win_rate", metrics.get("WinRate")), quality.get("status"), "trade_quality.win_rate"),
        "profit_factor": (quality.get("profit_factor", metrics.get("ProfitFactor")), quality.get("profit_factor_status", metrics.get("ProfitFactorStatus")), "trade_quality.profit_factor"),
        "expectancy": (quality.get("expectancy", metrics.get("Expectancy")), quality.get("status"), "trade_quality.expectancy"),
        "avg_win": (quality.get("avg_win", metrics.get("AvgWin")), quality.get("status"), "trade_quality.avg_win"),
        "avg_loss": (quality.get("avg_loss", metrics.get("AvgLoss")), quality.get("status"), "trade_quality.avg_loss"),
        "net_pnl": (metrics.get("NetPnL"), None, "NetPnL"),
        "commission": (metrics.get("TotalCommission"), None, "TotalCommission"),
        "slippage": (metrics.get("TotalSlippage"), None, "TotalSlippage"),
        "mean_holding_hours": (_mapping(quality.get("holding_duration_hours")).get("mean"), _mapping(quality.get("holding_duration_hours")).get("status"), "trade_quality.holding_duration_hours.mean"),
        "mean_mae": (_mapping(risk.get("mae")).get("mean"), _mapping(risk.get("mae")).get("status"), "r_multiple.mae.mean"),
        "mean_mfe": (_mapping(risk.get("mfe")).get("mean"), _mapping(risk.get("mfe")).get("status"), "r_multiple.mfe.mean"),
        "max_drawdown_days": (metrics.get("MaxDrawdownDurationDays"), metrics.get("DrawdownStatus"), "MaxDrawdownDurationDays"),
        "underwater_ratio": (metrics.get("UnderwaterRatio"), metrics.get("DrawdownStatus"), "UnderwaterRatio"),
    }
    stats, statuses = {}, {}
    invalid_input = _mapping(metrics.get("TradeInputIntegrity")).get("status") == "invalid_input"
    for key, (recorded, status, field) in fields.items():
        status = status if isinstance(status, str) else None
        quality_keys = {"closed_trades": ("sample_size", "TotalTrades"), "win_rate": ("win_rate", "WinRate"),
                        "profit_factor": ("profit_factor", "ProfitFactor"), "expectancy": ("expectancy", "Expectancy"),
                        "avg_win": ("avg_win", "AvgWin"), "avg_loss": ("avg_loss", "AvgLoss")}
        if key in quality_keys and quality_keys[key][0] not in quality:
            field = quality_keys[key][1]
        elif field.startswith(("trade_quality.", "r_multiple.")):
            field = "ExtendedAnalytics." + field
        samples = _count(quality.get("sample_size", metrics.get("TotalTrades")))
        if key in {"mean_mae", "mean_mfe"}:
            samples = _count(_mapping(risk.get(key.removeprefix("mean_"))).get("sample_size"))
        elif key == "mean_holding_hours":
            samples = _count(_mapping(quality.get("holding_duration_hours")).get("sample_size"))
        elif key in {"max_drawdown_days", "underwater_ratio"}:
            samples = None
        number = _count(recorded) if key == "closed_trades" else _numeric(recorded)
        if status in {"invalid_input", "excluded", "not_modeled", "undefined"} or (invalid_input and key in {"win_rate", "profit_factor", "expectancy"}):
            stats[key] = None
            statuses[key] = {"status": "invalid_input" if invalid_input else status,
                             "source": f"metrics.json:{field}", "sample_size": samples,
                             "reason": "Report explicitly marks this statistic unavailable."}
        elif number is not None:
            stats[key] = number
            statuses[key] = {"status": _status(status, "recorded"), "source": f"metrics.json:{field}",
                             "sample_size": samples, "reason": None}
        else:
            value = fallback[key]
            stats[key] = value
            status = "partial" if table["invalid_rows"] else "descriptive"
            if key in {"win_rate", "profit_factor", "expectancy"} and sample < 30:
                status = "partial" if table["invalid_rows"] else "insufficient"
            statuses[key] = {"status": status if value is not None else "unknown",
                             "source": "closed_trades.csv" if value is not None else None,
                             "sample_size": (len(values["holding_hours" if key == "mean_holding_hours" else key.removeprefix("mean_")])
                                             if key.startswith("mean_") and available else sample if available else None),
                             "reason": ("Descriptive closed-trade sample; no inference of strategy edge."
                                        if value is not None else "Required observation was not recorded or is undefined.")}
    for key, value in stats.items():
        if value is not None and ((key in {"win_rate", "underwater_ratio"} and not 0 <= value <= 1)
                                  or (key in {"closed_trades", "profit_factor", "mean_holding_hours", "max_drawdown_days"} and value < 0)):
            stats[key] = None
            statuses[key].update(status="invalid_input", reason="Recorded value is outside its valid range.")
    return stats, statuses


def _concentration(metrics: dict, summary: dict) -> dict:
    recorded = _mapping(_mapping(metrics.get("Diagnostics")).get("pnl_concentration"))
    if recorded:
        top = []
        for key, raw in list(_mapping(recorded.get("top_n")).items())[:12]:
            if not str(key).isdigit() or not isinstance(raw, dict):
                continue
            top.append({"n": int(key), **{name: _numeric(raw.get(name)) for name in
                        ("contribution", "share_of_total", "total_excluding", "sample_size")}})
        return {"status": _status(recorded.get("status")), "source": "metrics.json:Diagnostics.pnl_concentration",
                "sample_size": _count(recorded.get("sample_size")), "profit_hhi": _numeric(recorded.get("profit_hhi")),
                "total_net_pnl": _numeric(recorded.get("total_net_pnl")), "top_n": top}
    pnls = summary["values"]["net_pnl"]
    if not pnls:
        return {"status": "unknown", "source": None, "sample_size": 0, "profit_hhi": None, "top_n": []}
    ordered = sorted(pnls, reverse=True)
    total = _sum(pnls)
    wins = [value for value in pnls if value > 0]
    gross = _sum(wins)
    top = []
    for count in (1, 3, 5, 10):
        if count <= len(pnls):
            contribution = _sum(ordered[:count])
            top.append({"n": count, "sample_size": count, "contribution": contribution,
                        "share_of_total": finite(contribution / total) if total and total > 0 and contribution is not None else None,
                        "total_excluding": finite(total - contribution) if total is not None and contribution is not None else None})
    return {"status": "descriptive", "source": "closed_trades.csv", "sample_size": len(pnls),
            "total_net_pnl": total, "profit_hhi": finite(sum((value / gross) ** 2 for value in wins)) if gross else None,
            "top_n": top}


def _recorded_risk_stats(metrics: dict, stats: dict, statuses: dict) -> None:
    """Project risk statistics using their saved clocks, statuses and samples."""
    extended = _mapping(metrics.get("ExtendedAnalytics"))
    risk = _mapping(extended.get("portfolio_risk"))
    exposure = _mapping(extended.get("exposure"))
    elapsed = _mapping(exposure.get("elapsed_time_weighted"))
    turnover = _mapping(extended.get("turnover"))

    def assign(key, value, status, samples, source, *, strict=False, ratio=False, positive=False):
        number = _numeric(value)
        state = _status(status, "recorded" if number is not None else "unknown")
        unavailable = {"invalid_input", "undefined", "excluded", "not_modeled", "not_recorded"}
        if strict:
            unavailable |= {"insufficient", "insufficient_data"}
        if state in unavailable:
            number = None
        elif number is None and state in {"ok", "recorded", "partial"}:
            state = "undefined" if source else "unknown"
        if number is not None and ((ratio and not 0 <= number <= 1) or (positive and number < 0)):
            number, state = None, "invalid_input"
        stats[key] = number
        statuses[key] = {"status": state, "sample_size": _count(samples), "source": source,
                         "reason": None if number is not None else "Risk statistic is unrecorded, undefined or explicitly lacks sufficient observations."}

    assign("sharpe_ratio", metrics.get("SharpeRatio"), metrics.get("SharpeStatus"), metrics.get("SharpeSamples"),
           "metrics.json:SharpeRatio" if "SharpeRatio" in metrics else None, strict=True)
    for key, aliases, status_key, sample_key in (
        ("sortino_ratio", ("SortinoRatio", "sortino_ratio"), "SortinoStatus", "SortinoSamples"),
        ("calmar_ratio", ("CalmarRatio", "calmar_ratio"), "CalmarStatus", "CalmarSamples"),
        ("annualized_volatility", ("AnnualizedVolatility", "AnnualVolatility", "annualized_volatility"),
         "AnnualizedVolatilityStatus", "VolatilitySamples"),
    ):
        selected = next((name for name in aliases if name in metrics), None)
        if selected is not None:
            assign(key, metrics[selected], metrics.get(status_key), metrics.get(sample_key),
                   f"metrics.json:{selected}", strict=True, positive=key == "annualized_volatility")
        else:
            assign(key, risk.get(key), risk.get("status"), risk.get("sample_size"),
                   f"metrics.json:ExtendedAnalytics.portfolio_risk.{key}" if key in risk else None,
                   strict=True, positive=key == "annualized_volatility")
    for key in ("time_in_market_ratio", "mean_gross_leverage", "max_gross_leverage"):
        field = elapsed if key in elapsed else exposure
        path = "elapsed_time_weighted." if field is elapsed else ""
        assign(key, field.get(key), field.get("status"), exposure.get("sample_size"),
               f"metrics.json:ExtendedAnalytics.exposure.{path}{key}" if key in field else None,
               ratio=key == "time_in_market_ratio", positive=True)
    assign("turnover_ratio", turnover.get("ratio"), turnover.get("status"), None,
           "metrics.json:ExtendedAnalytics.turnover.ratio" if "ratio" in turnover else None, positive=True)
    commission, gross = _numeric(metrics.get("TotalCommission")), _numeric(metrics.get("GrossPnL"))
    ratio = finite(commission / gross) if commission is not None and commission >= 0 and gross is not None and gross > 0 else None
    stats["fee_return_ratio"] = ratio
    statuses["fee_return_ratio"] = {
        "status": "descriptive" if ratio is not None else "unknown", "sample_size": _count(metrics.get("TotalTrades")),
        "source": "metrics.json:TotalCommission / GrossPnL" if commission is not None and gross is not None else None,
        "reason": "Recorded commission / positive recorded aggregate gross P&L; excludes slippage to avoid double-counting fill-price costs."
        if ratio is not None else "Requires recorded commission and strictly positive aggregate gross P&L.",
    }


def _activity_evidence(metrics: dict) -> tuple[dict, list[dict]]:
    """Return recorded gate facts separately from non-causal inactivity findings."""
    raw = _mapping(_mapping(metrics.get("Diagnostics")).get("strategy_activity_consistency"))
    activity = {
        "status": _status(raw.get("status")),
        "longest_no_closed_trade_days": _numeric(raw.get("longest_no_trade_days")),
        "start": _text(raw.get("longest_no_trade_start")), "end": _text(raw.get("longest_no_trade_end")),
        "suppressed_raw_setups": _count(raw.get("suppressed_raw_setups")),
        "silent_inactivity_detected": raw.get("silent_inactivity_detected") if isinstance(raw.get("silent_inactivity_detected"), bool) else None,
        "findings": [_text(item, 1024) for item in raw.get("findings", [])[:20] if isinstance(item, str)]
        if isinstance(raw.get("findings"), list) else [],
        "source": "metrics.json:Diagnostics.strategy_activity_consistency" if raw else None,
        "strategies": [],
        "note": "The reported quiet gap uses closed-trade exit timestamps; it does not measure absence of orders or open positions. Findings are report assertions, not independently established causes.",
    }
    blockers = []
    health = _mapping(metrics.get("StrategyHealth"))
    suppression_projected = False
    for name, raw_entry in list(health.items())[:32]:
        entry = _mapping(raw_entry)
        if not entry:
            continue
        strategy = _text(name) or "unknown"
        source = f"metrics.json:StrategyHealth.{strategy}"
        allowed = entry.get("allows_new_entries") if isinstance(entry.get("allows_new_entries"), bool) else None
        suppressed = _count(entry.get("suppressed_raw_setups"))
        activity["strategies"].append({
            "strategy": strategy, "status": _text(entry.get("status")), "allows_new_entries": allowed,
            "raw_setup_count": _count(entry.get("raw_setup_count")), "suppressed_raw_setups": suppressed,
            "last_raw_setup_at": _text(entry.get("last_raw_setup_at")),
            "last_suppressed_setup_at": _text(entry.get("last_suppressed_setup_at")), "source": source,
        })
        if allowed is False:
            blockers.append({"kind": "health_entry_gate", "stage": "risk", "strategy": strategy,
                "count": None, "status": "recorded", "source": source + ".allows_new_entries",
                "reason": _text(entry.get("manual_lock_reason") or entry.get("trigger_reason"), 1024)
                or "Recorded strategy health snapshot disallows new entries.",
                "scope": "report_health_snapshot; this does not prove a block throughout the entire backtest"})
        if suppressed is not None and suppressed > 0:
            suppression_projected = True
            blockers.append({"kind": "suppressed_raw_setups", "stage": "risk", "strategy": strategy,
                "count": suppressed, "status": "recorded", "source": source + ".suppressed_raw_setups",
                "reason": "The report records raw setups suppressed by strategy-health controls; raw setups are not filled entry chains.",
                "scope": "recorded strategy counter; no inferred conversion into missing fills"})
    suppressed = activity["suppressed_raw_setups"]
    if not suppression_projected and suppressed is not None and suppressed > 0:
        blockers.append({"kind": "suppressed_raw_setups", "stage": "risk", "strategy": None,
            "count": suppressed, "status": "recorded", "source": "metrics.json:Diagnostics.strategy_activity_consistency.suppressed_raw_setups",
            "reason": "The report records suppressed raw setups; their relationship to entry-chain counts is not assumed.",
            "scope": "aggregate report counter"})
    lifecycle = _mapping(metrics.get("BacktestLifecycle"))
    termination = _text(lifecycle.get("termination_reason"), 1024)
    if termination:
        blockers.append({"kind": "recorded_termination", "stage": "execution", "strategy": None,
            "count": _count(lifecycle.get("suppressed_setups_after_termination")), "status": "recorded",
            "source": "metrics.json:BacktestLifecycle.termination_reason", "reason": termination,
            "timestamp": _text(lifecycle.get("termination_timestamp")),
            "scope": "recorded lifecycle termination; does not explain activity before this timestamp"})
    return activity, blockers


def _drawdowns(metrics: dict) -> list[dict]:
    events = _mapping(metrics.get("ExtendedAnalytics")).get("drawdown_events")
    if not isinstance(events, list):
        return []
    result = []
    for row in events[:100]:
        if not isinstance(row, dict):
            continue
        entry = {key: _text(row.get(key)) for key in ("peak", "trough", "recovery")}
        entry.update({key: _numeric(row.get(key)) for key in
                      ("depth_pct", "depth_amount", "duration_days", "duration_periods", "recovery_days")})
        entry["is_open"] = row.get("is_open") if isinstance(row.get("is_open"), bool) else None
        result.append(entry)
    return result


def _funnel(metrics: dict, table: dict) -> tuple[dict, dict]:
    raw = _mapping(_mapping(metrics.get("ExtendedAnalytics")).get("signal_funnel"))
    recorded = _mapping(raw.get("stages"))
    labels = (("data", "数据"), ("warmup", "预热"), ("signal", "信号"),
              ("routing", "路由"), ("risk", "风险"), ("execution", "执行"))
    aliases = {"data": ("data_ready", "data_loaded"), "warmup": ("warmup_ready", "warmup_completed"),
               "signal": ("signal",), "routing": ("routing_approved", "routed"),
               "risk": ("risk_approved",), "execution": ("filled",)}
    stages = []
    for stage, label in labels:
        count, source = None, None
        for name in aliases[stage]:
            count = _count(_mapping(recorded.get(name)).get("count"))
            if count is not None:
                source = f"metrics.json:ExtendedAnalytics.signal_funnel.stages.{name}.count"
                break
        if stage == "signal" and _count(raw.get("raw_entry_signal_chains")) is not None:
            count = _count(raw.get("raw_entry_signal_chains"))
            source = "metrics.json:ExtendedAnalytics.signal_funnel.raw_entry_signal_chains"
        stages.append({"id": stage, "label": label, "status": "recorded" if count is not None else "unknown",
                       "count": count, "unit": "recorded_stage_observations" if stage in {"data", "warmup"} else "entry_chains",
                       "source": source, "reason": "Explicit recorded stage count; zero is an observation, not an inferred cause."
                       if count is not None else "This report does not record this pipeline stage."})
    details = []
    for name in ("risk_evaluated", "risk_approved", "order_created", "order_accepted", "filled"):
        entry = _mapping(recorded.get(name))
        if _count(entry.get("count")) is not None:
            details.append({"id": name, "count": _count(entry.get("count")),
                            "pct_of_total": _numeric(entry.get("pct_of_total")),
                            "pct_of_previous_stage": _numeric(entry.get("pct_of_previous_stage"))})
    by_id = {entry["id"]: entry for entry in stages}
    filled = by_id["execution"]["count"]
    no_trade = {"status": "unknown", "stage": None,
                "reason": "Entry-fill evidence is absent. Zero closed trades does not imply zero entries or identify a blocking stage."}
    if filled is not None and filled > 0:
        no_trade = {"status": "entries_observed", "stage": "execution",
                    "reason": f"The report records {filled} filled entry chains; closed trades are a different population."}
    elif filled == 0:
        zero = next((entry for entry in stages if entry["count"] == 0), by_id["execution"])
        no_trade = {"status": "no_entry_fills_recorded", "stage": zero["id"],
                    "reason": f"No filled entry chains are recorded; the earliest explicitly zero stage is {zero['id']}. Unrecorded upstream stages remain unknown, so this does not establish a root cause."}
    if table["invalid_rows"]:
        no_trade["closed_trade_warning"] = "Some closed-trade rows are invalid; their absence is not evidence of no activity."
    return {"status": "recorded" if all(entry["count"] is not None for entry in stages)
            else "partial" if any(entry["count"] is not None for entry in stages) else "unknown",
            "stages": stages, "recorded_stages": details,
            "excluded_exit_chains": _count(raw.get("excluded_exit_chains")),
            "unclassified_chains": _count(raw.get("unclassified_chains")),
            "incomplete_entry_stages": _count(raw.get("incomplete_entry_stages"))}, no_trade


def _exit_reasons(metrics: dict, summary: dict) -> list[dict]:
    recorded = _mapping(_mapping(_mapping(metrics.get("Diagnostics")).get("exit_attribution")).get("by_reason"))
    pnls = _mapping(_mapping(_mapping(metrics.get("ExtendedAnalytics")).get("attribution")).get("by_exit_reason"))
    if recorded or pnls:
        names = list(dict.fromkeys([*recorded, *pnls]))[:_MAX_GROUPS]
        return [{"reason": _text(name) or "unknown", "count": _count(recorded.get(name)),
                 "net_pnl": _numeric(pnls.get(name)), "source": "metrics.json"} for name in names]
    return [{"reason": name, **values, "source": "closed_trades.csv"}
            for name, values in summary["groups"]["exit_reason"].items()]


def load_diagnostics(reports_dir: Path, run_id: str, page: int = 1, page_size: int = 25) -> dict[str, Any]:
    """Read a saved report's diagnostic facts; no missing stage becomes zero."""
    if (isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= _MAX_CLOSED_ROWS
            or isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100):
        raise ValueError("invalid closed trade pagination")
    run = _report_run(Path(reports_dir), run_id)
    dependencies = [run / name for name in ("metrics.json", "closed_trades.csv", "dashboard_job.json")]
    signature = _signature(dependencies)
    key = ("diagnostics", str(run.resolve()), page, page_size)
    cached = _CACHE.get(key, signature)
    if cached is not None:
        return cached
    metrics, metrics_available = _read_metrics(run / "metrics.json")
    table, summary = _closed_trades(run / "closed_trades.csv", page, page_size)
    stats, statuses = _trade_stats(metrics, table, summary)
    _recorded_risk_stats(metrics, stats, statuses)
    funnel, no_trade = _funnel(metrics, table)
    activity, blockers = _activity_evidence(metrics)
    no_trade["blockers"] = blockers
    sensitivity = _mapping(_mapping(metrics.get("ExtendedAnalytics")).get("cost_sensitivity"))
    warnings = []
    if not metrics_available:
        warnings.append("metrics.json is absent; only recorded closed-trade facts can be summarized.")
    if not table["available"]:
        warnings.append("closed_trades.csv is absent; execution fills are not substituted for closed round trips.")
    if table["invalid_rows"]:
        warnings.append(f"Excluded {table['invalid_rows']} invalid closed-trade rows; derived summaries are partial.")
    result = {
        "id": run_id, "available": metrics_available or table["available"],
        "sources": {"metrics": metrics_available, "closed_trades": table["available"]},
        "stats": stats, "metric_status": statuses, "closed_trades": table,
        "costs": {"commission": stats["commission"], "slippage": stats["slippage"],
                  "cost_semantics": _text(sensitivity.get("cost_semantics")) or summary["cost_semantics"],
                  "note": _text(sensitivity.get("unmodeled_note"), 1024) or
                  "Recorded commission and slippage only. Slippage can already be embedded in fill-price P&L; do not subtract it again. Other costs are unknown unless recorded."},
        "exit_reasons": _exit_reasons(metrics, summary), "concentration": _concentration(metrics, summary),
        "drawdowns": _drawdowns(metrics), "funnel": funnel, "no_trade": no_trade, "activity": activity,
        "attribution": {field: [{"name": name, **entry} for name, entry in grouped.items()]
                        for field, grouped in summary["groups"].items() if field != "exit_reason"},
        "warnings": warnings,
        "methodology": {
            "closed_trades": "Recorded closed round trips from closed_trades.csv. Each valid row requires finite net_pnl. Execution fills are a separate population.",
            "profit_factor": "Sum of positive closed-trade net_pnl / absolute sum of negative closed-trade net_pnl. Undefined without observed losses; insufficient sample status is preserved.",
            "win_rate": "Count of closed trades with net_pnl > 0 / valid closed trades; breakeven trades remain in the denominator.",
            "expectancy": "Arithmetic mean of recorded closed-trade net_pnl, in its original report currency.",
            "holding": "Elapsed UTC hours between recorded entry_time and exit_time; missing or negative durations are excluded.",
            "mae_mfe": "Recorded excursion fields summarized in their original units. No bar-path reconstruction or conversion into returns is performed.",
            "concentration": "Top-N net-P&L contribution / total net P&L only when total is positive. Profit HHI uses positive P&L shares squared.",
            "drawdown": "Recorded account-equity drawdown events. max_drawdown_days is the duration of the maximum-depth event, not a closed-trade holding period.",
            "funnel": "Only explicitly recorded entry-chain counts are projected. Data, warmup and routing are unknown when not recorded. An observed zero does not by itself establish a causal blocking reason.",
            "risk_statistics": "Sharpe, Sortino, Calmar and annualized volatility are projected only when actually saved; their original statuses, samples and clock conventions are preserved. No new annualization is inferred here.",
            "fee_return_ratio": "Recorded TotalCommission / recorded GrossPnL, only when gross P&L is positive. Slippage is not added because fill-price gross P&L may already include it; this ratio is not total-cost drag.",
            "exposure": "Recorded elapsed-time-weighted exposure is preferred when present; otherwise saved observation-weighted exposure is shown with its exact source path. Turnover retains the report's executed-notional / mean-equity policy.",
            "blockers": "Only explicit health-entry gates, positive suppressed-setup counters and recorded lifecycle termination reasons are listed. These facts retain snapshot/counter scope and are not asserted to cause the whole run's inactivity.",
        },
    }
    if signature == _signature(dependencies):
        _CACHE.put(key, signature, result)
    return result
