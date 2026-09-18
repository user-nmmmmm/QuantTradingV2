"""P1 research exports; predictive diagnostics are not portfolio performance."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import math
from pathlib import Path

from backtest.reporting.signal_observation import _write_csv
from core.reproducibility import canonical_json, sha256_bytes


INTERPRETATION = "diagnostic_only_correlated_candidates_no_causal_gate_uplift_no_live_admission"
LIMITATIONS = [
    "结果是研究层预测诊断，不改变路由、订单、仓位、风控或策略准入。",
    "候选可能跨资产相关、持有期重叠；不相加为组合收益，也不把 allow/veto 均值差解释为因果提升。",
    "冷启动或证据不足为 abstain（未知），不是 veto（有证据拒绝）；缺失与未成熟标签不填零。",
    "评估使用已含执行成本的净收益标签，不重复扣除候选时点估计成本。",
    "有效样本量与时间块有效样本量是依赖性诊断，不保证观测独立；近似保守置信界不保证覆盖率。",
    "固定预定义状态轴与等权组合不等于论文完整实现；未启用 Wasserstein 聚类、动态归因权重或动态仓位。",
    "只有严格时间顺序的冻结预测才可用于此诊断；已使用的历史数据不因此成为新的样本外准入证据。",
]


def signal_meta_layer_digest(payload):
    """Canonical research identity, separate from official trading identity."""
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _mean(values):
    return math.fsum(values)/len(values) if values else None


def _ranks(values):
    """Average ranks, including ties, without an optional scipy dependency."""
    ordered = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0]*len(values)
    start = 0
    while start < len(ordered):
        end = start+1
        while end < len(ordered) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        for index in ordered[start:end]:
            ranks[index] = (start+end-1)/2.0
        start = end
    return ranks


def _rank_correlation(pairs):
    if len(pairs) < 3:
        return None
    x, y = (_ranks(list(values)) for values in zip(*pairs))
    xm, ym = _mean(x), _mean(y)
    xx = math.fsum((v-xm)**2 for v in x)
    yy = math.fsum((v-ym)**2 for v in y)
    if not xx or not yy:
        return None
    return math.fsum((a-xm)*(b-ym) for a, b in zip(x, y))/math.sqrt(xx*yy)


def _identity(row):
    return row.get("candidate_id"), row.get("horizon_bars"), row.get("fold_id")


def _diagnostic_rows(payload):
    """Keep the prediction cohort even when evaluation labels are absent."""
    predictions = payload.get("predictions", [])
    evaluations = payload.get("evaluations", [])
    if not predictions:
        return [dict(row) for row in evaluations]
    labels = defaultdict(list)
    for evaluation in evaluations:
        labels[_identity(evaluation)].append(evaluation)
    rows = []
    for prediction in predictions:
        matches = labels.get(_identity(prediction), [])
        row = dict(prediction)
        if len(matches) == 1:
            # Forecasts are the frozen predictions, never later label-side edits.
            evaluation = matches[0]
            for key in ("label_status", "realized_net_bps", "execution_flags"):
                row[key] = evaluation.get(key)
        else:
            row.update(label_status="duplicate_evaluation" if matches else "missing_evaluation",
                       realized_net_bps=None, execution_flags=[])
        if "baseline_ev_bps" not in row:
            row["baseline_ev_bps"] = (row.get("prior") or {}).get("mean_bps")
        rows.append(row)
    return rows


def signal_meta_diagnostics(payload):
    """Mature executable labels only; abstentions remain a separate cohort."""
    buckets = defaultdict(list)
    for row in _diagnostic_rows(payload):
        key = tuple(row.get(k) for k in
                    ("fold_id", "strategy", "direction", "horizon_bars", "status"))
        buckets[key].append(row)
    diagnostics = []
    for key, rows in sorted(buckets.items(), key=lambda item: tuple(str(v) for v in item[0])):
        eligible, conditional, paired = [], [], []
        matured, flagged, invalid, censored, unresolved = 0, 0, 0, 0, 0
        label_statuses = Counter()
        for row in rows:
            label_status = str(row.get("label_status") or "missing_evaluation")
            label_statuses[label_status] += 1
            if label_status != "matured":
                if label_status.startswith("censored"):
                    censored += 1
                else:
                    unresolved += 1
                continue
            matured += 1
            if row.get("execution_flags"):
                flagged += 1
                continue
            realized = _number(row.get("realized_net_bps"))
            if realized is None:
                invalid += 1
                continue
            eligible.append(row)
            estimate = _number(row.get("estimate_bps"))
            if estimate is None:
                continue
            conditional.append((estimate, realized))
            baseline = _number(row.get("baseline_ev_bps"))
            if baseline is not None:
                paired.append((estimate, baseline, realized))
        diagnostics.append({
            **dict(zip(("fold_id", "strategy", "direction", "horizon_bars", "predicted_status"), key)),
            "prediction_count": len(rows), "matured_count": matured,
            "censored_count": censored, "unresolved_count": unresolved,
            "non_executable_count": flagged, "invalid_net_label_count": invalid,
            "eligible_count": len(eligible), "forecast_count": len(conditional),
            "paired_forecast_count": len(paired),
            "positive_count": sum(_number(row["realized_net_bps"]) > 0 for row in eligible),
            "negative_count": sum(_number(row["realized_net_bps"]) < 0 for row in eligible),
            "mean_realized_net_bps": _mean([_number(row["realized_net_bps"]) for row in eligible]),
            "mean_forecast_bps": _mean([estimate for estimate, _ in conditional]),
            "mean_realized_scored_bps": _mean([realized for _, realized in conditional]),
            "conditional_mse_bps2": _mean([(estimate-realized)**2 for estimate, _, realized in paired]),
            "baseline_mse_bps2": _mean([(baseline-realized)**2 for _, baseline, realized in paired]),
            "conditional_rank_correlation": _rank_correlation(conditional),
            "baseline_rank_correlation": _rank_correlation([(baseline, realized) for _, baseline, realized in paired]),
            "label_statuses": dict(sorted(label_statuses.items())),
            "interpretation": INTERPRETATION,
        })
    return diagnostics


def _format(value):
    return "未知" if value is None else f"{value:.2f}"


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_signal_meta_layer_report(payload, output_dir):
    """Write read-only research facts and a Chinese explanation of their limits."""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    diagnostics = signal_meta_diagnostics(payload)
    predictions = payload.get("predictions", [])
    evaluations = payload.get("evaluations", [])
    folds = payload.get("folds", [])
    files = {
        "ev_predictions.csv": (predictions, ["candidate_id", "strategy", "direction", "symbol",
            "horizon_bars", "available_at", "fold_id", "training_cutoff", "estimate_bps",
            "lower_bound_bps", "upper_bound_bps", "effective_samples", "effective_blocks",
            "status", "reason", "would_allow", "model_version"]),
        "ev_evaluations.csv": (evaluations, ["candidate_id", "fold_id", "horizon_bars", "status",
            "label_status", "realized_net_bps", "execution_flags", "estimate_bps", "baseline_ev_bps"]),
        "ev_cells.csv": (payload.get("cell_snapshots", []), ["fold_id", "strategy", "direction",
            "horizon_bars", "axis", "state", "raw_count", "weight_mass", "effective_samples",
            "effective_blocks", "mean_bps"]),
        "ev_folds.csv": (folds, ["fold_id", "start", "end", "train_start", "training_cutoff",
            "training_observations", "training_digest"]),
        "ev_diagnostics.csv": (diagnostics, ["fold_id", "strategy", "direction", "horizon_bars",
            "predicted_status", "prediction_count", "eligible_count", "censored_count",
            "unresolved_count", "mean_realized_net_bps", "mean_forecast_bps",
            "conditional_mse_bps2", "baseline_mse_bps2"]),
    }
    for name, (rows, fields) in files.items():
        _write_csv(root/name, rows, fields)
    status_counts = Counter(row.get("status", "unknown") for row in predictions)
    forecast_count = sum(row["forecast_count"] for row in diagnostics)
    validation = payload.get("validation", {})
    failed_validation = sorted(key for key, value in validation.items() if value is False)
    summary = {
        "schema": payload.get("schema", "signal_ev/v1"),
        "status": payload.get("status", "incomplete"),
        "evidence": "diagnostic_only" if forecast_count else "insufficient",
        "evidence_definition": "diagnostic_only means at least one finite frozen forecast with a mature executable label; it is not statistical validation or admission",
        "research_payload_sha256": signal_meta_layer_digest(payload),
        "policy": payload.get("policy", {}), "model_version": payload.get("model_version"),
        "context_definition": payload.get("context_definition", {}),
        "input_identity": payload.get("input_identity", {}),
        "prediction_count": len(predictions), "evaluation_count": len(evaluations),
        "fold_count": len(folds), "cell_count": len(payload.get("cell_snapshots", [])),
        "status_counts": {status: status_counts[status] for status in
            sorted({"allow", "veto", "abstain", *status_counts})},
        "matured_count": sum(row["matured_count"] for row in diagnostics),
        "eligible_count": sum(row["eligible_count"] for row in diagnostics),
        "censored_count": sum(row["censored_count"] for row in diagnostics),
        "unresolved_count": sum(row["unresolved_count"] for row in diagnostics),
        "non_executable_count": sum(row["non_executable_count"] for row in diagnostics),
        "forecast_count": forecast_count,
        "paired_forecast_count": sum(row["paired_forecast_count"] for row in diagnostics),
        "ingestion_audit": payload.get("ingestion_audit", {}),
        "validation": validation, "failed_validation": failed_validation,
        "errors": payload.get("errors", []),
        "interpretation": INTERPRETATION, "limitations": LIMITATIONS,
        "artifacts": sorted([*files, "ev_summary.json", "ev_report.md"]),
    }
    (root/"ev_summary.json").write_text(
        json.dumps(json.loads(canonical_json(summary)), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    lines = ["# P1 条件期望收益研究报告", "",
        f"工程／数据状态：{summary['status']}；研究证据：{summary['evidence']}。", "",
        f"冻结预测 {len(predictions)} 条，时间折 {len(folds)} 个；allow {status_counts['allow']} 条、"
        f"veto {status_counts['veto']} 条、abstain {status_counts['abstain']} 条。", "",
        f"已成熟且可执行标签 {summary['eligible_count']} 条，其中具有有限预测值的 {forecast_count} 条；"
        f"截尾 {summary['censored_count']} 条、未解决 {summary['unresolved_count']} 条、"
        f"不可执行诊断 {summary['non_executable_count']} 条。", "",
        "complete 只表示研究数据与工程流程完整，不表示盈利、统计显著、可实盘或通过策略准入。"
        "diagnostic_only 只表示至少存在一条可计算误差的冻结预测；全部未知时为 insufficient。", "",
        "## 模型与时间约束", "",
        "按时间顺序冻结每折训练截止时间与模型版本；只能纳入在截止时间前已成熟可见的标签。"
        "当前预测之后才到期的标签仅用于事后评估，不能倒流到该次预测。具体窗口、半衰期、收缩强度、"
        "最小样本量及门槛以 ev_summary.json 中的 policy 和 ev_folds.csv 为准。", "",
        "半衰期衰减让旧观察的权重随时间下降；收缩把稀疏状态单元拉向同策略／方向／期限的先验，"
        "不把少数高收益误当作可靠优势。weight_mass 为衰减及状态归属后的权重和；Kish 有效样本量"
        "为权重和的平方除以平方权重和；时间块有效样本量进一步按共同时间块合并权重，"
        "用于保守处理同日跨资产相关与重叠期限。三者不能混用。", "",
        "置信上下界是考虑时间块依赖的近似保守诊断，并非覆盖率保证。abstain 表示信息不足、"
        "上下文缺失或模型不可用；不得把它合并进否决统计。预定义轴固定、轴组合等权，"
        "本阶段不学习 Wasserstein 状态、不启用动态归因或按预测调整仓位。", "",
        "## 冻结预测诊断", "",
        "只有 label_status=matured、净收益有限且 execution_flags 为空的记录进入下表收益与误差计算。"
        "净收益标签已含成本，不再扣一次预估成本。均值为候选级而非组合级；条件模型与先验基线 MSE"
        "在相同的成对有限样本上计算。秩相关至少需要 3 个有限样本且两侧均有变化；否则保留未知。", "",
        "| 时间折 | 策略 | 方向 | 期限 | 预测状态 | 预测数 | 可评估 | 截尾 | 未解决 | 不可执行 | 平均实际净 bp | 平均预测 bp | 条件 MSE | 基线 MSE |",
        "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in diagnostics:
        values = [row[k] for k in ("fold_id", "strategy", "direction", "horizon_bars",
            "predicted_status", "prediction_count", "eligible_count", "censored_count",
            "unresolved_count", "non_executable_count")]
        values += [_format(row[k]) for k in ("mean_realized_net_bps", "mean_forecast_bps",
            "conditional_mse_bps2", "baseline_mse_bps2")]
        lines.append("| "+" | ".join(_cell(value) for value in values)+" |")
    if not diagnostics:
        lines += ["", "没有可分组的预测或评估记录；所有统计保持未知，不构造零收益结论。"]
    lines += ["", "## 解释边界", "", *(f"- {limitation}" for limitation in LIMITATIONS)]
    if failed_validation or summary["errors"]:
        lines += ["", "## 数据／验证异常", "",
            "存在异常或未通过的检查；先处理证据完整性，不据此选择策略或调整门槛。", "",
            f"未通过检查：{', '.join(failed_validation) or '见 errors 字段'}；"
            "完整异常及输入身份见 ev_summary.json。"]
    lines += ["", "逐笔冻结预测、标签、状态单元与训练折分别见 ev_predictions.csv、ev_evaluations.csv、"
        "ev_cells.csv、ev_folds.csv；完整分组诊断见 ev_diagnostics.csv。"]
    (root/"ev_report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return summary
