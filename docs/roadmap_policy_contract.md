# 旧计划冲突统一口径：可执行契约

生效日期：2026-09-20。承接 [统一 Roadmap 第 5 节](unified_roadmap.md#5-旧计划冲突的统一口径)。本文件记录九条规则的本地实现与验收；不改变批准的风险阈值、研究候选身份或真实资金状态。正式策略仍为 `paused_revalidation`。

机器登记入口是 [verify_roadmap_policy.py](../scripts/verify_roadmap_policy.py) 的 `POLICIES`。每条记录同时保存原文档路径、旧 ID、实际读取的历史快照路径、运行代码和具体回归节点。原文档路径与旧 ID 组成复合键；同名 `BT-01` 不同来源保持为不同需求。历史快照继续保留原文，活动领域文档中的冲突语句已按下表修正。

| 规则 | 采用的行为与修订 | 当前代码与验收边界 |
| --- | --- | --- |
| POL-01 研究选择隔离 | train/validation 决定候选；最终 holdout 冻结后单次独立评价。已见区间仅回顾性研究。活动 S1-3、D5 删除“按最终 OOS 排序”验收 | `analysis/optimize.py`、`walk_forward.py`、`research_validation.py`；改变最终分区不能改变排名；holdout 拒绝调参用途及重复打开。前瞻协议与后继候选工具记录在源码指纹中；前瞻准入另由对应专项验收负责 |
| POL-02 连续运行下限 | 2–4 周仅中间检查；生产下限固定为 56 个连续自然日和两种市场状态。更长的冻结协议、更多状态要求继续生效 | `core/admission_gates.py`：输入即使请求 1/14/28/55 日或 1 状态，也不能降低 56/2；缺日期、断档、重复、倒序、不完整证据失败。`review_admission` 不再仅信任 `passed=true` 和版本号，还复核时长、状态、观察数及连续性摘要 |
| POL-03 健康恢复 | 延续 9月14日分级恢复、已消费样本边界、迁移规则，人工锁保持 | `core/strategy_health.py`；复用人工锁、成功试运行消费边界及旧检查点迁移回归。本轮不调恢复阈值 |
| POL-04 不可变批准风险 | 已批准订单只读原风险、参考价、初始止损；数量截断同比缩小预算，恢复或权益增长不扩大旧预算 | `core/entry_risk.py`、风险审批和成交投影；覆盖缩量、跳空、非法显式预算、精度截断、重启恢复 |
| POL-05 真实成交与流动性 | 保留实际跳空和成本，所有执行 pass 共用每 bar 参与率；未完成目标持续减仓，不能创造流动性 | broker 撮合/成交服务、回测引擎、实盘风险动作；同 bar 二次执行不能获得新额度，部分减仓跨重启保留目标与剩余保护 |
| POL-06 指标可审计状态 | 不可用为 null+状态+原因，区分输入错误与样本不足；旧四态只经版本适配 | `core/metric_result.py` 与报告序列化；容器 `quanttrading.metrics/v1`、状态 `metric-result/v2`。非法事实不能显示有效指标；缺失流与空流分别为 `not_modeled`、`insufficient_data` |
| POL-07 账本事实边界 | 权威来自持久化订单/成交、lot 与可重建投影，不因某个离线研究模块存在就完成交易链路 | `core/events/store.py`、lot/成交投影及 `research/audit/ledger.py` 中的共享投影类；不可改写、幂等、重建、反向成交和部分 lot 事实守恒均有测试。代码命名空间不替代链路证据；真实账户完整对账仍需运行资料 |
| POL-08 基准身份 | 再平衡与买入持有使用不同 `benchmark_id`。9月14日冻结基准保持 BTC/ETH 各 50%，各自首个有效 open 买入，上市前该份额留现金，不再平衡 | `core/benchmarks.py`；三类基准身份互异、首 open/延迟上市可手算、再平衡真实换手收费，缺来源失败。不能看结果后换基准 |
| POL-09 原文路径+旧 ID | 相同编号不同文档不能合并；文件缺失、旧 ID 缺失、测试节点缺失和同规则重复来源均失败 | `trace_key`、登记验证器和哈希校验；规范化斜线但不丢路径，拒绝绝对路径/越界。两份旧计划的 `BT-01` 有不同键 |

POL-02 的 `minimum_paper_days` / `minimum_market_regimes` 是冻结协议的要求输入：下限只能加强，不能放松。非法值（小数、布尔、非正整数）拒绝，防止隐式整数截断缩短协议。直接调用 `review_admission` 时，应同时传入已冻结的较长时长/状态要求；完整证据包入口 `evaluate_phase6` 已自动透传。日期按 UTC 自然日和逐日连续覆盖判断，首尾日期不能代替中间观测。

运行下列本地检查，不连接交易所，不打开真实最终样本，也不发出资金放行：

```powershell
.venv/Scripts/python.exe scripts/verify_roadmap_policy.py --run-tests --output reports/roadmap_v3/POLICY/20260920-section56-release
```

输出 [policy_report.json](../reports/roadmap_v3/POLICY/20260920-section56-release/policy_report.json)、[policy_tests.log](../reports/roadmap_v3/POLICY/20260920-section56-release/policy_tests.log) 和 [policy_tests.xml](../reports/roadmap_v3/POLICY/20260920-section56-release/policy_tests.xml)。不传 `--run-tests` 只检验登记、来源和测试节点，状态为 `references_verified_tests_not_run`，不能记为本地行为验收通过。脚本复用 Windows 可用的隔离临时目录测试入口。

追加前瞻协议/新身份反例后，当前九规则专项 **70 passed，0 skipped/failed**；此前完整新增规则测试、现有 Phase6 及恢复专项同为 **70 passed**（测试集合不同，不相加）。后者保留原有 pandas 警告，不影响通过。正式报告还要求所有登记节点都实际出现在 JUnit、无跳过，并在执行前后复核登记输入的 SHA-256；执行期间源码漂移即失败。哈希覆盖的是登记的局部证据输入，不代替整工作区冻结。后续这些输入变化后须重新执行，旧测试报告不能直接继承。

所有输出固定 `live_admission=false`、`external_evidence=not_evaluated`。这些通过结果证明规则实现和本地反例检查完成；没有宣称已经取得 56 日真实连续运行、两种市场状态、独立未来样本盈利性、人工资金批准或真实账户逐笔对账。真实准入仍遵守 Roadmap R3/R6/R7/R8 与冻结研究协议。
