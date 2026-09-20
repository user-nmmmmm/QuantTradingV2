"""P2 predictive diagnostics and P3 finite-capital policy replay reports."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from itertools import combinations
import json
import math
from numbers import Real
from pathlib import Path

from backtest.reporting.signal_meta_layer import _diagnostic_rows, signal_meta_diagnostics
from backtest.reporting.signal_observation import _write_csv
from core.reproducibility import canonical_json, sha256_bytes


P2_LIMITATIONS = [
    "本实现是论文思路的工程扩展，不是论文完整复现；状态数、邻域、窗口和权重边界使用预先声明的政策。",
    "Wasserstein 距离的 softmax 输出是软归属权重，不是经校准的真实状态概率。未知状态的熵保留未知，不以零熵表示高确定性。",
    "三轴可以共享信息、相关或冗余；熵与训练分离度不证明各轴独立，也不证明预测能力。",
    "归因只使用信号出现时已冻结的预测和当时之后、训练截止前成熟的标签；测试期收益不得倒流。",
    "正 Spearman 相关平方、截断、EMA 和有界投影属于保守工程设定；反向相关不获得正归因奖励。",
    "近似下界和时间块有效样本量不保证置信覆盖率或样本独立；abstain 仍是未知，不是 veto。",
    "误差比较在每行明确的共同有限样本上计算；不得跨不同分母选择最优模型，未成熟、缺失与不可执行标签不填零。",
    "标签已经包含执行成本，不重复扣减；候选可跨币种相关、期限重叠，候选收益不能相加为组合收益。",
    "已有历史不会因滚动验证成为新的样本外准入证据；此报告不证明因果提升、盈利或实盘准入。",
]
P3_LIMITATIONS = [
    "三个政策各自拥有相同初始资本的独立 Broker 账户；同一政策内所有候选共享资金池，并受未成交订单预留与总敞口预算约束。",
    "baseline 接收 P0 合格原始候选；gate 仅接收 allow；sizing 的 veto 为零、abstain 使用预声明最小规模、allow 使用有上限的规模。",
    "sizing 对 abstain 的最小仓位是政策假设，不是正收益证据；horizon、规模上限、资金和预估净优势满仓门槛均在 policy 中记录。",
    "入场根据冻结预测决定，下一实际市场柱撮合；从首次成交累计实际持有柱，期满撤销剩余入场量并在后续实际柱退出。",
    "手续费、滑点、借贷及资金费、部分成交、订单约束和资本争用由影子 Broker 计入；随机滑点使用配置的确定性费率，不共享正式账户随机状态。",
    "期末持仓不强制清仓。期末标记权益包括未平仓损益；已平候选净损益与账户总损益口径不同，不得互相替代或再次扣除成本。",
    "收益率分母为初始资本；最大回撤分母为此前标记权益峰值。缺失市场柱沿用最近标记，相关时点在 equity 中记录。",
    "账户停止、执行异常或证据不完整时收益与回撤保持未知，不排名；没有成交的 inactive 账户不能被解释为成功择优。",
    "政策影子账户采用固定期限，不等于生产策略原始动态退出、健康度和风险管理；政策间结果差异不构成因果门控收益证明。",
    "既有历史不是新的样本外证据；complete 只表示工程与数据流程完成，不表示盈利、统计显著或实盘准入。",
]


def research_digest(payload):
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def _number(value):
    if not isinstance(value, Real) or isinstance(value, bool):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except (ValueError, OverflowError):
        return None


def _mean(values):
    return math.fsum(values) / len(values) if values else None


def _format(value, *, percent=False):
    value = _number(value)
    return "未知" if value is None else f"{value*100:.2f}%" if percent else f"{value:.4f}"


def _json(path, payload):
    path.write_text(json.dumps(json.loads(canonical_json(payload)), ensure_ascii=False,
                               indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _write_tables(root, tables):
    for name, (rows, fields) in tables.items():
        # Also normalise nonfinite nested diagnostics to JSON null in CSV.
        _write_csv(root / name, json.loads(canonical_json(rows)), fields)


def _common(payload, artifacts):
    validation = payload.get("validation", {})
    failed = sorted(key for key, value in validation.items() if value is False)
    return {"schema": payload.get("schema"), "policy": deepcopy(payload.get("policy", {})),
            "status": "incomplete" if failed or payload.get("errors") else payload.get("status", "incomplete"),
            "research_payload_sha256": research_digest(payload), "input_identity": deepcopy(payload.get("input_identity", {})),
            "protocol": deepcopy(payload.get("protocol", {})), "validation": deepcopy(validation),
            "failed_validation": failed, "errors": deepcopy(payload.get("errors", [])),
            "artifacts": sorted(artifacts)}


def _prediction_key(row):
    return row.get("candidate_id"), row.get("horizon_bars")


def _comparison_diagnostics(payload, p1_payload):
    """Each comparison has its own explicitly reported common finite cohort."""
    references = defaultdict(list)
    p1_identity_ok = True
    if p1_payload is not None:
        current = payload.get("input_identity", {}).get("p0_snapshot_version")
        prior = p1_payload.get("input_identity", {}).get("p0_snapshot_version")
        p1_identity_ok = (isinstance(current, str) and bool(current) and current == prior
                          and p1_payload.get("status") == "complete")
        if p1_identity_ok:
            for row in p1_payload.get("predictions", []):
                references[_prediction_key(row)].append(row)
    rows = _diagnostic_rows(payload)
    duplicates = Counter(_prediction_key(row) for row in rows)
    samples = []
    for row in rows:
        key = _prediction_key(row)
        actual = _number(row.get("realized_net_bps"))
        if (duplicates[key] != 1 or row.get("label_status") != "matured"
                or row.get("execution_flags") or actual is None):
            continue
        values = {"prior": _number((row.get("prior") or {}).get("mean_bps")),
                  "uniform": _number(row.get("uniform_estimate_bps")),
                  "adaptive": _number(row.get("estimate_bps"))}
        if p1_payload is not None:
            matches = references.get(key, [])
            reference = matches[0] if len(matches) == 1 else None
            compatible = reference is not None and all(
                row.get(field) == reference.get(field) for field in
                ("strategy", "direction", "symbol", "available_at"))
            values["p1"] = _number(reference.get("estimate_bps")) if compatible else None
        samples.append((actual, values))
    names = ["prior", "uniform", "adaptive"] + (["p1"] if p1_payload is not None else [])
    comparisons = []
    for compared in [*combinations(names, 2), tuple(names)]:
        paired = [(actual, values) for actual, values in samples
                  if all(values[name] is not None for name in compared)]
        mse = {name: _mean([(values[name] - actual) ** 2 for actual, values in paired])
               for name in compared}
        comparisons.append({"comparison": "_vs_".join(compared), "models": list(compared),
            "paired_count": len(paired), "mse_bps2": mse,
            **{name + "_mse_bps2": value for name, value in mse.items()},
            "scope": "same_candidate_horizon_finite_frozen_predictions_mature_executable_labels"})
    return comparisons, p1_identity_ok


def _regime_diagnostics(payload):
    models = payload.get("regime_models", [])
    predictions = payload.get("predictions", [])
    names = sorted({axis for model in models for axis in model.get("axes", {})}
                   | {axis for row in predictions for axis in row.get("memberships", {})})
    diagnostics = {}
    for name in names:
        model_axes = [model["axes"][name] for model in models if name in model.get("axes", {})]
        entropies, normalized, unknown = [], [], 0
        for row in predictions:
            states = row.get("memberships", {}).get(name, {})
            probabilities = [_number(value) for value in states.values()]
            if (not probabilities or any(value is None or value < 0 for value in probabilities)
                    or "unknown" in states
                    or not math.isclose(math.fsum(probabilities), 1., rel_tol=0, abs_tol=1e-9)):
                unknown += 1
                continue
            entropy = -math.fsum(value * math.log(value) for value in probabilities if value > 0)
            entropies.append(entropy)
            if len(states) > 1:
                normalized.append(entropy / math.log(len(states)))
        diagnostics[name] = {
            "model_axis_count": len(model_axes),
            "available_model_count": sum(axis.get("status") == "available" for axis in model_axes),
            "unavailable_model_count": sum(axis.get("status") != "available" for axis in model_axes),
            "unavailable_reasons": dict(sorted(Counter(axis.get("reason", "unknown") for axis in model_axes
                                                       if axis.get("status") != "available").items())),
            "known_prediction_count": len(entropies), "unknown_prediction_count": unknown,
            "mean_entropy_nats": _mean(entropies), "mean_normalized_entropy": _mean(normalized),
            "mean_fit_separation_bps2": _mean([value for axis in model_axes
                                               if (value := _number(axis.get("separation_bps2"))) is not None]),
            "mean_fit_inertia_bps2": _mean([value for axis in model_axes
                                            if (value := _number(axis.get("inertia_bps2"))) is not None]),
            "interpretation": "entropy_and_training_separation_not_axis_independence_or_predictive_validation"}
    missing = Counter((row.get("fold_id"), row.get("strategy"), row.get("direction"),
                       row.get("horizon_bars"), row.get("reason", "unknown"))
                      for row in predictions if row.get("regime_model_id") is None)
    missing_rows = [{**dict(zip(("fold_id", "strategy", "direction", "horizon_bars", "prediction_reason"), key)),
                     "prediction_count": count,
                     "model_reason": "initial_training_window" if key[0] is None else "no_fitted_model_for_book"}
                    for key, count in sorted(missing.items(), key=lambda item: tuple(map(str, item[0])))]
    return diagnostics, missing_rows


def write_signal_adaptive_report(payload, output_dir, *, p1_payload=None):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    diagnostics = signal_meta_diagnostics(payload)
    comparisons, reference_ok = _comparison_diagnostics(payload, p1_payload)
    regimes, missing_models = _regime_diagnostics(payload)
    predictions = payload.get("predictions", [])
    attribution = payload.get("attribution", [])
    status_counts = Counter(row.get("status", "unknown") for row in predictions)
    tables = {
        "p2_predictions.csv": (predictions, ["candidate_id", "horizon_bars", "fold_id", "available_at",
            "strategy", "direction", "symbol", "status", "reason", "estimate_bps", "uniform_estimate_bps",
            "lower_bound_bps", "axis_weights", "memberships", "regime_model_id", "model_version"]),
        "p2_evaluations.csv": (payload.get("evaluations", []), ["candidate_id", "horizon_bars", "fold_id",
            "label_status", "realized_net_bps", "execution_flags"]),
        "p2_folds.csv": (payload.get("folds", []), ["fold_id", "train_start", "regime_fit_cutoff",
            "training_cutoff", "start", "end", "training_observations", "model_version"]),
        "p2_cells.csv": (payload.get("cell_snapshots", []), ["fold_id", "strategy", "direction",
            "horizon_bars", "axis", "state", "raw_count", "effective_samples", "effective_blocks"]),
        "p2_attribution.csv": (attribution, ["fold_id", "strategy", "direction", "horizon_bars",
            "cutoff", "status", "reason", "weights", "correlations", "sample_count", "effective_blocks",
            "source_model_versions", "information_id"]),
        "p2_calibration_predictions.csv": (payload.get("calibration_predictions", []), ["candidate_id",
            "horizon_bars", "fold_id", "available_at", "max_training_label_at", "label_available_at",
            "label_eligible", "net_return_bps", "axes", "model_version"]),
        "p2_diagnostics.csv": ([{"diagnostic_kind": "cohort", **row} for row in diagnostics]
            + [{"diagnostic_kind": "paired_mse", **row} for row in comparisons],
            ["diagnostic_kind", "fold_id", "strategy", "direction", "horizon_bars", "predicted_status",
             "prediction_count", "eligible_count", "comparison", "paired_count", "mse_bps2"]),
    }
    _write_tables(root, tables)
    model_artifact = {"models": payload.get("regime_models", []), "missing_model_predictions": missing_models,
        "policy": payload.get("policy", {}), "axis_diagnostics": regimes,
        "probability_interpretation": "uncalibrated_soft_memberships"}
    _json(root / "p2_regime_models.json", model_artifact)
    summary = _common(payload, [*tables, "p2_regime_models.json", "p2_summary.json", "p2_report.md"])
    forecast_count = sum(row["forecast_count"] for row in diagnostics)
    models = payload.get("regime_models", [])
    summary.update(model_version=payload.get("model_version"), prediction_count=len(predictions),
        evaluation_count=len(payload.get("evaluations", [])), fold_count=len(payload.get("folds", [])),
        cell_count=len(payload.get("cell_snapshots", [])), regime_model_count=len(models),
        calibration_prediction_count=len(payload.get("calibration_predictions", [])),
        available_regime_model_count=sum(bool(model.get("axes")) and
            all(axis.get("status") == "available" for axis in model["axes"].values()) for model in models),
        missing_model_prediction_count=sum(row["prediction_count"] for row in missing_models),
        status_counts={status: status_counts[status] for status in sorted({"allow", "veto", "abstain", *status_counts})},
        forecast_count=forecast_count, evidence="diagnostic_only" if forecast_count else "insufficient",
        axis_diagnostics=regimes, attribution_count=len(attribution),
        attribution_fallback_count=sum(row.get("status") == "uniform_fallback" for row in attribution),
        attribution_reason_counts=dict(sorted(Counter(row.get("reason", "unknown") for row in attribution).items())),
        forecast_comparisons=comparisons, p1_reference_compatible=reference_ok if p1_payload is not None else None,
        p1_reference_sha256=research_digest(p1_payload) if p1_payload is not None else None,
        limitations=P2_LIMITATIONS)
    for field in ("matured_count", "eligible_count", "censored_count", "unresolved_count", "non_executable_count"):
        summary[field] = sum(row[field] for row in diagnostics)
    _json(root / "p2_summary.json", summary)
    lines = ["# P2 状态学习与动态归因研究报告", "",
        f"工程状态：{summary['status']}；证据：{summary['evidence']}。complete 不表示盈利或通过准入。", "",
        f"冻结预测 {len(predictions)} 条；allow {status_counts['allow']}、veto {status_counts['veto']}、"
        f"abstain {status_counts['abstain']}。时间折 {summary['fold_count']}；完整可用状态模型 "
        f"{summary['available_regime_model_count']}/{len(models)}；无对应模型预测 {summary['missing_model_prediction_count']}。", "",
        f"归因账本 {len(attribution)} 个，等权回退 {summary['attribution_fallback_count']} 个。"
        f"成熟可执行标签 {summary['eligible_count']} 条，具有有限冻结预测 {forecast_count} 条。", "",
        "## 时间与状态诊断", "",
        "较早子窗口拟合分布原型，较晚子窗口按时间依次积累 EV 账本和冻结轴预测；"
        "归因只用严格截止前成熟的冻结预测，外层测试折冻结模型、账本及轴权重。完整质心、温度、"
        "最小样本配置与模型不可用原因见 p2_regime_models.json。", "",
        "| 轴 | 可用模型 | 已知预测 | 未知预测 | 平均熵（nat） | 归一化熵 | 平均训练分离度（bp²） |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for axis, row in regimes.items():
        lines.append(f"| {axis} | {row['available_model_count']} | {row['known_prediction_count']} | "
            f"{row['unknown_prediction_count']} | {_format(row['mean_entropy_nats'])} | "
            f"{_format(row['mean_normalized_entropy'])} | {_format(row['mean_fit_separation_bps2'])} |")
    lines += ["", "未知状态不纳入平均熵。训练分离度与熵只描述拟合和归属，不表示三轴独立或有效。", "",
        "## 共同样本预测误差", "",
        "prior 为同账本总体均值；uniform 为 P2 相同状态模型的等权分数；adaptive 为动态加权分数；"
        "p1 为可选 P1 固定轴参考。每行所有模型共用同一批有限预测与成熟、可执行净标签；"
        "不同表行的样本量可能不同，不能跨行按误差高低排名。标签侧后改的预测不用于比较。", "",
        "| 比较 | 共同样本数 | 同分母 MSE（bp²） |", "| --- | ---: | --- |"]
    for row in comparisons:
        lines.append(f"| {row['comparison']} | {row['paired_count']} | " +
                     "；".join(f"{name}: {_format(value)}" for name, value in row["mse_bps2"].items()) + " |")
    if p1_payload is not None and not reference_ok:
        lines += ["", "P1 参考的观察快照身份不匹配或状态不完整，其配对误差保留未知。"]
    lines += ["", "## 解释边界", "", *(f"- {item}" for item in P2_LIMITATIONS)]
    if summary["status"] != "complete":
        lines += ["", "存在数据或验证异常，详见 p2_summary.json 的 errors 与 failed_validation。"]
    (root / "p2_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def write_signal_meta_replay_report(payload, output_dir):
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    rows = payload.get("rows", [])
    validation = payload.get("validation", {})
    complete = payload.get("status") == "complete" and not payload.get("errors") and all(validation.values())
    account_summaries = []
    for account in payload.get("accounts", []):
        arm = account.get("arm")
        own_rows = [row for row in rows if row.get("arm") == arm]
        closed = [value for row in own_rows if row.get("status") == "closed"
                  and (value := _number(row.get("net_pnl"))) is not None]
        valid = (complete and account.get("status") == "completed" and account.get("halt_time") is None
                 and _number(account.get("return_fraction")) is not None
                 and _number(account.get("max_drawdown_fraction")) is not None)
        active = account.get("activity") == "active" and (_number(account.get("fills")) or 0) > 0
        result = {**deepcopy(account), "closed_candidate_net_pnl": math.fsum(closed) if closed else None,
            "finite_closed_candidate_count": len(closed), "candidate_count": len(own_rows),
            "candidate_status_counts": dict(sorted(Counter(row.get("status", "unknown") for row in own_rows).items())),
            "censored_candidate_count": sum(str(row.get("status", "")).startswith("censored_") for row in own_rows),
            "metrics_valid": valid, "comparison_eligible": valid and active,
            "evidence": "marked_policy_replay" if valid and active else "inactive" if valid else "unresolved"}
        if not valid:
            result.update(net_pnl=None, return_fraction=None, max_drawdown_fraction=None)
        account_summaries.append(result)
    tables = {
        "p3_candidates.csv": (rows, ["arm", "candidate_id", "strategy", "symbol", "direction",
            "decision_available_at", "horizon_bars", "prediction_status", "multiplier", "research_notional",
            "status", "reason", "filled_quantity", "closed_quantity", "net_pnl"]),
        "p3_accounts.csv": (account_summaries, ["arm", "status", "activity", "initial_capital",
            "ending_marked_equity", "net_pnl", "return_fraction", "max_drawdown_fraction", "fills",
            "closed_candidate_net_pnl", "open_positions", "commission", "slippage_cost", "financing_cost",
            "metrics_valid", "comparison_eligible"]),
        "p3_equity.csv": (payload.get("equity", []), ["arm", "timestamp", "available_at", "marked_equity",
            "cash", "gross_used", "pending_reserved", "gross_budget", "status", "stale_mark_symbols"]),
        "p3_fills.csv": (payload.get("fills", []), ["arm", "candidate_id", "symbol", "fill_time", "side",
            "qty", "fill_price", "commission", "slip"]),
        "p3_financing.csv": (payload.get("financing", []), ["arm", "candidate_id", "symbol", "timestamp", "amount"]),
        "p3_execution_audit.csv": (payload.get("execution_audit", []), ["arm", "timestamp", "symbol", "order_id", "reason"]),
    }
    _write_tables(root, tables)
    summary = _common(payload, [*tables, "p3_summary.json", "p3_report.md"])
    summary.update(implementation_version=payload.get("implementation_version"), accounts=account_summaries,
        candidate_count=len({_prediction_key(row) for row in rows}), arm_candidate_count=len(rows),
        fill_count=len(payload.get("fills", [])), equity_count=len(payload.get("equity", [])),
        financing_count=len(payload.get("financing", [])), execution_audit_count=len(payload.get("execution_audit", [])),
        inactive_arms=[row["arm"] for row in account_summaries if row.get("activity") == "inactive"],
        unresolved_arms=[row["arm"] for row in account_summaries if not row["metrics_valid"]],
        comparison_eligible_arms=[row["arm"] for row in account_summaries if row["comparison_eligible"]],
        interpretation="finite_capital_policy_replay_no_causal_uplift_no_live_admission",
        limitations=P3_LIMITATIONS)
    if summary["unresolved_arms"]:
        summary["status"] = "incomplete"
    _json(root / "p3_summary.json", summary)
    lines = ["# P3 有限资本政策影子账户报告", "", f"工程状态：{summary['status']}。", "",
        "各政策使用独立但等额初始资本，同政策的候选共享资金池。收益与回撤来自逐市场事件标记权益，"
        "不是将逐笔标签相加，也不是事后筛选已发生的成交。", "",
        "| 政策 | 账户状态 | 活动 | 初始资本 | 期末标记权益 | 标记收益率 | 最大回撤 | 成交数 | 已平候选净损益 | 期末持仓数 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for account in account_summaries:
        lines.append(f"| {account['arm']} | {account.get('status')} | {account.get('activity')} | "
            f"{_format(account.get('initial_capital'))} | {_format(account.get('ending_marked_equity'))} | "
            f"{_format(account.get('return_fraction'), percent=True)} | "
            f"{_format(account.get('max_drawdown_fraction'), percent=True)} | {account.get('fills', 0)} | "
            f"{_format(account['closed_candidate_net_pnl'])} | {account.get('open_positions', '未知')} |")
    policy = payload.get("policy", {})
    lines += ["", f"政策期限：{policy.get('horizon_bars', '未知')} 个实际柱；固定参考名义金额："
        f"{_format(policy.get('reference_notional'))}；sizing 弃权规模：{_format(policy.get('min_size_multiplier'))} 倍；"
        f"最大规模：{_format(policy.get('max_size_multiplier'))} 倍。", "",
        "gate 的 abstain 不下单，sizing 的 abstain 使用预设最小规模；veto 在两者中均不下单。"
        "allow 的 sizing 使用净优势下界映射并受规模上限约束。", "",
        "已平候选列只汇总状态为 closed 且净损益已知的候选，没有已平候选时保留未知。"
        "未平持仓与待处理订单的期末截尾仍保留，不把未知结果记为零。", "",
        "inactive 表示没有成交：即使标记收益为零，也不能据此宣称门控成功或优于有交易的政策。"
        "停止后的标记权益仅供诊断，其收益和回撤不参与比较。本报告不自动给账户排名。", "",
        "## 成本与政策边界", "", *(f"- {item}" for item in P3_LIMITATIONS)]
    if summary["status"] != "complete":
        lines += ["", "执行或数据证据存在异常；账户损益指标保留未知，异常明细见 p3_summary.json。"]
    lines += ["", "候选、账户、权益、成交、融资与执行约束分别保存在对应 p3 CSV 文件，完整政策与输入身份见 p3_summary.json。"]
    (root / "p3_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
