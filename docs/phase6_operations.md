# Phase 6 模拟实盘、准入与扩容治理

本阶段的权威任务是路线图中的 T-6.1 至 T-6.8。代码提供可审计、失败关闭的证据链，
但不会把尚未发生的 8–12 周 Paper 运行、人工批准或真实资金观察伪装成已完成。

## 证据评估入口

将证据保存为一个 JSON 对象，然后运行：

~~~powershell
python -m core.admission_gates --input reports/phase6/evidence_bundle.json --output reports/phase6/phase6_report.json
~~~

退出码 `0` 表示输入证据在八项任务的计算门槛中全部通过；该工具不能独立认证调用方输入的真实性。退出码 `2` 表示至少一个门槛未通过，
报告会保留每项 gate、覆盖率和失败原因。输出使用原子替换，适合由调度器重复执行。

可从 [evidence_bundle_template.json](phase6/evidence_bundle_template.json) 复制一份证据模板。
模板默认全部为空或未批准，因此不会意外放行；只填写真实运行数据和已经签署的批准。

## 八项任务与证据

| 任务 | 必需证据 | 自动门槛 |
| --- | --- | --- |
| T-6.1 Shadow | `backtest_signals`、`shadow_signals` | `record_id`、信号时间、策略、动作和冻结规则版本逐项一致 |
| T-6.2 Paper | `paper_observations` | 不可降低的下限为 56 个连续自然日、两种市场状态；冻结协议更长或状态更多时采用更严要求；逐日覆盖、无观测断档、无未解决事件 |
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

`run_live.py --live` 要求显式真实模式、最小权限账户、提现禁用、独立环境批准、回滚快照、单交易所、单标的和唯一非 Cash 路由策略。正式策略仍须满足既有治理准入；当前 `paused_revalidation` 不因这次门禁实现而解除。

`--r8-evidence` 现在只接受 `r8-admission-evidence/v1` 原始证据封装，同时必须提供 `--r8-evidence-sha256`。这个 SHA-256 应来自文件外的可信交付或人工复核记录；自行给任意文件计算摘要不能证明其事实真实或已经获批。旧 R7 `ok/passed` 报告、仅含 `admission_passed` 的 Phase 6 汇总，以及单独的 `T-6.6.passed` 均被拒绝，不自动迁移为新证据。

封装顶层必须且只能包含以下五项：

| 字段 | 当前契约 |
| --- | --- |
| `schema_version` | 固定字符串 `r8-admission-evidence/v1` |
| `identity` | `source_sha256`、`config_sha256`、唯一 `strategy`，以及 `runtime` 中的 `exchange/environment/account/market_type`；环境必须为 `live` |
| `scope` | `exchange`、`symbol`、`strategy`、`base_currency`、`max_order_notional`、`max_daily_new_risk`；额度均为有限正数，不接受布尔、NaN 或 Infinity |
| `approval` | `approved=true`、非空 `operator`、带时区的 `approved_at/expires_at`、完整且相同的 `identity/scope`、`phase6_bundle_sha256` |
| `phase6_bundle` | 本文前述原始 signals、paper、生命周期、执行校准、监控、holdout 决定与人工裁决输入；不使用已经计算的总体通过标志替代原始观测 |

`phase6_bundle_sha256` 使用 [canonical_bundle_sha256](../core/gray_release.py) 的规范序列化：键排序、紧凑分隔符、UTF-8、`ensure_ascii=False`、`allow_nan=False` 后计算 SHA-256。封装文件的外部 SHA-256 则直接绑定实际文件字节。解析拒绝重复 JSON 键和非有限数字，包括指数溢出的数字。

`phase6_bundle.admission_approval` 中的 `approved/operator/approved_at` 必须与封装批准一致；`holdout_report` 还须带匹配的 `source_sha256/config_sha256/strategy` 及带时区的 `evaluated_at`。所有观测和裁决时间必须不晚于人工批准时间，批准必须已经生效且尚未过期，不能预填未来 56 日记录。信号中的策略和币对必须匹配本次唯一运行范围。

运行入口从实际受控源码清单计算当前 `source_sha256`，按实际 `config/params.yaml` 字节计算 `config_sha256`，并核对已加载配置与文件一致；策略名来自实际 `routing`，账户和范围来自启动参数。预期身份不从证据文件反读。声明的批准额度必须匹配 `--r8-max-order-notional` / `--r8-max-daily-risk`，当前启动安全额度不得超过批准范围。

门禁会重新执行 `evaluate_phase6`，只采纳重新计算的 `T-6.6` 准入结果；14 日、缺日、单一市场状态、对账缺层、不健康监控、未关闭 P0 或 holdout 拒绝均不能通过。Micro Live 和扩容尚未发生时，整个 Phase 6 的总 `passed` 可以为假，但原始准入前置必须全部通过。现有回滚数据库完整性、账户身份及交易所权限检查继续执行。

相关参数组为：

~~~text
--r8-evidence <已独立复核的原始证据封装.json>
--r8-evidence-sha256 <文件外可信交付记录中的64位小写摘要>
--rollback-snapshot <同一账户身份的有效SQLite快照>
--r8-max-order-notional <明确批准的单笔金额上限>
--r8-max-daily-risk <明确批准的每日新增风险上限>
~~~

每次 buy/short 新风险提交前，门禁再次核对当前源码/配置/策略、环境批准、证据文件摘要及批准有效期；失败发生在已有持久风险预留之前。sell/cover 保护性退出继续走既有守卫，不因批准过期而撤销必要保护。门禁计算和摘要绑定不能代替真实来源认证、人工授权交付或已发生的连续运行；本次正例测试使用明确的离线夹具，没有产生资金运行授权。

任何 unknown order、unknown position、数据质量失败、对账差异或未解释 P0 事件都应立即
停止新增风险。收益良好不能自动扩大资金、标的、交易所、策略、杠杆或无人值守时间。

只读运维看板会自动读取 Phase 6 报告，并显示五个监控维度：

~~~powershell
python -m dashboard --phase6-report reports/phase6/phase6_report.json
~~~
