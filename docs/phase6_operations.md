# Phase 6 模拟实盘、准入与扩容治理

本阶段的权威任务是路线图中的 T-6.1 至 T-6.8。代码提供可审计、失败关闭的证据链，
但不会把尚未发生的 8–12 周 Paper 运行、人工批准或真实资金观察伪装成已完成。

## 证据评估入口

将证据保存为一个 JSON 对象，然后运行：

~~~powershell
python -m core.phase6 --input reports/phase6/evidence_bundle.json --output reports/phase6/phase6_report.json
~~~

退出码 `0` 表示八项任务的真实证据全部通过；退出码 `2` 表示至少一个门槛未通过，
报告会保留每项 gate、覆盖率和失败原因。输出使用原子替换，适合由调度器重复执行。

可从 [evidence_bundle_template.json](phase6/evidence_bundle_template.json) 复制一份证据模板。
模板默认全部为空或未批准，因此不会意外放行；只填写真实运行数据和已经签署的批准。

## 八项任务与证据

| 任务 | 必需证据 | 自动门槛 |
| --- | --- | --- |
| T-6.1 Shadow | `backtest_signals`、`shadow_signals` | `record_id`、信号时间、策略、动作和冻结规则版本逐项一致 |
| T-6.2 Paper | `paper_observations` | 默认至少 56 个自然日、至少两种市场状态、无未解决事件 |
| T-6.3 逐笔对账 | `expected_lifecycle`、`actual_lifecycle` | signals/orders/fills/positions/costs/pnl 六层均非空且 100% 一致 |
| T-6.4 校准 | `execution_observations` | 滑点、价差、拒单率和延迟误差均在配置容忍区间内 |
| T-6.5 监控 | `monitoring_snapshots` | 生命周期、成本、回撤、状态切换和数据质量五维齐全；关键告警投递已演练 |
| T-6.6 准入 | P0、最终 Holdout、T-6.1 至 T-6.5、人工批准 | 所有 gate 同时通过才允许 Micro Live |
| T-6.7 灰度 | 单交易所/单标的/单策略范围、观察记录、逐笔对账 | 低杠杆、无未解释事件、对账覆盖率 100% |
| T-6.8 扩容 | 当前/候选范围、容量/成本/风险复审、人工批准 | 每次只能改变一个维度，任何复审失败都禁止扩容 |

逐笔记录必须包含稳定的 `record_id`。时间戳必须带时区；建议统一使用 UTC ISO-8601。
实际记录可以包含 `observed_at`、`received_at`、`recorded_at` 和 `source` 等采集元数据，
这些字段不会造成业务事实误报。

## 实盘安全边界

`run_live.py --live` 仍要求显式真实模式、最小权限账户、提现禁用、独立环境批准、
回滚快照、单交易所和单标的。`--r8-evidence` 可以使用旧 R7 通过报告，也可以使用
本模块生成且 `admission_passed: true` 的 Phase 6 报告。Phase 6 报告的总体 `passed`
在真实 Micro Live 和扩容观察完成前可以保持 `false`；这不会弱化 T-6.6 的独立准入门槛。

任何 unknown order、unknown position、数据质量失败、对账差异或未解释 P0 事件都应立即
停止新增风险。收益良好不能自动扩大资金、标的、交易所、策略、杠杆或无人值守时间。

只读运维看板会自动读取 Phase 6 报告，并显示五个监控维度：

~~~powershell
python -m dashboard --phase6-report reports/phase6/phase6_report.json
~~~
