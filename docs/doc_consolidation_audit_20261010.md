# 文档完成度与合并审计(2026-10-10)

范围:`git ls-files '*.md'` 共 160 个,其中 `docs/archive/` 52 个(已是归档,不在本次判断内),其余 108 个。
方法:读取各文档自述状态;统计被其他 MD/代码/测试/JSON 引用的次数;核对验证脚本与测试是否把该文档作为输入;抽查代码(见第 5 节)。**没有重新运行测试**,"完成"指文档自述 + 抽查一致,不等于重新验收。
本文只给建议,**没有删除或移动任何文件**。

## 0. 先决约束:不能直接删

下列机制把 MD 当作输入,直接删除/改名会使校验失败,必须同步修改并重跑:

| 机制 | 锁定的文档 |
|---|---|
| `docs/development_task_registry.json`(41 个任务引用 28 个 MD) | `development_plan/details`、`unified_roadmap`、`roadmap_completion_contract`、`roadmap_traceability`、`followup_completion_20260920`、`phase3/4/5/6`、`p0/p1/p23_*`、`protective_stop/strategy_health/portfolio_risk` 契约、`glossary`、`authoritative_ledger`、`canonical_trading_events`、`automated_backtest_plan`、`position_management_plan`、`live_trading_remediation_plan`、`current_strategy_remediation_roadmap`、`backtest_metrics_detailed_development_plan`、`strategy_remediation_contract_20260914` |
| `scripts/verify_roadmap_policy.py`(按"路径+旧编号"校验标题与关键词) | `strategy_development_roadmap`、`backtest_optimization_roadmap`、`phase6_operations`、`backtest_metrics_detailed_development_plan`、`authoritative_ledger`、`live_trading_remediation_plan`、`strategy_remediation_contract_20260914` |
| `scripts/verify_roadmap_completion.py` 的输入摘要 | `development_plan`、`development_details`、`unified_roadmap`、`roadmap_completion_contract` |
| `docs/archive/2026-09-roadmap-rebaseline/manifest.json` 与邮件 JSON(路径+哈希) | `docs/baseline/phase0/*`、`r6_operations`、`r7_sandbox_runbook`、`codex_mail_roadmap_audit`、`batch0_fixed_baseline` 等 |
| `docs/file_retention_manifest.json` | `reports/strategy_security_review_20260908.md` |
| `docs/research/paper_remaining_acceptance_20261004.json` | `paper_remaining_tasks_20261004.md` |

另:`file_retention.md` 规定历史交付证据不得仅因体积删除。因此**"可删除"的文档限于:无上述引用、内容已被代码/新文档取代**。其余只能"合并/归档",并同步更新登记表与校验路径。

## 1. 可删除或归档(已完成、无机器引用)

引用数 = 其他 MD 引用次数(含导航页)。建议统一移入 `docs/archive/2026-10-doc-consolidation/`(保留历史可追溯),而不是物理删除;若确认 Git 历史足够,可直接删除标"可直接删"的项。

| 文档 | 状态判断 | 依据 | 建议 |
|---|---|---|---|
| `research/smart_capital_rollback_20261004.md` | 已完成的一次性回滚说明 | 0 引用;结果已记入 README/基线 | 可直接删,事实保留在 Git 提交 `7f76023` 与 README |
| `research/matplotlib_style_20261004.md` | 已实现(`backtest/plot_style.py`) | 0 引用 | 3 行用法并入 `modules/backtest.md` 后删除 |
| `backtest_event_performance_20260924.md`、`backtest_kline_performance_20260926.md` | 已完成的优化记录 | 0 引用 | 并入下文"回测性能"合并文档后删除 |
| `technical_report_6_5_year_inactivity_and_code_review.md`(548 行) | 2026-08-31 只读诊断;所述"组合冻结/人工锁定"后续由 P0 恢复系列修复 | 0 引用 | 归档(与 `archive/2026-08-technical-reviews/` 同类) |
| `strategy_source_analysis.md`(602 行) | 2026-08-31 v1.0 快照 | 0 引用;内容与 `modules/strategies.md` 重叠 | 归档;现状以 `modules/strategies.md` 为准 |
| `research/deep_recovery_backtest_20260909.md`、`research/health_recovery_60symbols_20260906.md`、`research/p0_attribution_recovery_20260906.md`、`research/p0_automatic_recovery_20260909.md` | 已完成的研究运行,结论均为"工程通过、研究不准入" | 0–2 引用 | 合并为一份 `research/p0_recovery_history.md`,保留结论表后归档原文 |
| `missing_capabilities_acceptance.md`(28 行) | M-01–M-20 全部标已实现 | 仅 2 处导航引用 | 并入 `development_details.md` 验收索引后删除 |
| `phase5/README.md`(9 行) | 只是指针 + FIX-08 修正说明 | 被登记表引用 | 说明并入 `development_details.md` 后保留空壳或更新登记表 |

## 2. 可以合并(内容仍有效,但文件过碎)

| 合并目标 | 来源文件 | 合并理由 |
|---|---|---|
| **`ml_selection.md`**(现状 + 操作)及 `ml_selection_history.md`(V1 复核 + 本轮执行/诊断/稳健性/冻结回测) | `ml_selection_roadmap`、`ml_selection_engineering_roadmap`、`ml_selection_v1_review`、`ml_selection_next_roadmap`、`ml_selection_next_execution`、`backtest_coin_selector`、`research/ml_selection_frozen_backtest`、`research/ml_selection_next_diagnostics`、`research/ml_selection_next_robustness`(共 9 个,约 1500 行) | 同一主题,互相只做"最新入口"跳转;`next_roadmap` 里一半是 source3–6 的运行流水账 |
| `ml_selection_learning_checklist`(184 行) | 单独保留或移入 `docs/learning/` | 是学习清单,不是状态文档 |
| **`paper_applications.md`**(研究)+ 保留 `paper_validation_contract` | `paper_application_list/roadmap/execution`、`paper_roadmap_implementation`、`paper_engineering_completion`、`paper_continuation`、`paper_remaining_tasks`、`paper_return_followup`(8 个) | 每篇开头都在指向下一篇"最新入口",是同一条时间线;`paper_remaining_tasks` 被 JSON 引用,需同步改路径 |
| **`backtest_performance.md`** | `backtest_performance_20260922`、`..._memory_20260922`、`backtest_event_performance_20260924`、`backtest_kline_performance_20260926` | 同一优化线的 4 个日期快照 |
| **`architecture_performance.md`** | `architecture_performance_roadmap/validation/followup_20261002`(3 个) | AP01–AP13 同一张表分三处维护 |
| **`signal_meta_layer.md`** | `p0_signal_observation`、`p1_signal_meta_layer`、`p23_signal_meta_layer` | P0→P3 同一元层的三个阶段;被登记表引用,需改路径 |
| **`trend_portfolio.md`** | `trend_portfolio_v2`、`trend_portfolio_v3` | V3 继承 V2;与 V4 提议的合并策略类一致 |
| **`roadmap_acceptance_log.md`** | `r_series_acceptance_20260920`、`section56_acceptance_20260920`、`section7_acceptance_20260920`、`followup_completion_20260920`、`r_series_metrics_contract_20260920` | 同日连续验收记录;`followup_completion` 被登记表锁定 |
| **`live_safety.md`**(或并入 `modules/live_trading.md`) | `g1_live_safety`、`g2_order_lifecycle`、`protective_stop_contract`(保留为契约)、`revalidation_migration_recovery_20260908` | G1/G2 是现行为说明;迁移说明属运维,可入 `account_fact_operations.md` |
| **`baselines.md`** | `baseline/phase0/{README,baseline_lock,freeze_notice}`、`baselines/{batch0_fixed_baseline,main_20260906/README}` | 目录 `baseline`/`baselines` 单复数并存;被归档清单按路径+哈希锁定,需同步 |
| **`dashboard/README.md` + `FRONTEND.md` + `DESIGN.md`** | 三者(540 行) | 同一前端的使用/工程/设计 |
| **旧路线图族(归档,不继续排期)** | `strategy_development_roadmap`、`backtest_optimization_roadmap`、`current_strategy_remediation_roadmap`(673)、`live_trading_remediation_plan`(472)、`backtest_metrics_detailed_development_plan`(459)、`codex_mail_roadmap_audit`(646)、`automated_backtest_plan`、`position_management_plan` | 统一 Roadmap 已声明其为"保留追溯、不独立排期"。迁入 `archive/` 的前提是改 `verify_roadmap_policy.py` 与登记表路径并重跑验收——属独立治理任务,不要与文档清理混在一起 |

按以上合并,108 个文档可降到约 55–60 个,且不丢失"当前有效 / 历史证据"的区分。

## 3. 保持不动(现行有效)

`README.md`、`docs/README.md`、`modules/*`(core/backtest/live_trading/router/strategies/analysis…)、`backtest_assumptions`、`glossary`、`deployment`、`dependency_management`、`file_retention`、`account_fact_operations`、`r6_operations`、`r7_sandbox_runbook`、`canonical_trading_events`、`authoritative_ledger`、`strategy_health_contract`、`portfolio_risk_contract`、`s2_selection_contract`、`s3_position_capabilities`、`research/strategy_remediation_contract_20260914`、`research/approved_entry_risk_contract_20260912`、`research/paper_validation_contract_20261003`、`unified_roadmap`、`development_plan`、`development_details`(1932 行,可按任务拆分但先不动)、`roadmap_*contract`、`roadmap_traceability`、`strategy_health_lock_investigation`(仍被 8 处引用)、`reports/strategy_security_review_20260908.md`(受保留清单保护)、`tests/fixtures/backtest/README.md`、`scripts/README.md`。
`.kimi-code/skills/quant-backtest/SKILL.md` 是另一个工具的技能文件,0 引用,是否保留取决于你是否还用 Kimi,我无法判断。

## 4. 仍未完成的内容(这些文档必须保留并持续维护)

| 事项 | 来源 | 核对结果 |
|---|---|---|
| 登记表 17 项未验收:SYS-03–10、16、19(部分实现待验收)、SYS-11/17/18(待验证)、SYS-12/13(P3 部分实现)、SYS-14/15(后续扩展) | `development_task_registry.json` | 24/41 已验收,与 `followup_completion` 自述一致 |
| 正式策略 `paused_revalidation`;R3 起准入未过,R7 连续运行、R8 灰度未放行;56 日连续观察 | `unified_roadmap` | 文档自述,无连续运行证据 |
| `core → research` 反向依赖(A3) | `architecture_review` | **已核对仍存在**:`core/events/codec.py:29` |
| mixin 拆包待改组合(A4) | `architecture_review` | **已核对仍存在**:Broker/LiveBroker/RiskManager/LiveTradingEngine |
| 数据清单只覆盖 2 个标的,而已跟踪 CSV 为 31 个文件(30 个 CSV + 清单) | `engineering_structure_roadmap`、`file_retention` | **已核对** |
| CI:mypy 仅 3 个文件、ruff 规则 `E9/F63/F7/F82` | `engineering_structure_roadmap` | **已核对** `tests.yml`、`pyproject.toml` |
| AP12 真实订单计时(无沙盒凭证);AP10 WebSocket 未接入实际输入 | `architecture_performance_*` | 文档自述 |
| ML 选币:`formal_admission=false`、已验证前向决策日期 0、成熟标签 0、独立 final 未打开 | `ml_selection_next_roadmap` | 文档自述 |
| 论文应用:真实成交 0(305 张拒单未提交),执行成本校准样本不足,历史成员未证明 | `paper_remaining_tasks` | 文档自述 |
| 前向观察窗 [2026-10-21, 2027-04-19),最早成熟日 2027-05-09 | `unified_roadmap` | 文档自述,尚未开始 |

## 5. 建议执行顺序

1. **第一批(零风险)**:第 1 节中无引用的 7 个文档归档/删除;同步修 `docs/README.md` 导航链接(其中有对 `paper_*`、`backtest_*` 的直接链接)。
2. **第二批(只动无锁定文档)**:ML 9→2、论文 8→1、回测性能 4→1、架构性能 3→1、`trend_portfolio` 2→1。每步用 `grep` 检查入链并修复,再跑 `tests/test_roadmap_*`、`scripts/check_repository_hygiene.py`。
3. **第三批(带锁定,需改登记表/校验路径并重跑验收)**:`signal_meta_layer`、`roadmap_acceptance_log`、`baselines`、旧路线图族归档。独立成一个 PR,附 `verify_roadmap_completion.py` / `verify_roadmap_policy.py` 通过记录。
4. 每个新合并文档开头写明"合并自 …(原路径,Git 历史可查)",避免追溯链断裂。
5. 未来文档规则:一个主题一个文件,新增进展追加"日期节"而不是新开 `*_YYYYMMDD.md`——这是目前碎片化的根因。

## 局限

- "完成"取自文档自述,仅对 `core/events/codec.py`、mixin、数据清单、CI 配置做了代码核对;没有逐个核对 108 个文档与代码是否一致,也没有运行测试或验证脚本。
- 引用统计按文件名匹配,`README.md` 等同名文件的计数会偏高(已在判断时剔除)。
- 未打开各合并来源的全文,合并时需人工确认没有只存在于某一份里的结论。

## 6. 执行记录

### 第一批:无锁定文档合并(本 PR)

合并(各源文件原样并入,仅调整标题层级与相对链接,原路径以注释标明,Git 历史可查):`research/p0_recovery_history.md`(4)、`ml_selection.md`(4)、`ml_selection_history.md`(5)、`research/paper_applications.md`(7)、`backtest_performance.md`(4)、`architecture_performance.md`(3)、`trend_portfolio.md`(2);`dashboard/README.md` 并入 `FRONTEND.md`、`DESIGN.md`;`account_fact_operations.md` 并入账户迁移说明;`modules/backtest.md` 并入 Matplotlib 样式说明。
归档到 `docs/archive/2026-10-doc-consolidation/`:`technical_report_6_5_year…`、`strategy_source_analysis`、`smart_capital_rollback`(README 未记录其复现命令,故归档而非删除)。
删除:`.kimi-code/skills/quant-backtest/SKILL.md`(用户确认不再使用)。

有意保留:`paper_remaining_tasks_20261004.md`(被验收 JSON 按 SHA-256 锁定)、`baseline/phase0`、`baselines/`(代码与归档清单依赖)、`phase5/README.md`、`strategy_health_lock_investigation.md`。源码注释与冻结协议 JSON 中的旧路径未改(会改变源码/协议哈希身份)。`docs/archive/2026-08*`、`2026-09*` 快照未改。

第二批(被登记表/校验脚本锁定的合并、旧路线图归档与校验路径更新)另提 PR。

### 第二批:被锁定文档合并、旧路线图归档与校验路径更新

合并:`signal_meta_layer.md`(`p0_signal_observation`、`p1_signal_meta_layer`、`p23_signal_meta_layer`)、`roadmap_acceptance_log.md`(R 系列、第 5/6/7 节、`followup_completion`、`r_series_metrics_contract`、`missing_capabilities_acceptance`)、`live_safety.md`(G1、G2)。
归档到 `docs/archive/2026-10-doc-consolidation/`:`strategy_development_roadmap`、`backtest_optimization_roadmap`、`current_strategy_remediation_roadmap`、`live_trading_remediation_plan`、`backtest_metrics_detailed_development_plan`、`codex_mail_roadmap_audit_20260920`、`automated_backtest_plan`、`position_management_plan`。
路径同步:`scripts/verify_roadmap_policy.py`(规则中的"路径+旧编号"键)、`docs/development_task_registry.json`、`roadmap_traceability.md`、`unified_roadmap.md`、`development_*.md`、各契约文档及 `tests/test_sr*.py` 中读取这些文档的路径。
未改:`tests/test_roadmap_policy.py`(其中路径只是 `trace_key` 的字符串样例)、源码注释与冻结协议 JSON 中的旧路径(会改变哈希身份)、2026-08/09 归档快照。
注意:任务登记表内容已变,若在含完整 `reports/` 的环境运行 `verify_roadmap_completion.py` 完整证据模式,验收索引中记录的登记表摘要需要重新生成。
