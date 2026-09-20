# Roadmap 第 7 节：优先级与完成定义契约 v1

> 公开发布视图：保留任务编号、状态、依赖及技术契约；私人邮件正文、逐条审查摘录和邮箱映射未发布。原始本地登记仍是权威资料。`reports/`、`outputs/`、`tmp/`、`docs/archive/` 及邮件审计文件均为仅本地引用，不随源码发布；公开 CI 的 structure-only 结果不代表这些历史证据已验证。

适用范围是任务治理、历史本地验收证据和后续关闭任务的检查。权威状态仍由[统一 Roadmap](unified_roadmap.md)、[开发计划](development_plan.md)、[任务登记表](development_task_registry.json)共同维护。契约版本为 `roadmap-completion/v1`；[完成证据清单](roadmap_completion_manifest.json)为不同历史回执提供显式适配，原始回执和研究结论不改写。

## 优先级与依赖

| 优先级 | 可执行规则 | 边界 |
| --- | --- | --- |
| P0 | 未关闭项进入正式冻结和长跑阻断清单；本地关闭必须具备正例、反例及故障证据 | 阻止涉及该能力的运行放行，仍允许无冲突的独立开发 |
| P1 | 未关闭项进入正式冻结和长跑阻断清单 | 样本不足、前视、账务失真和恢复分叉不能靠工程代码存在视为关闭 |
| P2 | 未验收项阻止依赖它的任务关闭 | 不要求无关交易回归，验证范围应符合风险 |
| P3 | 必须进入 BX 独立支线，关闭时必须满足全部前置 | 本地隔离能力和整体任务完成分开记录 |

[优先级检查](../scripts/roadmap_priority.py)拒绝未知状态、重复编号、未知/重复/自身依赖、依赖环，以及“前置未验收但下游已验收”。输出拓扑顺序、按优先级排列的依赖已满足队列、传递阻断项和未关闭的 P0/P1。`ready_for_acceptance` 只表示前置已满足，仍需任务自身的全部证据。所有输出保留 `live_authorization=false`。

当前整体主线必须同时满足全部相关能力，故整体冻结/长跑报告列出所有未关闭 P0/P1。若将来评估范围更小的运行，应使用其经过批准的能力依赖范围重新审查，不能通过删除当前登记项来缩小阻断清单。

## 七项完成定义

| 要求 | 完成清单字段 | 检查方式 |
| --- | --- | --- |
| 明确契约 | `contract`、`accepted_scope` | 契约路径、SHA-256、适用范围；带 anchor 时核对锚点 |
| 与风险相称的验证 | `checks` | 工程任务要求 positive、negative、reconciliation、integration；P0 另需 fault；每项必须 pass、有理由及具体文件 |
| 迁移兼容 | `compatibility` | 非空迁移说明，与任务登记中的兼容要求对应 |
| 固定输入及源码身份 | `source_identity`、`source_snapshot_root`、`fixed_inputs` | 核验源码清单中每个冻结文件的实际字节，并要求固定输入绑定同一清单中的夹具；文档任务核验归档文件及输入来源；原始测试身份与后续集成身份分开 |
| 差异或对账结果 | `checks[reconciliation]` | 指向真实差异、事实桥接或明确覆盖相应契约的测试产物 |
| 未建模项和限制 | `limitations` | 显式保留本地/真实运行、历史/未来样本及其他未建模边界 |
| 可定位验收产物 | `legacy_receipt`、各证据引用 | 索引、登记和完成清单覆盖一致；实际文件存在且摘要匹配 |

纯文档任务只要求 links、ids、coverage、hashes 四类检查，不强加交易行为测试。`workflow` 的五个字段分别记录现状确认、失败样本或文档结构检查、实现、专项验收和集成证据。已存在的旧任务以真实历史记录适配；无法证明的环节必须记录缺口，不能倒造开发时间线。

JUnit 文件必须有真实测试案例，不能含失败或错误，也不能全部跳过。少量确实需要外部环境的跳过项必须逐个登记 `allowed_skips`（`classname::name`）和 `skip_reason`；允许跳过不代表该案例通过。业务反例应是“拒绝不合法输入”的通过测试，不能拿一个失败测试日志作为通过证据。

被引用JSON中的显式结果同样接受检查：历史工程回执必须有通过的检查或成功进程退出码；文档审计不能有失败状态或非空错误，质量检查不能有非零退出码。外层清单写pass不能覆盖产物内部的失败。差异样本内部的修复前失败事实保留为对比，不被误当作修复后测试失败。

## 历史兼容与状态含义

历史回执存在 DOC 归档清单、进程退出码、旧的描述性状态等多种格式。v1 清单绑定原回执 SHA-256，并明确证据角色；既有文件不被重写为统一的新格式。DOC-01 使用归档和链接校验；SYS-01、SYS-02 仅保留已证明的工程关闭边界，运行 pending 不会被提升为 pass。

FIX-14、FIX-18的原始中间清单含未纳入冻结目录的运行文档，FIX-19、FIX-20存在未保留的中间源码字节。这四项明确使用后续freeze-04的固定源码、固定夹具和同版本完整集成结果建立可恢复的最终工程验收身份；原始专项清单继续作为历史证据，缺失的中间身份在各项限制中保留，不宣称其可完整恢复。

`historical_evidence_verified` 表示所引用的历史输入和产物可定位且通过本契约校验。`current_source_revalidated=false` 表示没有据此宣称当前整个工作区重新通过所有历史行为验收。源码变更后的正式冻结仍要使用既有冻结及受影响行为测试流程。当前检查不会打开未成熟的前瞻样本，不改策略参数或账户状态。

开放任务可以拥有 `local_contract_status=pass`，但其整体状态继续开放。检查会保留原始研究 fail，拒绝将没有通过证据的研究/运行字段改成 pass。研究、运行与工程始终分别判断。

## 执行与输出

在仓库根目录执行完整本地检查（需保留历史 reports 产物）：

```powershell
.venv/Scripts/python.exe scripts/verify_roadmap_completion.py --output reports/roadmap_v3/section7/<new-run-id>/completion_report.json
```

不提供 `--output` 时只读取和报告结果。输出路径已存在时拒绝覆盖，失败返回非零退出码。完整结果的 `pass` 仅表示第 7 节治理及已声明历史本地证据检查通过；输出始终为 `live_admission=false`。

在不含本地历史报告的干净检出或 CI 中执行：

```powershell
python scripts/verify_roadmap_completion.py --structure-only
python scripts/run_portable_tests.py -q tests/test_roadmap_priority.py tests/test_roadmap_completion.py
```

`--structure-only` 验证任务图、计划/详情目录/登记一致性、全部清单字段与引用格式，明确返回 `structure_verified_evidence_not_checked`。该结果不能代替完整文件/哈希校验或新的任务验收。单元测试使用固定合成证据验证拒绝路径，不依赖本地忽略的历史产物。

关闭新任务时应完成全部自身证据，新增或更新其版本化完成清单，同步计划、详情当前目录、登记及验收索引，然后运行完整检查。检查脚本本身不会修改任务状态。每次新证据使用独立运行目录，保留失败日志和旧回执。
