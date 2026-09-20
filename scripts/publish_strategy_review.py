"""Publish completed registered evidence; pending work is never a failed experiment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pandas as pd
from scripts.run_strategy_review import save, digest
from scripts.summarize_strategy_review_families import run as summarize_families
from scripts.strategy_review_report_helpers import (
    GATE_KEYS, closure, combined_status, cross_evidence, diagnostic_summaries,
    finite, meta_evidence, public_evidence, read, validation_evidence, verified_runs,
)


def test_evidence(batch):
    path = batch / "engineering_tests_delivery.xml"
    if not path.exists():
        path = batch / "engineering_tests_final.xml"
    if not path.exists():
        path = batch / "engineering_tests.xml"
    if not path.exists():
        return {"status": "pending"}
    rows = list(ET.parse(path).getroot().iter("testsuite"))
    result = {key: sum(int(row.attrib.get(key, 0)) for row in rows)
              for key in ("tests", "failures", "errors", "skipped")}
    result["status"] = ("fail" if result["failures"] or result["errors"] else
                        "pass" if result["tests"] > result["skipped"] else "insufficient")
    result["artifact"] = path.name
    return result


def arm_summaries(protocol, matrix):
    output = []
    expected = len(protocol["rolling_windows"])
    for arm in protocol["arms"]:
        group = matrix.loc[matrix.arm.eq(arm["arm"])] if len(matrix) else pd.DataFrame()
        positive = int(group.return_pct.gt(0).sum()) if len(group) else 0
        median = float(group.return_pct.median()) if len(group) else None
        cohorts = group.get("cohort_count", pd.Series(dtype=float)).dropna()
        output.append({"arm": arm["arm"], "families": ",".join(arm["families"]),
            "windows": len(group), "expected_windows": expected, "positive_windows": positive,
            "negative_windows": int(group.return_pct.lt(0).sum()) if len(group) else 0,
            "median_return_pct": median, "worst_return_pct": float(group.return_pct.min()) if len(group) else None,
            "maximum_window_drawdown_pct": float(group.max_drawdown_pct.max()) if len(group) else None,
            "minimum_cohorts": int(cohorts.min()) if len(cohorts) else None,
            "statistical_windows_pass": int(group.statistical_gate_status.eq("pass").sum()) if len(group) else 0,
            "statistical_windows_insufficient": int(group.statistical_gate_status.isin(["insufficient", "pending"]).sum()) if len(group) else 0,
            "rolling_gate": "pending" if len(group) != expected else
                "pass" if positive >= protocol["gates"]["positive_windows_at_least"] and median > 0 else "fail"})
    return output


def legacy_evidence(batch, names):
    source = ROOT / "reports/strategy_remediation_20260914/research_gates.json"
    old, current = read(source, {}), {}
    for name in ("main_1", "final20"):
        benchmark = old.get(name, {}).get("benchmark_return_pct")
        row = read(batch / f"validation_results/runs/{name}/summary.json") if name in names else None
        current[name] = {"status": "pending" if row is None or benchmark is None else
            "pass" if row["return_pct"] > max(0, benchmark) else "fail",
            "strategy_net_return_pct": row.get("return_pct") if row else None,
            "benchmark_return_pct": benchmark,
            "rule": "net_return_pct > max(0, frozen_same_period_BTC_ETH_50_50_gross_return_pct)"}
    return {"source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else None,
            "frozen_20260914_results_unchanged": old, "current_legacy_benchmark_checks": current,
            "interpretation": "Frozen original cohort/bootstrap results retained separately; current cohort v2 statistics do not replace them"}


def engineering_comparison(batch, protocol):
    pairs = [("main", "validation", "main_1")]
    pairs += [(row["name"], "matrix", "baseline__" + row["name"]) for row in protocol["rolling_windows"]]
    pairs += [(name, "validation", name) for name in ("train60", "validation20", "final20")]
    parity = []
    for old_name, phase, new_name in pairs:
        old = read(batch / f"baseline_results/runs/{old_name}/digest.json")
        new = read(batch / f"{phase}_results/runs/{new_name}/digest.json")
        receipts = (batch / f"baseline_results/runs/{old_name}/review_identity.json", batch / f"{phase}_results/runs/{new_name}/review_identity.json")
        available = bool(old and new and all(path.exists() for path in receipts))
        equal = all(old.get(key) == new.get(key) for key in ("trades", "equity", "benchmark")) if available else None
        parity.append({"baseline": old_name, "revised": new_name, "available": available,
                       "economic_equal": equal, "status": "pending" if not available else "pass" if equal else "fail"})
    digests = [read(batch / f"validation_results/runs/main_{i}/digest.json")
               if (batch / f"validation_results/runs/main_{i}/review_identity.json").exists() else None for i in (1, 2, 3)]
    deterministic = None if not all(digests) else digests[0] == digests[1] == digests[2]
    return {"default_economics": parity, "three_run_determinism": deterministic,
            "determinism_status": "pending" if deterministic is None else "pass" if deterministic else "fail"}


def fmt(value, digits=3):
    number = finite(value)
    return "未知" if number is None else f"{number:.{digits}f}"


def required_deliverables(batch, protocol):
    """Report completion requires the registered analyses, even with disclosed gaps."""
    expected = {f"{arm['arm']}__{window['name']}" for arm in protocol["arms"]
                for window in protocol["rolling_windows"]}
    diagnostics = 0
    for name in expected:
        folder = batch / "matrix_results/runs" / name
        report = read(folder / "review_diagnostics.json", {})
        if report.get("status") == "complete" and all((folder / filename).exists() for filename in (
                "review_state_transitions.csv", "review_setup_timing.csv", "review_exit_quality.csv",
                "review_reentry_costs.csv", "review_score_buckets.csv")):
            diagnostics += 1
    strata = read(batch / "meta_review/stratified_coverage/summary.json", {})
    archive = read(batch / "public_data_archive_audit/manifest.json", {})
    forward = read(batch / "prospective_protocol.json", {})
    families = read(batch / "family_assessment.json", {})
    checks = {
        "matrix_diagnostics": bool(expected) and diagnostics == len(expected),
        "meta_stratified_report": strata.get("status") in {"complete", "incomplete_evidence"}
            and (batch / "meta_review/stratified_coverage/coverage.csv").exists(),
        "family_assessment": families.get("pipeline_status") == "complete"
            and len(families.get("families", [])) == 5,
        "official_archive_audit": archive.get("status") == "complete_audit"
            and archive.get("attempted_archives") == archive.get("registered_archives")
            and bool(archive.get("registered_archives")),
        "prospective_protocol": forward.get("status") == "pending_unseen_evidence"
            and forward.get("admission") == "paused_revalidation"
            and all(forward.get(key) for key in ("code_hash", "config_hash", "protocol_hash", "test_start", "test_end_exclusive", "mature_after"))
            and "data_access_log" in forward,
    }
    return {"complete": all(checks.values()), "checks": checks,
            "matrix_diagnostic_runs": diagnostics, "expected_matrix_diagnostic_runs": len(expected),
            "meta_stratified_evidence_status": strata.get("status", "pending"),
            "interpretation": "Delivered analyses may retain documented evidence gaps; future observation is not required today"}


def publish(batch):
    protocol = read(batch / "review_protocol.json")
    tests, quality = test_evidence(batch), read(batch / "engineering_quality.json", {})
    all_runs, identities = verified_runs(batch, protocol)
    all_runs.to_csv(batch / "all_run_summaries.csv", index=False)
    cost_columns = [key for key in all_runs.columns if key.startswith("financing_") or key in
                    {"name", "phase", "cost_scope", "cohort_admission_status", "statistical_gate_status_raw",
                     "statistical_gate_status", "net_closed_after_financing", "closed_net_reconciliation_status"}]
    all_runs[cost_columns].to_csv(batch / "cost_attribution_summary.csv", index=False)
    matrix = all_runs.loc[all_runs.phase.eq("matrix")].copy()
    if len(matrix):
        matrix["arm"] = matrix.name.str.rsplit("__", n=1).str[0]
    arms = arm_summaries(protocol, matrix)
    pd.DataFrame(arms).to_csv(batch / "matrix_summary.csv", index=False)
    families = (summarize_families(batch) if len(protocol["arms"]) == 52
                and len(protocol["rolling_windows"]) == 11 else {"families": []})
    window_rows = []
    for window in protocol["rolling_windows"]:
        group = matrix.loc[matrix.name.str.endswith("__" + window["name"])] if len(matrix) else pd.DataFrame()
        window_rows.append({**window, "completed_configurations": len(group),
            "positive_configurations": int(group.return_pct.gt(0).sum()) if len(group) else 0,
            "all_registered_configurations_lost": bool(len(group) == len(protocol["arms"]) and group.return_pct.lt(0).all())})
    pd.DataFrame(window_rows).to_csv(batch / "rolling_window_map.csv", index=False)
    validation = all_runs.loc[all_runs.phase.eq("validation")].set_index("name")
    names = set(validation.index)
    default = next(row for row in arms if row["arm"] == "baseline")
    costs = {}
    for multiplier in protocol["gates"]["cost_rerun_multipliers_required"]:
        name = f"cost_{multiplier:g}"
        if name not in names:
            costs[f"{multiplier:g}"] = "pending"
        else:
            row = validation.loc[name]
            costs[f"{multiplier:g}"] = "pass" if row.return_pct > 0 and row.max_drawdown_pct <= protocol["gates"]["max_drawdown_pct"] else "fail"
    final_gates = validation.loc["final20", "research_gates_true_cost"] if "final20" in names else {}
    final_status = combined_status(final_gates.get(key) for key in GATE_KEYS)
    legacy = legacy_evidence(batch, names)
    save(batch / "legacy_criteria.json", legacy)
    registered_status = combined_status([final_status, *costs.values(), default["rolling_gate"]])
    legacy_status = combined_status(row["status"] for row in legacy["current_legacy_benchmark_checks"].values())
    unified = {"schema": "strategy_review_assessment/v2", "final20": final_gates,
        "final20_status": final_status, "final20_period": read(batch / "reference_protocol.json")["segments"]["final20"],
        "cost_reruns": costs, "rolling": default, "prospective": "pending_unseen_evidence", "admission": "paused_revalidation",
        "trial_count": len(protocol["arms"]), "matrix_runs": len(matrix),
        "registered_retrospective_status": registered_status, "legacy_benchmark_status": legacy_status,
        "retrospective_assessment": combined_status([registered_status, legacy_status]),
        "observed_failed_requirements": [f"final20.{key}" for key, val in final_gates.items() if val == "fail"] +
            [f"cost_{key}" for key, val in costs.items() if val == "fail"] + (["rolling"] if default["rolling_gate"] == "fail" else []) +
            [f"legacy_benchmark.{key}" for key, val in legacy["current_legacy_benchmark_checks"].items() if val["status"] == "fail"],
        "bootstrap": {**protocol["gates"]["bootstrap"], "unit": "chronological_exit_cohorts_not_coins_or_bars", "confidence": .95},
        "units": {"return_pct": "percentage_points", "max_drawdown_pct": "positive_percentage_points", "profit_factor": "unitless"}}
    save(batch / "research_assessment.json", unified)
    comparison = engineering_comparison(batch, protocol)
    fixed = validation_evidence(batch, protocol, names)
    save(batch / "fixed_validation_summary.json", fixed)
    comparison["reversed_and_prefix"] = fixed["comparisons"]
    save(batch / "engineering_comparison.json", comparison)
    quality_status = "pending" if "passed" not in quality else "pass" if quality["passed"] is True else "fail"
    isolation = read(batch / "meta_review/official_isolation.json", {})
    isolation_status = ("pending" if not all(key in isolation for key in ("digest", "health", "allocation"))
                        else "pass" if all(isolation[key] is True for key in ("digest", "health", "allocation")) else "fail")
    engineering_status = combined_status([tests["status"], quality_status, comparison["determinism_status"],
        isolation_status, *(row["status"] for row in comparison["default_economics"]),
        *(row["status"] for row in fixed["comparisons"].values())])
    meta, cross, public = meta_evidence(batch), cross_evidence(batch), public_evidence(batch)
    save(batch / "meta_assessment.json", meta)
    save(batch / "cross_market_assessment.json", cross)
    save(batch / "public_evidence_summary.json", public)
    diagnostic_runs, diagnostic_families, score_buckets = diagnostic_summaries(batch, protocol, matrix)
    diagnostic_runs.to_csv(batch / "diagnostic_run_summary.csv", index=False)
    diagnostic_families.to_csv(batch / "diagnostic_family_summary.csv", index=False)
    score_buckets.to_csv(batch / "diagnostic_score_buckets.csv", index=False)
    stop_modes = []
    for row in identities:
        receipt = read(batch / f"{row['phase']}_results/runs/{row['name']}/review_identity.json")
        stop_modes.append({"phase": row["phase"], "name": row["name"], "resolved_initial_stop_mode": receipt.get("resolved_initial_stop_mode"), "identity_sha256": row["sha256"]})
    pd.DataFrame(stop_modes).to_csv(batch / "resolved_stop_policies.csv", index=False)
    counts = {phase: int(all_runs.phase.eq(phase).sum()) for phase in ("baseline", "matrix", "validation")}
    expected = {"baseline": len(protocol["baseline_specs"]), "matrix": len(protocol["arms"]) * len(protocol["rolling_windows"]), "validation": len(protocol["validation_specs"])}
    matrix_complete = counts["matrix"] == expected["matrix"] and all(row["windows"] == row["expected_windows"] for row in arms)
    issues = [{"id": key, "status": "已修复" if engineering_status == "pass" else "证据不足",
               "engineering_status": engineering_status, "evidence": f"{tests.get('artifact', 'engineering_tests_final.xml')}; engineering_comparison.json; engineering_quality.json"}
              for key in ("health_grouping", "persistent_reduce", "short_exit_and_stop_interface")]
    issues += [
        {"id": "official_alpha", "status": "研究失败" if unified["observed_failed_requirements"] else "证据不足", "retrospective_status": unified["retrospective_assessment"], "prospective_status": "pending_unseen_evidence", "evidence": "research_assessment.json; legacy_criteria.json", "live_admission": False},
        {"id": "gate_stop_score_neighborhoods", "status": "证据不足", "research_completion": "complete" if matrix_complete else "pending", "evidence": "matrix_summary.csv; diagnostic_family_summary.csv", "interpretation": "Fixed trials complete separately from stable-edge proof; no candidate selected, no new confirmatory sample", "defaults_changed": False},
        {"id": "meta_incremental_edge", "status": closure(meta["research_status"]), "research_completion": meta["pipeline_status"], "evidence": "meta_assessment.json; meta_review/primary_policy_comparison.csv"},
        {"id": "cross_market", "status": closure(cross["research_status"]), "research_completion": cross["pipeline_status"], "evidence": "cross_market_assessment.json"},
        {"id": "static_selection_bias", "status": "证据不足", "evidence": "public_evidence_summary.json", "interpretation": "Static selected universe; historical lifecycle evidence incomplete"},
        {"id": "unseen_evidence", "status": "证据不足", "evidence": "prospective_protocol.json", "research_completion": "pending_unseen_evidence"}]
    family_ids = {"timing": "state_confirmation_and_cooldown", "ablation": "factor_ablations",
                  "stops": "initial_and_trailing_stops", "scoring": "candidate_scoring", "donchian": "breakout_parameters"}
    issues += [{"id": family_ids[row["family"]], "status": closure(row["research_status"]),
                "research_completion": row["pipeline_status"],
                "registered_configurations": row["registered_configurations"],
                "rolling_pass_configurations": row["rolling_pass_configurations"],
                "evidence": "family_assessment.json; family_paired_window_differences.csv",
                "interpretation": "Rolling facts and descriptive paired changes are not full admission evidence"}
               for row in families["families"]]
    issues += [{"id": key, "status": "证据不足", "research_completion": meta["components"].get(component, {}).get("status", "pending"),
                "evidence": evidence, "interpretation": "Component completion does not establish incremental edge; support and matched controls evaluated separately"}
               for key, component, evidence in (
                   ("P1_predictive_gate", "p1", "meta_review/ev_summary.json; meta_review/stratified_coverage/coverage.csv"),
                   ("P2_regime_and_attribution", "p2", "meta_review/p2_summary.json; meta_review/stratified_coverage/coverage.csv"),
                   ("P3_shadow_account_uplift", "p3_primary", "meta_review/primary_policy_comparison.csv; meta_assessment.json"))]
    save(batch / "issue_closure.json", issues)
    deliverables = required_deliverables(batch, protocol)
    save(batch / "required_deliverables.json", deliverables)
    completion = {"schema": "strategy_review_completion/v2", "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_counts": counts, "expected_run_counts": expected, "matrix_complete": matrix_complete,
        "engineering_pass": engineering_status == "pass", "engineering_status": engineering_status,
        "meta_official_isolation": isolation_status,
        "evidence_pipeline_complete": all(counts[key] == expected[key] for key in expected) and meta["pipeline_status"] == "complete" and cross["pipeline_status"] == "complete" and public["pipeline_status"] == "complete" and public["lifecycle"]["status"] == "complete" and deliverables["complete"],
        "required_deliverables": deliverables,
        "tests": tests, "research": unified, "public_streams": public["matrix_streams"], "public_complete": public["complete_streams"],
        "admission": "paused_revalidation", "identities_sha256": digest(identities), "driver_amendment": read(batch / "driver_amendment.json", {}),
        "unseen_observation_is_not_required_for_current_pipeline_completion": True}
    save(batch / "completion.json", completion)
    lines = ["# 当前策略修复与固定研究验收", "", f"生成时间：{completion['generated_at']}。正式策略准入保持 **paused_revalidation**。", "",
        f"工程验收：**{engineering_status}**；默认策略回顾性统计状态：**{unified['retrospective_assessment']}**；准入证据：**{'研究失败' if unified['observed_failed_requirements'] else '证据不足'}**，前瞻证据仍待观察。",
        f"运行数量：修复前 {counts['baseline']}/{expected['baseline']}；固定矩阵 {counts['matrix']}/{expected['matrix']}；修复后验证 {counts['validation']}/{expected['validation']}。",
        "未完成运行记为 pending，样本不足记为 insufficient；两者均不记作已经失败的研究。研究流程完成与盈利证据通过分别统计。", "",
        f"全量测试：{tests}；静态／类型／依赖检查：{quality_status}；三次确定性：{comparison['determinism_status']}。",
        f"15组默认经济路径对照：{sum(row['status'] == 'pass' for row in comparison['default_economics'])}/15；币序和前缀对照：{ {name: row['status'] for name, row in fixed['comparisons'].items()} }。", "",
        "## 默认策略结果", "", "| 运行 | 净收益 % | 最大回撤 % | 事件组数 | 事件组 PF | 统计状态 |", "|---|---:|---:|---:|---:|---|"]
    for name in ("main_1", "train60", "validation20", "final20", "cost_1.5", "cost_2", "cost_3"):
        if name in names:
            row = validation.loc[name]
            pf = "无亏损分母（无界）" if bool(row.get("cohort_pf_unbounded", False)) else fmt(row.get("cohort_pf"))
            lines.append(f"| {name} | {fmt(row.return_pct)} | {fmt(row.max_drawdown_pct)} | {fmt(row.get('cohort_count'), 0)} | {pf} | {row.statistical_gate_status} |")
        else:
            lines.append(f"| {name} | — | — | — | — | pending |")
    lines += ["", f"最后20%固定时间段：{unified['final20_period']}，沿用冻结历史边界，并非新获得的未见样本。",
        f"默认滚动：盈利 {default['positive_windows']}/{default['windows']}，中位收益 {fmt(default['median_return_pct'])}%，状态 {default['rolling_gate']}。已观察到未通过要求：{unified['observed_failed_requirements']}。", "",
        f"全部已登记配置同时亏损的完整窗口：{[row['name'] for row in window_rows if row['all_registered_configurations_lost']]}；精确日期见 rolling_window_map.csv。完整固定顺序热力图见 figures/matrix_returns.png。这些描述不改变任何验收门槛。", "",
        "账户收益包含引擎实际扣除的全部建模成本；事件组PF原始值只包含成交成本。有非零账户融资但缺少唯一事件组归属的运行，其PF和集中度准入证据降为不足，原值及原先失败均保留。费用不按事后约定强行分配，详见 cost_attribution_summary.csv；原单次结果未覆盖。", "",
        "## 原冻结标准与本轮标准", "", "9月14日原门槛及原结果完整保存在 legacy_criteria.json，包括相对 BTC/ETH 50/50 毛收益基准的优势要求，未替换为本轮净收益为正。",
        f"当前同区间基准比较：{legacy['current_legacy_benchmark_checks']}。",
        "本轮事件组按同策略、UTC日、退出控制者归组，账户风险动作保留动作编号。至少30组、PF>1.15且95%区间下界>1；按时间顺序连续5个事件组作循环块抽样，种子42、2000次，单位是事件组而非币种或bar。",
        "表中收益和最大回撤使用百分数，PF无单位。主区间期末估值转移不计为退出盈利事件；明确要求强制退出的独立分段和滚动窗口保留真实含成本退出。", "",
        "## 全部参数研究", "", "按登记顺序完整披露，没有自动选优或正式参数写回。冷却0表示无后续冷却，状态切换当根仍跳过。", "",
        "52个配置均只运行11个滚动窗口。最后20%分段、全区间成本1.5/2/3倍和其他固定验证仅针对默认策略；非默认配置的滚动标准通过不表示已完成全部准入要求。", "",
        "| 配置 | 完成窗口 | 盈利窗口 | 中位收益 % | 最大窗口回撤 % | 滚动标准 |", "|---|---:|---:|---:|---:|---|"]
    for row in arms:
        lines.append(f"| {row['arm']} | {row['windows']}/{row['expected_windows']} | {row['positive_windows']} | {fmt(row['median_return_pct'])} | {fmt(row['maximum_window_drawdown_pct'])} | {row['rolling_gate']} |")
    lines += ["", "状态确认、突破过滤、信号至成交、浮盈回吐、持仓期间有利价格变化捕获、再次入场成本和资金竞争摘要见 diagnostic_run_summary.csv；家族描述性中位数见 diagnostic_family_summary.csv；分数分组已平部分净损益见 diagnostic_score_buckets.csv。",
        "MFE及observed_trend_capture仅衡量实际持仓期间观察到的有利价格变化：退出前已完成bar加退出价格，不包含退出bar后续极值，也不是假设抓住整段趋势的利润。不同窗口重叠，不能当作独立样本；被拒信号的未知反事实收益未填零。解析后止损模式见 resolved_stop_policies.csv。", "",
        "家族分别验收见 family_assessment.json；每配置与相同窗口基线的收益和回撤差见 family_paired_window_differences.csv。共享基线在四家族引用，55个家族配置引用仍仅对应52个唯一配置、572次引擎运行。", "",
        "| 家族 | 完成配置 | 局部滚动通过 | 研究结论 |", "|---|---:|---:|---|"]
    for row in families["families"]:
        lines.append(f"| {row['family']} | {row['complete_configurations']}/{row['registered_configurations']} | {row['rolling_pass_configurations']} | {closure(row['research_status'])} |")
    lines += ["", "## 固定压力与一致性验证", "", f"1%随机缺失的20个登记种子42～61：{fixed['missing_one_percent']}。",
        f"独立起跑：{fixed['independent_starts']}。", f"其他压力披露：{fixed['stress_disclosures']}。",
        "独立起跑使用新资金和新状态，不要求等于持续账户历史切片。币序和前缀检查比较实际成交经济字段及同期标记权益。", "",
        "## 元层、25%对照与跨市场", "", f"元层流程：{meta['pipeline_status']}；研究结论：{closure(meta['research_status'])}；实际组件状态：{meta['components']}。",
        f"P1覆盖：{meta['p1']}。P2覆盖：{meta['p2']}。",
        "主评价固定为TrendBreakout多头、五根实际bar；baseline、gate、sizing及固定25%名义金额对照使用独立等额初始资本。", "",
        "| 主评价账户 | 活动 | 收益 % | 最大回撤 % | 已平候选 | 指标有效 |", "|---|---|---:|---:|---:|---|"]
    for row in meta["primary_accounts"]:
        ret, dd = finite(row.get("return_fraction")), finite(row.get("max_drawdown_fraction"))
        lines.append(f"| {row.get('arm')} | {row.get('activity')} | {fmt(ret * 100 if ret is not None else None)} | {fmt(dd * 100 if dd is not None else None)} | {row.get('finite_closed_candidate_count')} | {row.get('metrics_valid')} |")
    lines += ["", f"实际对照差异：{meta['descriptive_comparisons']}。证据限制：{meta['observed_limitations']}。",
        "组件complete仅表示数据和计算完成，不表示模型增益通过；没有事后挑选元层显著性门槛。",
        f"跨市场流程：{cross['pipeline_status']}；结论：{closure(cross['research_status'])}；原始回顾性状态：{cross['retrospective_status']}；成对结果：{cross['paired_comparisons']}。",
        f"跨市场固定对照不构成完整准入试验，尚缺证据：{cross['missing_required_research_evidence']}。", "",
        "## 公开数据与生命周期", "", f"采用public_data_validated/manifest.json：有效bar {public['rows']}，完整流 {public['complete_streams']}/{public['matrix_streams']}，缺失 {public['missing_bars']}；主评价完整流 {public['historical_complete_streams']}，近期完整流 {public['recent_complete_streams']}。",
        "逐流缺口和拒绝原因见public_evidence_summary.json，原始响应及初版证据保留；未换源、未填零。",
        f"原有生命周期公告核对 {public['lifecycle']['existing_corroborated']}/5；当前合约资料核对 {public['lifecycle']['current_instruments_verified']}/12，当前资料不能证明全部历史生命周期。",
        f"Binance归档ZIP及CHECKSUM补充核对：{public['binance_archive_audit'].get('status', 'pending')}；已核验归档 {public['binance_archive_audit'].get('official_checksum_verified_archives', '未知')}/{public['binance_archive_audit'].get('registered_archives', '未知')}，与REST数值差异行 {public['binance_archive_audit'].get('mismatched_numeric_rows', '未知')}。实际字节与bar覆盖另见public_evidence_summary.json。",
        "归档核对完成不代表与REST完全一致。24条成交量差异全部位于实际100根预热内，可能影响指标与排序，不能声称与策略无关；6根短周期缺口则位于实际预热支持之外。逐字段差异保留在public_data_archive_audit。研究继续使用冻结REST输入，未追加未登记重跑。",
        "跨市场采用现金现货、无杠杆、共同研究成本；四小时与日线使用相同bar参数，所以时钟期限不同。不复刻历史保证金账户或历史手续费档位。", "",
        "## 逐项关闭与边界", "", "| 问题 | 结论 |", "|---|---|"]
    lines += [f"| {row['id']} | {row['status']} |" for row in issues]
    if completion["driver_amendment"]:
        lines += ["", f"驱动修订记录：{completion['driver_amendment']}。启动前驱动失败与成功引擎运行分别计数，前者不算额外策略试验。"]
    lines += ["", "固定60币选择偏差仍在；回顾性公开补充不是未见样本。前瞻候选固定为修复后默认策略，冻结后隔离30天、观察180天并等待标签成熟，保持pending_unseen_evidence。",
        "各运行身份及输出校验见review_identity.json；进度见completion.json。研究不会自动解除paused_revalidation。"]
    (batch / "研究验收报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Published: counts={counts}; engineering={engineering_status}; research={unified['retrospective_assessment']}", flush=True)
    return completion


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=ROOT / "reports/strategy_review_20260919")
    publish(parser.parse_args().batch.resolve())
