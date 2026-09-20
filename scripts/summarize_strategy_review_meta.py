"""Report-only stratification of completed P1/P2 research artifacts.

No model fitting, engine imports, label mutation or parameter selection occurs.
Prediction CSVs are streamed; only support values and small counters are kept.
Existing status strata form disjoint diagnostic populations. Their MSE values
are combined using paired_forecast_count, the actual MSE denominator.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import median


KEY_FIELDS = ("strategy", "direction", "horizon_bars", "fold_id")
AXES = ("trend", "efficiency", "volatility")
COUNT_FIELDS = ("prediction_count", "matured_count", "eligible_count", "forecast_count",
                "paired_forecast_count", "censored_count", "unresolved_count",
                "non_executable_count", "invalid_net_label_count")
SOURCE_FILES = ("ev_predictions.csv", "ev_diagnostics.csv", "ev_summary.json",
                "p2_predictions.csv", "p2_diagnostics.csv", "p2_summary.json",
                "p2_regime_models.json", "p2_attribution.csv")


def _finite(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _count(value):
    number = _finite(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _key(row):
    return (row.get("strategy") or None, row.get("direction") or None,
            _count(row.get("horizon_bars")), row.get("fold_id") or None)


def _object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return None
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else None
    except (TypeError, ValueError):
        return None


def _csv_rows(path):
    if not path.exists():
        return
    csv.field_size_limit(32 * 1024 * 1024)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def weighted_paired_mse(rows, field):
    """An absent positive-count subgroup makes the total MSE unknown.

    Zero pairs means no error estimate. The known denominator is reported even
    when the complete denominator is unknown; it never substitutes for it.
    """
    denominator, known, numerator, incomplete = 0, 0, 0.0, False
    if not rows:
        return {"value": None, "paired_count": None, "known_pair_count": None, "status": "diagnostics_missing"}
    for row in rows:
        count, value = _count(row.get("paired_forecast_count")), _finite(row.get(field))
        if count is None:
            incomplete = True
            continue
        denominator += count
        if count > 0:
            if value is None or value < 0:
                incomplete = True
            else:
                known += count
                numerator += value * count
    return {"value": numerator / denominator if denominator and not incomplete else None,
            "paired_count": denominator if all(_count(row.get("paired_forecast_count")) is not None for row in rows) else None,
            "known_pair_count": known,
            "status": "incomplete_error_evidence" if incomplete else "paired_estimate" if denominator else "no_paired_forecasts"}


def membership_status(states):
    """Unknown memberships are not low-entropy or successful state forecasts."""
    if not isinstance(states, dict) or not states:
        return "missing_or_invalid_membership"
    if "unknown" in states:
        return "explicit_unknown"
    probabilities = [_finite(value) for value in states.values()]
    if any(value is None or value < 0 for value in probabilities):
        return "invalid_probability"
    if not math.isclose(math.fsum(probabilities), 1, rel_tol=0, abs_tol=1e-9):
        return "invalid_probability_mass"
    return "known"


def _stats(values, name):
    return {name + "_known_count": len(values), name + "_min": min(values) if values else None,
            name + "_median": median(values) if values else None, name + "_max": max(values) if values else None}


def _new_group():
    return {"prediction_count": 0, "statuses": Counter(), "abstentions": Counter(),
            "effective_samples": [], "effective_blocks": [], "support_invalid": Counter(),
            "memberships": {axis: Counter() for axis in AXES}, "all_axes_known": 0,
            "model_ids": Counter(), "missing_model_id": 0, "prediction_attribution": Counter(),
            "signal_versions": set(), "snapshot_versions": set(), "timeframes": set()}


def _prediction_groups(path, layer):
    groups = defaultdict(_new_group)
    for row in _csv_rows(path):
        group = groups[_key(row)]
        group["prediction_count"] += 1
        status = row.get("status") or "unknown"
        group["statuses"][status] += 1
        if status == "abstain":
            group["abstentions"][row.get("reason") or "unknown"] += 1
        for field in ("effective_samples", "effective_blocks"):
            number = _finite(row.get(field))
            if number is not None and number >= 0:
                group[field].append(number)
            elif row.get(field) not in (None, ""):
                group["support_invalid"][field] += 1
        for field in ("signal_version", "snapshot_version", "timeframe"):
            if row.get(field):
                group[{"signal_version": "signal_versions", "snapshot_version": "snapshot_versions",
                       "timeframe": "timeframes"}[field]].add(row[field])
        if layer == "P2":
            memberships = _object(row.get("memberships")) or {}
            statuses = [membership_status(memberships.get(axis)) for axis in AXES]
            for axis, current in zip(AXES, statuses):
                group["memberships"][axis][current] += 1
            group["all_axes_known"] += all(current == "known" for current in statuses)
            model_id = row.get("regime_model_id")
            if model_id:
                group["model_ids"][model_id] += 1
            else:
                group["missing_model_id"] += 1
            group["prediction_attribution"][row.get("attribution_status") or "unknown"] += 1
    return groups


def _diagnostic_groups(path, layer):
    groups = defaultdict(list)
    for row in _csv_rows(path):
        if layer == "P2" and row.get("diagnostic_kind") != "cohort":
            continue  # Global pairwise comparisons cannot be assigned to a book/fold.
        groups[_key(row)].append(row)
    return groups


def _strict_sum(rows, field):
    values = [_count(row.get(field)) for row in rows]
    return sum(values) if values and all(value is not None for value in values) else None


def _state_evidence(group, models, attribution, *, models_present, attribution_present, predictions_present):
    availability = {}
    for axis in AXES:
        axes = [(_object(model.get("axes")) or {}).get(axis) for model in models]
        availability[axis] = {
            "model_count": len(models),
            "available_model_count": sum(isinstance(value, dict) and value.get("status") == "available" for value in axes),
            "unavailable_reasons": dict(Counter(value.get("reason") or "unknown" for value in axes
                if isinstance(value, dict) and value.get("status") != "available")),
            "missing_axis_count": sum(not isinstance(value, dict) for value in axes),
        }
    model_ids = {model.get("model_id") for model in models}
    missing_reference = sum(count for identity, count in group["model_ids"].items() if identity not in model_ids)
    fallback = [row for row in attribution if row.get("status") == "uniform_fallback"]
    output = {
        "regime_model_count": len(models) if models_present else None,
        "regime_all_axes_available_model_count": sum(all(
            (_object((_object(model.get("axes")) or {}).get(axis)) or {}).get("status") == "available" for axis in AXES)
            for model in models) if models_present else None,
        "regime_axis_model_availability": availability if models_present else None,
        "regime_missing_model_id_prediction_count": group["missing_model_id"] if predictions_present else None,
        "regime_unlinked_model_id_prediction_count": missing_reference if models_present and predictions_present else None,
        "regime_all_axes_known_prediction_count": group["all_axes_known"] if predictions_present else None,
        "regime_axis_membership_counts": {axis: dict(group["memberships"][axis]) for axis in AXES} if predictions_present else None,
        "attribution_record_count": len(attribution) if attribution_present else None,
        "attribution_uniform_fallback_count": len(fallback) if attribution_present else None,
        "attribution_uniform_fallback_reasons": dict(Counter(row.get("reason") or "unknown" for row in fallback)) if attribution_present else None,
        "attribution_prediction_status_counts": dict(group["prediction_attribution"]) if predictions_present else None,
    }
    for field in ("sample_count", "effective_blocks"):
        values = [value for row in attribution if (value := _finite(row.get(field))) is not None and value >= 0]
        output.update(_stats(values, "attribution_" + field) if attribution_present else {
            "attribution_" + field + suffix: None for suffix in ("_known_count", "_min", "_median", "_max")})
    return output


def summarize_meta(root):
    """Return per-layer/book/fold coverage and integrity notes without writes."""
    root = Path(root)
    model_path, attribution_path = root / "p2_regime_models.json", root / "p2_attribution.csv"
    models, attributions = defaultdict(list), defaultdict(list)
    if model_path.exists():
        payload = json.loads(model_path.read_text(encoding="utf-8"))
        for model in payload.get("models", []):
            models[_key({**model.get("book", {}), "fold_id": model.get("fold_id")})].append(model)
    for row in _csv_rows(attribution_path):
        attributions[_key(row)].append(row)
    output, issues = [], []
    for layer, prefix in (("P1", "ev"), ("P2", "p2")):
        prediction_path, diagnostics_path = root / f"{prefix}_predictions.csv", root / f"{prefix}_diagnostics.csv"
        predictions = _prediction_groups(prediction_path, layer)
        diagnostics = _diagnostic_groups(diagnostics_path, layer)
        keys = set(predictions) | set(diagnostics)
        if layer == "P2":
            keys |= set(models) | set(attributions)
        for key in sorted(keys, key=lambda values: tuple("" if value is None else str(value) for value in values)):
            group, rows = predictions.get(key, _new_group()), diagnostics.get(key, [])
            statuses = [row.get("predicted_status") or "unknown" for row in rows]
            duplicate_strata = len(statuses) != len(set(statuses))
            if duplicate_strata:
                issues.append({"layer": layer, **dict(zip(KEY_FIELDS, key)), "reason": "duplicate_diagnostic_status_strata"})
            diagnostic_count = _strict_sum(rows, "prediction_count")
            prediction_count = group["prediction_count"] if prediction_path.exists() else None
            partition = "unknown" if diagnostic_count is None or prediction_count is None else \
                "matched" if diagnostic_count == prediction_count and not duplicate_strata else "mismatch"
            if partition == "mismatch" and not duplicate_strata:
                issues.append({"layer": layer, **dict(zip(KEY_FIELDS, key)), "reason": "prediction_diagnostic_count_mismatch"})
            row = {"layer": layer, **dict(zip(KEY_FIELDS, key)),
                   "fold_phase": "initial_training_or_missing_fold" if key[-1] is None else "frozen_test_fold",
                   "prediction_count": prediction_count,
                   "status_counts": dict(group["statuses"]) if prediction_path.exists() else None,
                   "abstention_reasons": dict(group["abstentions"]) if prediction_path.exists() else None,
                   "diagnostic_prediction_count": diagnostic_count,
                   "diagnostic_partition": partition,
                   "diagnostic_status_stratum_count": len(rows) if diagnostics_path.exists() else None,
                   "signal_versions": sorted(group["signal_versions"]),
                   "snapshot_versions": sorted(group["snapshot_versions"]),
                   "timeframes": sorted(group["timeframes"])}
            for status in ("allow", "veto", "abstain"):
                row[status + "_count"] = group["statuses"][status] if prediction_path.exists() else None
            for field in ("effective_samples", "effective_blocks"):
                row.update(_stats(group[field], field) if prediction_path.exists() else {
                    field + suffix: None for suffix in ("_known_count", "_min", "_median", "_max")})
                row[field + "_invalid_count"] = group["support_invalid"][field] if prediction_path.exists() else None
            for field in COUNT_FIELDS[1:]:
                row[field] = _strict_sum(rows, field) if not duplicate_strata else None
            for field, label in (("conditional_mse_bps2", "conditional"), ("baseline_mse_bps2", "baseline")):
                mse = weighted_paired_mse(rows, field)
                valid_partition = not duplicate_strata and partition == "matched"
                row["weighted_" + field] = mse["value"] if valid_partition else None
                row[label + "_mse_known_pair_count"] = mse["known_pair_count"] if not duplicate_strata else None
                row[label + "_mse_status"] = mse["status"] if valid_partition else "unknown_or_invalid_partition"
            if layer == "P2":
                row.update(_state_evidence(group, models.get(key, []), attributions.get(key, []),
                    models_present=model_path.exists(), attribution_present=attribution_path.exists(),
                    predictions_present=prediction_path.exists()))
            output.append(row)
        # Release large support vectors before streaming the next layer.
        del predictions
    return output, issues


README = """# P1/P2 分层覆盖与预测误差

本表只读取已完成的研究导出，不运行模型或交易引擎。`coverage.csv` 每行按 P1/P2、策略、方向、持有期限及时间折分组；无时间折的初始训练记录单独保留。主评价仍为 TrendBreakout / long / 5 根柱，其他分组仅作诊断。

- `prediction_count`、allow/veto/abstain 及 `abstention_reasons` 来自逐条冻结预测。弃权代表证据不足，不能并入否决。空白表示缺失证据；有原始文件时真实计数为零才写零。
- `effective_samples` 和 `effective_blocks` 仅给出逐预测支持量的已知计数及最小值、中位数、最大值。不会相加，因为多个预测可能重复使用同一批历史或重叠时间块。这些分布也不等于独立样本量。
- `matured_count` 为标签已成熟；`eligible_count` 还要求实际净标签有限且没有执行排除标记；`forecast_count` 再要求冻结条件预测有限；`paired_forecast_count` 还要求同条先验基线有限。未知、截尾及不可执行标签不作为零收益。
- 原诊断表将预测状态分成互不重叠的小组。合并 MSE 使用 `Σ(小组 MSE × 小组成对样本数) / Σ(小组成对样本数)`，分母严格为 `paired_forecast_count`，不能用预测数或全部有限预测数。正分母小组缺少 MSE 时，合并误差保持未知；没有成对样本也不写零误差。`*_mse_known_pair_count` 明确披露实际可核验的分母。
- `diagnostic_partition` 检查诊断条数与预测条数一致且预测状态分组没有重复。异常时合并 MSE 保持未知。P2 原报告的全局多模型成对比较不能分配给某个策略或时间折，因此不会混入本表。
- P2 的 `regime_axis_model_availability` 按同账本及同时间折关联模型清单；“三轴模型均可用”与“预测当时三轴归属均已知”分别计数。归属中的 `unknown`、缺轴、无效概率、概率总和异常均保持未知，不能当作零熵或高确定性。
- `regime_missing_model_id_prediction_count` 表示预测自身没有状态模型编号；`regime_unlinked_model_id_prediction_count` 表示有编号却找不到该账本/折的模型记录。缺少模型清单时后一统计未知，不能据此宣称模型不存在。
- `attribution_uniform_fallback_count` 是该账本/折归因记录中的等权回退次数，并列出原因。归因样本与块支持也只报告分布，不与预测支持量相加。P1 不包含状态学习或动态归因，相关列空白。

这些是候选级、回顾性的覆盖和预测诊断，不能相加为账户收益，不能证明因果门控增益或实盘准入。`summary.json` 保留输入文件和本汇总脚本的哈希、源报告状态及异常；README 中的定义适用于本轮所有分组。
"""


def write_summary(source, output=None, *, p2_summary=None):
    source = Path(source).resolve()
    output = Path(output).resolve() if output is not None else source / "stratified_coverage"
    if output == source:
        raise ValueError("Use a separate summary directory to preserve source reports")
    output.mkdir(parents=True, exist_ok=True)
    rows, issues = summarize_meta(source)
    fields = list(dict.fromkeys(["layer", *KEY_FIELDS, *(key for row in rows for key in row)]))
    with (output / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _json(value) if isinstance(value, (dict, list)) else value for key, value in row.items()})
    source_paths = {name: source / name for name in SOURCE_FILES}
    if p2_summary is not None:
        source_paths["p2_summary.json"] = Path(p2_summary).resolve()
    sources = {name: _hash(path) if path.exists() else None for name, path in source_paths.items()}
    statuses = {}
    for label, name in (("P1", "ev_summary.json"), ("P2", "p2_summary.json")):
        path = source_paths[name]
        statuses[label] = json.loads(path.read_text(encoding="utf-8")).get("status") if path.exists() else None
    required = ["ev_predictions.csv", "ev_diagnostics.csv", "ev_summary.json", "p2_predictions.csv",
                "p2_diagnostics.csv", "p2_summary.json", "p2_regime_models.json", "p2_attribution.csv"]
    missing = [name for name in required if sources[name] is None]
    summary = {"schema": "strategy_review_meta_stratified/v1", "row_count": len(rows),
               "layer_row_counts": dict(Counter(row["layer"] for row in rows)),
               "status": "complete" if not issues and not missing and all(value == "complete" for value in statuses.values()) else "incomplete_evidence",
               "source_statuses": statuses, "missing_sources": missing, "issues": issues,
               "runner_sha256": _hash(Path(__file__)), "source_sha256": sources,
               "source_paths": {name: str(path) for name, path in source_paths.items()},
               "support_aggregation": "per_prediction_min_median_max_never_sum",
               "mse_weight": "paired_forecast_count_not_forecast_or_prediction_count",
               "primary_selector": {"strategy": "TrendBreakout", "direction": "long", "horizon_bars": 5},
               "official_account_mutation": False, "interpretation": "descriptive_research_only_no_profit_or_admission_claim"}
    (output / "summary.json").write_text(_json(summary) + "\n", encoding="utf-8")
    (output / "README.md").write_text(README, encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meta-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--p2-summary", type=Path, help="Append-only recovered summary; raw CSVs still come from --meta-dir")
    args = parser.parse_args()
    print(_json(write_summary(args.meta_dir, args.output, p2_summary=args.p2_summary)))


if __name__ == "__main__":
    main()
