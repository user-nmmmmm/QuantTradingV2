# Roadmap 第 5、6 节执行记录（2026-09-20）

本轮将旧计划冲突落实为可执行检查，并补齐选币与目标仓位的隔离工程能力。第 5 节使用九条统一规则逐条验收；第 6 节分别记录工程交付、研究证据和运行准入。**第 6 节整体尚未验收，正式策略仍为 `paused_revalidation`**。已知真实数据、账户及独立研究缺口不能用合成测试关闭。

## 第 5 节：统一口径

[规则契约](roadmap_policy_contract.md)登记 POL-01–POL-09 的原文档路径、旧编号、当前代码和具体回归节点。[验证入口](../scripts/verify_roadmap_policy.py)检查引用、复合追溯键和源文件摘要，并执行行为回归；只检查引用时明确记录 `references_verified_tests_not_run`。

本次发现并修复的行为缺口：

- 连续运行参数曾能把 56 天、两种市场状态下限调低。现在下限不可降低，更长冻结要求继续生效；直接提交的 paper 报告也必须有完整连续性和覆盖事实，不能仅凭 `passed=true` 放行。
- 前瞻样本开启曾直接信任 JSON 内的成熟日期。现在先校验原登记哈希、身份和时间边界；标签完成必须是布尔 `true`，字符串 `"false"` 不算有效证据。
- 活动领域文档中按最终 OOS 表现排序、较短 paper 观察等冲突文字已改为统一规则；受保护历史快照保持原样。

九条规则的机器回执见 [policy_report.json](../reports/roadmap_v3/POLICY/20260920-section56-release/policy_report.json)。工程检查不构成真实连续运行证据。

## 第 6 节：支线实际交付

| 支线 | 本轮可核验交付 | 仍未完成的边界 |
| --- | --- | --- |
| S0/S1、SR0–SR5 | 复核既有生命周期、增量事实、真实成本、train/validation 隔离和报告回归；原失败结论保留 | SYS-04 的真实历史成员/借贷资格/独立源异常确认；SYS-11 未见样本裁决 |
| S2 / SYS-12 | PIT 生效时间与可得时间、来源内容哈希、rank/zscore、TopN 缓冲、显式 symbols、目标权重、现金/费用/换手/参与率/待成交约束、持续退市退出提案；真实 Broker 部分成交对账 | 全历史退市数据真实性、因子 Top/Bottom 独立样本分离、正式风险路由接线和前置任务整体验收 |
| S3 / SYS-13、PM1–PM4 | 默认关闭的波动率定仓、集中度与相关簇约束、原批准风险限制、冻结目标、分批决策与检查点恢复；保护单绑定权威仓位身份，覆盖未观测 flat 的同向重开、取消后重启、UNKNOWN 与遗留单迁移 | 完整协方差风险贡献研究、目标仓位正式接线、真实账户生命周期和前置系统整体验收 |
| 信号 P0–P3 / SYS-10、11 | 原交付回执与正式账户隔离证据保留；新候选独立冻结，原协议哈希和研究失败不改写 | 样本支持与动态模型有效性；全弃权、无交易、等权回退或固定 25% 仓位均不能证明有效 |
| S4 / SYS-14 | 既有 spot/spot_margin/perpetual 账户、资金费率、保证金、强平和容量基础回归；逐项记录候选评估前置状态 | SYS-04/05/07/08 尚未整体通过，不启动具体合约候选评估；真实盘口/资金费率/借贷资格及实际运行证据不足 |

S2/S3 的具体接口、单位和迁移规则见[选币契约](s2_selection_contract.md)与[仓位能力契约](s3_position_capabilities.md)。它们默认不接正式路由，不改 `config/params.yaml`。SYS-12/13 从“后续扩展”更新为“部分实现待验收”，没有把独立纯决策模块标成完整生产能力。

PM1 的共享保护逻辑已接入现有回测和 live 调用链：从 open lot 核对实际数量并获取 position ID；新旧身份无交集时不继承旧棘轮；取消确认后重新核对仓位；UNKNOWN 不生成替代单。取消已确认、替换尚未提交时发生重启，从同仓位的持久订单事实恢复已确认止损，避免放松；待退出订单占用全部库存不再被当作实际 flat。缺失归属的旧保护单不能静默降低止损。这些是本地故障验收，未替代实际交易所运行。

## 可复现的隔离例子

从仓库根目录运行：

```powershell
.venv\Scripts\python.exe scripts/run_strategy_branch_example.py --output reports/roadmap_v3/section56/example-new.json
```

该入口构造明确标记为 synthetic 的 PIT 成员与因子，经过选择、波动率缩量、费用/现金/参与率约束，再交给已有真实回测 Broker。两根后续 bar 各成交 1 单位，尚未成交的目标保留；输出订单、成交、费用、现金、仓位和对账差异。输出文件拒绝覆盖。[本轮实例](../reports/roadmap_v3/section56/20260920-implementation/branch_example.json)只证明工程语义，不是收益或容量研究。

## 新候选与观察安排

原协议保持 **[2026-10-20, 2027-04-18)**、最早成熟日 **2027-05-08**，未开启也未修改。新修复身份单独保存源码快照、逐文件哈希、原配置与原实验登记引用；按同一 30 天隔离、180 天观察、20 天标签成熟规则从实际新登记时间重新安排，不继承旧候选已经经过的天数。

2026-09-20 登记的新身份观察窗为 **[2026-10-21, 2027-04-19)**，最早成熟日 **2027-05-09**。见[新登记回执](../reports/roadmap_v3/SYS-11/20260920-section56-successor-02/acceptance.json)和[源码清单](../reports/roadmap_v3/SYS-11/20260920-section56-successor-02/source_manifest.json)。登记仅建立可复现身份和未来观察边界；不会自动开启研究、解除策略锁或批准资金。

## 验证与证据

最终全套为 **1,717 passed、1 skipped、46 subtests passed**，47 条警告保留，覆盖率 **89.26%**，耗时 272.31 秒。跳过项是需显式开关和真实连接的 sandbox 测试。九规则专项 **70 passed**；环境、依赖锁、受保护历史归档、关键运行接口类型检查和 Ruff 均通过。源码身份为 `1174212be1fe952929bf83138c50285394a8213ca72b2c11b70f033da3774824`。

三进程的固定事实和解析配置逐字节一致；相对前序 freeze04，固定样本成交、现金/权益、lot、健康状态和配置相同，仅两条保护单意图增加仓位归属。首轮全套出现一个旧测试替身缺少历史订单接口的失败，补齐模拟订单事实后再跑全套通过；首轮冻结和失败日志保留。详见[基线差异](../reports/roadmap_v3/section56/20260920-implementation/baseline_migration.json)及[最终日志](../reports/roadmap_v3/section56/20260920-implementation/full-tests-final.log)。

本轮[总回执](../reports/roadmap_v3/section56/20260920-implementation/acceptance.json)记录源文件、配置、固定输入、专项与集成测试摘要、限制及产物索引；[全套回归](../reports/roadmap_v3/section56/20260920-implementation/full-tests-final.xml)、[质量检查](../reports/roadmap_v3/section56/20260920-implementation/quality.json)、[逐项验收索引](../reports/roadmap_v3/acceptance_index.json)共同用于复核。此前失败日志保留，不覆盖旧研究或旧源码冻结。

合约候选的具体阻断项见 [S4 readiness](../reports/roadmap_v3/SYS-14/20260920-section56/readiness.json)。市场数据真实性、56 天以上连续运行、远程 CI 与真实交易所/告警送达均不在本地合成验收中宣称通过。

交付校验另行核对40项任务在计划/登记/索引中的状态、所有索引回执哈希、本文档链的本地链接、374个源码文件与冻结副本、九规则证据摘要，以及新旧前瞻协议均未开启。见[交付校验](../reports/roadmap_v3/section56/20260920-implementation/artifact_validation.json)。
