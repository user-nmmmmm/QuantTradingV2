# Roadmap 第 7 节验收记录（2026-09-20）

本轮实现优先级与完成定义的机器校验。验收范围是任务治理及历史本地证据的显式适配；不改变策略、风险参数或交易运行代码。40项任务继续保留24项已验收、16项开放，正式策略仍为 `paused_revalidation`。

## 交付内容

- [优先级与依赖检查](../scripts/roadmap_priority.py)：拒绝未知编号、重复/未知依赖、依赖环和未满足前置的关闭状态；按依赖与优先级输出工作队列、开放任务及正式冻结/长跑阻断项。
- [完成定义检查](../scripts/verify_roadmap_completion.py)：核对计划、详情当前目录及任务卡、登记与验收索引；强制七项完成要求，验证证据路径、冻结源码成员及SHA-256，拒绝缺失、篡改、错误归属和内部失败却宣称通过的JSON/JUnit。
- [完成定义契约](roadmap_completion_contract.md)与[版本化清单](roadmap_completion_manifest.json)：为24项历史验收逐项绑定原始回执、契约、兼容规则、固定源码/输入、正反例与差异/集成证据、限制及开发流程。旧回执保持原样。
- CI增加结构检查。完整证据校验在保留历史报告的工作区运行；CI结构检查明确表示尚未检查历史产物，不伪装完整验收。

发现并同步了开发详情当前目录中SYS-12/13仍写为“后续扩展”的旧状态，使其与计划、登记及当前复核的“部分实现待验收”一致；原始审计卡片和历史快照继续保留。

实现复核进一步关闭了源码/输入角色替换、源文件变化、回执顶层通过覆盖内部失败、详情任务卡当前状态漂移等误通过路径。源码身份相关的4项最小失败样本保存在[source-binding-before.xml](../reports/roadmap_v3/section7/20260920-completion/source-binding-before.xml)及[原始日志](../reports/roadmap_v3/section7/20260920-completion/source-binding-before.log)，后续修复与集成结果另存，不覆盖失败证据。

## 当前阻断项

| 优先级 | 尚未关闭 |
| --- | --- |
| P0 | SYS-05、SYS-08 |
| P1 | SYS-03、SYS-04、SYS-06、SYS-07、SYS-09、SYS-10、SYS-11、SYS-16、SYS-17、SYS-18 |
| P2 | 无 |
| P3 | SYS-12、SYS-13、SYS-14、SYS-15 |

SYS-05、SYS-04、SYS-03的任务依赖已满足，可以继续完成各自验收；其自身证据缺口仍需关闭。SYS-08的未闭环链为SYS-05→SYS-06→SYS-07→SYS-08。其余开放任务的传递依赖见机器报告。

## 验证与产物

最终专项及相关规则回归 **248 passed，0 failed，0 skipped**，耗时5.58秒。其中本节优先级、完成定义及源码绑定测试216项，既有规则回归32项。Ruff、环境依赖、锁文件及受保护历史归档检查全部通过。保留本次380个受控源码/配置/夹具文件的快照，执行前后无漂移，逐文件快照摘要无差异。

本次源码身份为 `3f04768dfcfeb62728e65b36bb0510e21f8b3df4b4372247d7b62d80f94acebb`。这仅是本节验证运行的可复现身份，不将旧策略候选改称新身份，也不宣称重跑全部历史交易回归或远程CI。

验收产物：

- [完整完成定义报告](../reports/roadmap_v3/section7/20260920-completion/completion_report.json)：40项任务状态及依赖、24项历史验收证据、源码成员检查和仍然开放的任务。
- [验收总回执](../reports/roadmap_v3/section7/20260920-completion/acceptance.json)：契约、配置/输入、结果、差异、迁移、限制和产物摘要。
- [本次测试报告](../reports/roadmap_v3/section7/20260920-completion/tests.xml)与[日志](../reports/roadmap_v3/section7/20260920-completion/tests.log)：248项实际执行结果。
- [源码清单](../reports/roadmap_v3/section7/20260920-completion/source_manifest.json)与[执行前后身份校验](../reports/roadmap_v3/section7/20260920-completion/test_execution.json)：380个文件固定副本和零漂移。
- [质量与结构检查](../reports/roadmap_v3/section7/20260920-completion/quality.json)、[环境及归档检查](../reports/roadmap_v3/section7/20260920-completion/environment_checks.json)。

## 兼容与限制

原有任务数、优先级、依赖、验收状态及研究/运行结论不变。新清单为历史格式提供明确角色和引用，不改写原始源码冻结、原回执或旧研究fail结论。SYS-01/SYS-02的工程验收与运行pending可以同时成立；纯文档任务无需无关交易测试。

FIX-14/18/19/20的原始中间源码清单无法全部从现有冻结目录恢复，清单中明确改用后续freeze-04同版本源码、夹具和完整集成测试证明最终工程身份，并保留原始专项证据和这一限制。没有生成或猜测缺失的历史字节。

完整校验的pass只证明本节治理检查和被引用历史证据有效。`current_source_revalidated=false`、`live_admission=false`、研究和运行 `not_revalidated` 分别保留。没有取得新的未来独立样本、真实账户完整事实、56日连续运行或人工资金批准；本次治理交付不能消除这些阻断项。
