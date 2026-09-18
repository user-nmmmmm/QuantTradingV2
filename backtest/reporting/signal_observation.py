"""Export P0 facts and descriptive gate diagnostics without fitting a model."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import json
from pathlib import Path

from core.reproducibility import canonical_json, sha256_bytes
from core.signal_observation_types import fingerprint


def _default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(type(value).__name__)


def _write_csv(path, rows, required):
    fields = list(dict.fromkeys([*required, *(key for row in rows for key in row)]))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, default=_default, sort_keys=True)
                             if isinstance(value, (dict, list, tuple)) else value
                             for key, value in row.items()})


def gate_diagnostics(payload):
    candidates = {c["candidate_id"]: c for c in payload["candidates"]}
    decisions = {d["candidate_id"]: d for d in payload["decisions"]}
    buckets = defaultdict(list)
    for outcome in payload["outcomes"]:
        cid = outcome["candidate_id"]
        d, c = decisions[cid], candidates[cid]
        key = (c["strategy"], c["direction"], d["veto_stage"], outcome["horizon_bars"])
        buckets[key].append(outcome)
    result = []
    for (strategy, direction, stage, horizon), rows in sorted(buckets.items()):
        observed = [r for r in rows if r["status"] == "matured"]
        values = [r["net_return_bps"] for r in observed]
        result.append({"strategy": strategy, "direction": direction, "gate": stage,
            "horizon_bars": horizon, "candidates": len(rows), "matured": len(observed),
            "censored": len(rows)-len(observed), "positive": sum(v > 0 for v in values),
            "negative": sum(v < 0 for v in values), "breakeven": sum(v == 0 for v in values),
            "mean_net_bps": sum(values)/len(values) if values else None,
            "non_executable_diagnostics": sum(bool(r.get("execution_flags")) for r in observed),
            "interpretation": "descriptive_only_correlated_overlapping_candidates_no_causal_uplift"})
    return result


def signal_observation_digest(payload):
    """Separate research digest; never changes the official trading digest."""
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def ghost_diagnostics(payload):
    buckets = defaultdict(list)
    for row in payload.get("ghost", {}).get("rows", []):
        buckets[row["mode"], row["group"], row["strategy"]].append(row)
    result = []
    for (mode, group, strategy), rows in sorted(buckets.items()):
        closed = [r for r in rows if r["status"] == "closed"]
        counts = Counter(r["status"] for r in rows)
        result.append({"mode": mode, "group": group, "strategy": strategy,
            "candidates": len(rows), "closed": len(closed),
            "busy_blocked": counts["busy_blocked"], "capital_blocked": counts["capital_blocked"],
            "unfilled": sum(n for status, n in counts.items() if status.startswith("unfilled_")),
            "censored_or_unresolved": sum(n for status, n in counts.items()
                if status.startswith(("censored_", "unresolved_"))),
            "positive": sum(r["net_pnl"] > 0 for r in closed),
            "negative": sum(r["net_pnl"] < 0 for r in closed),
            "mean_closed_net_pnl": sum(r["net_pnl"] for r in closed)/len(closed) if closed else None,
            "interpretation": "fixed_horizon_replay_separate_accounts_no_causal_gate_uplift"})
    return result


def write_signal_observation_report(payload, output_dir):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    decisions = {d["candidate_id"]: d for d in payload["decisions"]}
    candidate_rows, context_rows = [], []
    for source in payload["candidates"]:
        row = dict(source)
        context_rows.append({"candidate_id": row["candidate_id"], **row.pop("context")})
        decision = decisions[row["candidate_id"]]
        row.update({k: decision[k] for k in ("accepted", "veto_stage", "veto_reason", "order_id")})
        candidate_rows.append(row)
    diagnostics = gate_diagnostics(payload)
    ghost_stats = ghost_diagnostics(payload)
    actual, ghosts = payload.get("actual", {}), payload.get("ghost", {})
    files = {
        "signal_candidates.csv": (candidate_rows, ["candidate_id", "timestamp", "symbol", "strategy", "accepted"]),
        "signal_contexts.csv": (context_rows, ["candidate_id", "available_at", "features"]),
        "signal_decisions.csv": (payload["decisions"], ["candidate_id", "accepted", "veto_stage", "veto_reason"]),
        "signal_outcomes.csv": (payload["outcomes"], ["candidate_id", "horizon_bars", "status", "net_return_bps"]),
        "signal_actuals.csv": (actual.get("summaries", []), ["candidate_id", "actual_status"]),
        "signal_actual_fills.csv": (actual.get("fills", []), ["candidate_id", "order_id", "fill_time"]),
        "signal_actual_closes.csv": (actual.get("closes", []), ["candidate_id", "exit_reason", "realized_net_pnl_ex_carry"]),
        "signal_actual_execution_audit.csv": (actual.get("execution_audit", []), ["candidate_id", "order_id"]),
        "signal_valuation_transfers.csv": (actual.get("valuation_transfers", []), ["order_id", "exit_reason"]),
        "signal_actual_financing.csv": (payload.get("actual_financing", []), ["timestamp", "symbol", "amount"]),
        "signal_ghosts.csv": (ghosts.get("rows", []), ["candidate_id", "mode", "group", "status", "net_pnl"]),
        "signal_ghost_fills.csv": (ghosts.get("fills", []), ["candidate_id", "mode", "order_id", "fill_time"]),
        "signal_ghost_financing.csv": (ghosts.get("financing", []), ["mode", "group", "amount"]),
        "signal_ghost_execution_audit.csv": (ghosts.get("execution_audit", []), ["mode", "group", "order_id"]),
        "signal_ghost_accounts.csv": (ghosts.get("accounts", []), ["track", "initial_capital", "ending_marked_equity"]),
        "gate_effectiveness.csv": (diagnostics, ["strategy", "direction", "gate", "horizon_bars", "mean_net_bps"]),
        "gate_ghost_effectiveness.csv": (ghost_stats, ["mode", "group", "strategy", "closed", "mean_closed_net_pnl"]),
    }
    for name, (rows, columns) in files.items():
        _write_csv(root/name, rows, columns)
    frozen = {k: payload[k] for k in ("schema", "policy", "snapshot_version", "costs", "strategy_versions")}
    summary = {**frozen, "status": payload["status"], "coverage": payload["coverage"],
        "candidate_count": len(candidate_rows), "decision_count": len(decisions),
        "candidate_ids_unique": len(candidates := {c["candidate_id"] for c in candidate_rows}) == len(candidate_rows),
        "decision_partition_ok": candidates == set(decisions),
        "gates": dict(Counter(d["veto_stage"] for d in decisions.values())),
        "outcome_statuses": dict(Counter(o["status"] for o in payload["outcomes"])),
        "ghost_statuses": dict(Counter(r["status"] for r in ghosts.get("rows", []))),
        "unmatched_actual_fills": actual.get("unmatched", []), "errors": payload.get("errors", []),
        "ghost_errors": ghosts.get("errors", []), "ghost_protocol": ghosts.get("protocol", {}),
        "signal_facts_sha256": fingerprint(payload["candidates"]),
        "research_payload_sha256": signal_observation_digest(payload),
        "artifacts": sorted([*files, "signal_observation_summary.json",
                             "signal_candidate_events.jsonl", "gate_effectiveness.md"]),
        "labeling": "candidate timestamp is bar open label; features available_at is bar close; outcomes update only at/after maturity",
        "actual_pnl_convention": actual.get("pnl_convention"),
        "research_interpretation": "P0 descriptive measurement; no EV fit, no parameter selection, no admission decision"}
    (root/"signal_observation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=_default, allow_nan=False), encoding="utf-8")
    with (root/"signal_candidate_events.jsonl").open("w", encoding="utf-8") as handle:
        for candidate in payload["candidates"]:
            handle.write(json.dumps(candidate, ensure_ascii=False, sort_keys=True, allow_nan=False)+"\n")
    lines = ["# P0 候选信号与门控观察报告", "",
        f"候选 {len(candidate_rows)} 个；数据完整性：{summary['status']}。结果仅用于描述与诊断。", "",
        "每个候选保留信号时点可见的上下文和首次实际阻断。未经过的后续门控为未知；接受订单不等于成交。", "",
        "固定期限结果从下一根连续 K 线开盘至第 H 根收盘，含手续费、价差、基础滑点、波动滑点、冲击和适用的融资费用。缺失 K 线、资金费或数据尾部不足明确标记，缺失结果不填零。", "",
        "Ghost 使用独立 Broker 复用成交成本、成交量上限、部分成交、订单过期和借贷/资金费；固定持有期到期后在下一根真实 K 线开盘退出。isolated_track 为独立账户，不能相加成一个组合收益；capital_constrained 在每个决策组内共享有限本金并限制总名义敞口。", "",
        "实际成交通过订单和 FIFO 部分平仓关联候选。实际盈亏字段 realized_net_pnl_ex_carry 已扣双边手续费并包含成交滑点，融资账本另列；不把账户级利息伪装为逐笔已知成本。无成本期末估值与真实成交分开。", "",
        "下表为相关、可能重叠的候选均值。被拒候选收益及独立回放均不能单独证明门控的因果效果，也不构成新的样本外准入证据。", "",
        "| 策略 | 方向 | 首次门控 | 期限 | 候选 | 成熟 | 不完整 | 正收益 | 负收益 | 平均净收益 bp |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in diagnostics:
        value = "未知" if r["mean_net_bps"] is None else f"{r['mean_net_bps']:.2f}"
        lines.append(f"| {r['strategy']} | {r['direction']} | {r['gate']} | {r['horizon_bars']} | {r['candidates']} | {r['matured']} | {r['censored']} | {r['positive']} | {r['negative']} | {value} |")
    lines += ["", "## 有限本金与占位后的回放", "",
        "下表只统计已真实模拟平仓的影子交易；仍持仓、未成交、占位阻断和资金不足单独计数。净盈亏单位为账户计价币，不是百分比。不同组和独立账户不得相加解释为同一组合。", "",
        "| 模式 | 决策组 | 策略 | 候选 | 已平仓 | 占位阻断 | 本金阻断 | 未成交 | 不完整 | 已平仓平均净盈亏 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for r in ghost_stats:
        value = "未知" if r["mean_closed_net_pnl"] is None else f"{r['mean_closed_net_pnl']:.2f}"
        lines.append(f"| {r['mode']} | {r['group']} | {r['strategy']} | {r['candidates']} | {r['closed']} | {r['busy_blocked']} | {r['capital_blocked']} | {r['unfilled']} | {r['censored_or_unresolved']} | {value} |")
    lines += ["", "完整字段、输入版本、覆盖计数、异常及回放约定见 signal_observation_summary.json；逐笔证据见同目录 CSV 与 signal_candidate_events.jsonl。"]
    (root/"gate_effectiveness.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return summary
