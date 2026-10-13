# 基础设施路线图（2026-10 起）

> 核对日期：2026-10-14，依据 `main` 的 `3203836`（#82）及实测数据。第 4 节末尾的"当前状态"、第 5 节"接下来的顺序"和第 9 节"交接说明"记录了这一天的进度、未决问题和本轮得到的教训。本文是**工程路线图**：排列基础设施与结构整理的顺序、依赖和退出条件。它不维护项目阶段（R0–R8）、任务状态或实盘准入，这些仍以[统一 Roadmap](unified_roadmap.md)为准；它也不改变任何策略研究结论。与之配套：[V4 架构设计（草案）](v4_architecture_design.md)、[架构决策记录](adr/README.md)、[质量门槛](quality.md)、[身份说明](identity.md)、[发布约定](releasing.md)。本文取代[工程结构优化路线图](engineering_structure_roadmap.md)中仍然适用的部分。

**一句话状态：结构整理（B1–B5）、身份与发布约定（A1–A4）、质量债务（C1–C5，C3 为持续项）和仓库/数据/环境（D2–D6）都已做完，其中 C1、C3、C5、D2–D6 在本 PR 里。还没做的是：首个发布标签 `v3.0.0`（需要仓库所有者）、分支清理（D1，需要所有者确认清单）、B6–B8（状态所有权、剩余 mixin、收紧与拆分），以及整理之后的新身份登记（A5）。**

## 1. 目标与原则

目标：在不改变交易语义的前提下，让项目**可持续**（谁都能安全地改）、**可扩展**（新增策略/选币/回测功能有清晰落点）、**健壮**（回退会被机器拦下）、**可复现**（身份、数据和环境可核验）。

沿用已在执行的原则：

1. 每个 PR 只改变一种东西：搬迁、行为、依赖、文档分开评审。
2. 先建护栏、再动结构；护栏的门槛必须先**实测**，不拍脑袋。
3. 不改写、不放宽任何已有的冻结校验；新方案与旧方案并存，直到有单独的决定取代。
4. 搬迁前先出依赖图（含函数内延迟导入），按实测而不是印象决定边界。
5. 能自动化的规则就写成检查（CI / 测试），不靠记忆。

## 2. 基线：已经完成的

| 编号 | 内容 | 状态 | 依据 |
|---|---|---|---|
| 0.1 | `.gitignore`/`.gitattributes` 修正，PR 模板、CODEOWNERS、SECURITY、CONTRIBUTING、Dependabot（仅 Actions） | 已完成 | #54 |
| 0.2 | 可编辑安装、控制台入口、删除 71 个文件的 `sys.path` 补丁、`install-smoke`（Linux/Windows × 3.11/3.13，已设为必需检查） | 已完成 | #60 |
| 0.3 | ruff/mypy 棘轮（最初 621/869 冻结；C2 之后降到 588/326）、覆盖率门槛 55%→85%、`-n 4` 并行、`slow` 标记、600 秒超时 | 已完成 | #65 |
| 0.4 | import-linter 分层规则，消除 `core → research` | 已完成 | #66 |
| 0.5 | 行为身份 `code-token/v1`、PR 影响报告、文档移动索引、断链检查 | 已完成 | #64 |
| 0.6 | `dev.py check`、ADR 0001–0006、dev container（未实际运行）、脚本索引补全 | 已完成 | #67 |
| — | 交付 ZIP 迁到 GitHub Release；文档整理（160→约 140 个 Markdown）；分支保护、密钥扫描、Dependabot alerts | 已完成 | #62、#52/#59、仓库设置 |
| V4.0-1 | 12 个运维/治理模块移到顶层包 `ops/`（ADR 0007） | 已完成 | #68 |
| — | 本路线图 | 已完成 | #69、#70 |
| A1 | G0 的结论：前向观察窗顺延，整理后重新登记（ADR 0008） | 已完成 | #71 |
| A2 | `identity_report.py` 识别移动与仅导入路径变化；用 #68 实测为"移动 15 个文件、4 个仅导入变化" | 已完成 | #71 |
| A3 | 发布约定：语义化版本、`CHANGELOG.md`、`release_check.py`、`pyproject` 版本 3.0.0（ADR 0009）。**标签 `v3.0.0` 尚未创建** | 已完成（标签待打） | #71 |
| A4 | 绑定记录 `release-bound-identity/v1`、`--bind-release/--verify-binding`（ADR 0010）；旧记录不动。真正的新身份登记留到整理完成之后 | 已完成 | #71 |
| B1 | 14 个信号元层模块（约 3500 行）移到顶层包 `signal_meta/`（ADR 0011）；回放指纹键保持旧名并有测试 | 已完成 | #74（经 #72、#73） |
| B2-1 | `core/market/`：17 个行情与时间/标的模块（ADR 0012）；`request_budget` 的仓库根路径修正并有测试 | 已完成 | #74 |
| B2-2 | `core/trading/`：20 个订单、保护单、账户与状态存储模块（ADR 0013） | 已完成 | #74 |
| B2-3 | `core/planning/`：7 个选币、打分、分配与目标仓位模块；`portfolio`、`valuation` 并入 `core/trading/`（ADR 0014） | 已完成 | #75 |
| B2-4 | `core/analytics/`：7 个审计、诊断、健康与指标模块（ADR 0015） | 已完成 | #76 |
| B2-5 | `core/foundation/`：10 个基础件模块（ADR 0016）；**持久化事件类型名保持 `core.domain:…`**；`core/` 平铺模块 78 → 0 | 已完成 | #77 |
| B3 | 分层规则清零 4 条已知例外：标签对账三个模块搬到 `signal_meta/`，`create_selector`/`restore_selector` 搬到 `research/ml_selection/serving.py`；`ignore_imports` 删除（ADR 0017） | 已完成 | #78 |
| B4 | 4 个宿主类的 13 个 mixin 改为持有 `host` 引用的协作类（ADR 0018）；mypy 869 → 404；**状态所有权迁移、报告与策略 mixin 未做** | 已完成（第一步） | #79 |
| B5 | `BacktestEngine.run` 与 `main()` 流水线化；入口移到 `cli/`，`main.py` 剩 5 行；`scripts/` 100 个脚本按生命周期分类并有测试，历史脚本因证据哈希绑定**不搬迁**（ADR 0019） | 已完成 | #80 |
| C4 | 测试治理：符号链接测试按能力探测跳过，`quarantine` 标记 + 到期检查，耗时预算与 CI 超时，约定写入 `docs/quality.md` | 已完成 | #81 |
| C2 | 棘轮扫描出的可疑模式逐个复核：借贷年化（`seconds_per_year`）是死代码，先写特征测试再清理；B023 共 29 处无害并显式绑定；B904、mypy `attr-defined` 逐项给出结论，无需改变交易行为。ruff 621 → 588，mypy 365 → 326 | 已完成 | #82 |
| C1 | 还债约定写入 CONTRIBUTING（顺手减少触碰文件的存量、规则清零后升硬门槛、季度回看）；`docs/quality.md` 增加季度记录表 | 已完成 | 本 PR |
| C3 | mypy 硬门槛清单 `scripts/mypy_hard_gate.txt`（128 个零错误模块），`quality_ratchet.py check` 对其中任何发现直接失败，`promote` 收进新达标的模块（ADR 0022）。持续项：随重构继续清零并扩围 | 已完成（持续项） | 本 PR |
| C5 | 覆盖率门槛 85% → 90%（行覆盖实测约 90.5%），为一批没有直接测试的运维与恢复工具补了行为测试；分支覆盖实测 79.8%，评估结论是暂不设门槛、按季度记录（ADR 0024） | 已完成 | 本 PR |
| D2 | 冻结研究输入清单 `data/binance/1d/_frozen_inputs.json` 与 `scripts/data_inputs.py`：30 个已跟踪 CSV 登记行数、范围与 SHA-256（LF 规范化），来源状态如实标注（2 个有下载记录，28 个未核验）；CI 与 `dev.py check` 核验（ADR 0020，`docs/data_identity.md`） | 已完成（28 个来源仍未核验） | 本 PR |
| D3 | `scripts/regenerate_lock.py`（再生成与离线 `--check`）；CI 从 `requirements.lock.txt` 安装并运行 `check_environment.py --strict-lock`、`verify_lock.py`、`regenerate_lock.py --check`；锁文件规范化（无版本变化）（ADR 0021） | 已完成（首次 CI 运行待观察） | 本 PR |
| D4 | Dependabot：Actions 每月、小版本分组、主版本单独成 PR；pip 版本更新继续不启用并写明原因（ADR 0021） | 已完成 | 本 PR |
| D5 | CodeQL 启用（不设必需检查）；dev container 保留并改用 `dev.py setup`，仍标"未实际运行"（本机无 Docker）；不增加 `install-smoke` 平台（ADR 0023） | 已完成（dev container 待有 Docker 的人验证） | 本 PR |
| D6 | `docs/architecture_inventory.md` 由 `scripts/architecture_inventory.py` 生成并在 CI 核对；交付图 `架构图/` 不改写，`path_moves.json` 记录其引用的 16 个已搬迁路径并被检查；文档维护约定写入 `docs/README.md`（ADR 0023） | 已完成 | 本 PR |

当前规模（`main`，2026-10-14）：`core/` 根没有平铺模块，有 12 个子包（`analytics`、`broker`、`events`、`exchange`、`factors`、`foundation`、`live_broker`、`market`、`metrics`、`planning`、`risk`、`trading`）；顶层包新增 `ops/`、`signal_meta/`、`cli/`；4013 个测试、266 个测试文件，本地串行（Windows，未装 xdist）约 5 分钟，CI 并行约 3 分钟；ruff 588、mypy 326；`BacktestEngine.run` 32 行，`main.py` 5 行；`scripts/` 100 个脚本（`supported` 34、`research` 47、`historical` 19）；24 条 ADR（0020–0024 在本 PR）；分层契约无例外；0 个发布标签（远端只有证据标签 `evidence-archive-2026-09`）。覆盖率 2026-10-14 在 Windows 本地实测：行 89.54%（补测试前）→ 90.58%（补测试后），分支 79.8%；CI 的 `--cov` 范围是 `core`、`ops`、`signal_meta`、`backtest`、`live_trading`，**不含 `cli`**。

## 3. 先决问题（阻塞项）

### G0 前向观察窗与源码身份（已决：顺延，见 [ADR 0008](adr/0008-defer-forward-observation-window.md)）
文档记载的前向观察窗为 **[2026-10-21, 2027-04-19)**（见[统一 Roadmap](unified_roadmap.md) 第 6 节），绑定的是 `followup-successor-03` 登记的源码清单（`code_hash = 1ed2cf91…`，395 个文件的字节哈希；协议尚未打开任何样本）。2026-10-12 的实测显示它已不对应 `main`（626 个文件中 536 个不同，`config/params.yaml` 的哈希也不再等于冻结的 `config_hash`：2026-10-04 的 `ca78e9c` 新增了默认关闭的 `capital:` 配置块）。此后的结构整理又改动了更多文件。

**结论：观察窗顺延。** 旧协议与回执保留为历史记录，不改写；结构整理先于新身份登记进行（已完成）；**下一步是登记新身份**：先说明 `capital:` 配置变更并另写协议，再用 A4 的绑定记录登记，新窗口和成熟日从登记时间重新计算。登记之后，冻结范围重新受约束：观察期内改动走重新登记流程。因此**登记之前**应把还想做的、会碰冻结范围（`core/`、`backtest/`、`strategies/`、`router/`、`composition/`、`live_trading/`、`signal_meta/`、`ops/`、`cli/`、`research/ml_selection/`、`config/`）的整理都做完。

另：已登记的纸面观察实验（`reports/paper_forward_observation_20261003`）按设计校验源码哈希，B1/B2 搬迁之后**不能再 `--resume`**，需要登记新协议才能追加会话（它的采集已结束，历史证据保留不动）。

## 4. 轨道与里程碑

编号 `A/B/C/D` 为轨道，`M` 表示里程碑；规模 S/M/L 指预计 PR 数量级（1 / 2–3 / 4+），不是日期。

### 轨道 A：身份与发布
目的：让"这个版本是什么"有一个可核验、与整理无关的答案。

| 编号 | 工作 | 规模 | 依赖 | 退出条件 |
|---|---|---|---|---|
| A1–A4 | **已完成**（见第 2 节） | — | — | 唯一遗留：创建并推送发布标签 `v3.0.0`（见[发布约定](releasing.md) §3），之后 `identity_report.py --base v3.0.0` 才可用 |
| A5 | **新身份登记**：说明 `capital:` 配置变更并另写协议；扩展 `register_strategy_successor.py` 接入绑定记录；在带注解的发布标签上登记 | M | A3 的标签、B 轨道完成 | 新窗口与成熟日从登记时间计算；旧协议不动；ADR 记录 |

### 轨道 B：结构（V4.0 的延续）
**B1–B5 已完成**（第 2 节）。每步沿用的流程：依赖图 → 方案 → 搬迁（无垫片）→ 身份报告 → 全量测试。下面是**遗留项**，都不是路线图原来的退出条件，而是做完 B4/B5 后实测发现的后续：

| 编号 | 工作 | 规模 | 依赖 | 退出条件 |
|---|---|---|---|---|
| B6 | **状态所有权迁移**：协作类现在通过 `host` 读写宿主上的 10–60 个属性；按属性设计归属，把状态搬进协作类，宿主属性改为只读代理或删除。涉及外部读写点（`state_export`、`recovery`、回测引擎、测试）和持久化路径，**每个宿主、每个协作类一个 PR**，先固定订单/成交/恢复/状态写入的行为 | L | B4 | 协作类拥有自己的状态；`host.<属性>` 引用显著减少；mypy 基线下降 |
| B7 | **剩余 mixin**：`backtest/reporting/` 的 4 个报告 mixin（部分被当作无状态方法包单独实例化）、`strategies/trend_breakout.py` 的 3 个私有 mixin（`mypy attr-defined` 剩下的 11 项都在这里） | M | — | mixin 继承清零 |
| B8 | **收紧与拆分**：`BacktestEngine.__init__`（149 行）、`_close_tail_positions`（118 行）；`_RunState`/`_MainState` 的字段目前标注为 `Any`，逐个收紧类型 | M | B5 | 无超过 100 行的函数；状态对象有具体类型 |

### 轨道 C：质量债务
目的：把"冻结的存量"变成"逐步清零"。

| 编号 | 工作 | 规模 | 依赖 | 退出条件 |
|---|---|---|---|---|
| C1 | **已完成**（本 PR）：约定写入 CONTRIBUTING（顺手减少触碰文件的存量、规则清零后升硬门槛、季度回看），`docs/quality.md` 有季度记录表 | 持续 | — | 下次回看 2027-01 |
| C2 | **已完成**（#82） | — | — | 结论见 `docs/quality.md` |
| C3 | **已完成，持续**（本 PR，ADR 0022）：`scripts/mypy_hard_gate.txt` 收进 128 个零错误模块，`quality_ratchet.py check` 对其中任何发现直接失败，`promote --write` 收进新达标的模块；其余 72 个文件的存量随重构清零 | 持续 | B4（已完成） | 硬门槛文件数增长；基线下降 |
| C4 | **已完成**（#81） | — | — | 约定见 `docs/quality.md` |
| C5 | **已完成**（本 PR，ADR 0024）：门槛 90%（本地行覆盖 90.58%），补了一批运维与恢复工具的行为测试；分支覆盖 79.8% 暂不设门槛、按季度记录。`cli` 仍不在 `--cov` 范围内，纳入它会改变口径，留待单独评估 | S | C4（已完成） | 门槛上调且 CI 通过（首次 CI 运行待观察） |

### 轨道 D：仓库、数据与环境
| 编号 | 工作 | 规模 | 依赖 | 退出条件 |
|---|---|---|---|---|
| D1 | 分支与 worktree 清理：现在远端约 64 个分支、本地 12 个 worktree（**数字已变，必须重新生成清单**）；已合并分支、Dependabot 旧分支、`old-origin` 的分支、失效的 worktree；未合并分支逐个决定（其中两个有不在 `main` 上的 8 月提交：本地 `docs/readme-architecture-and-local-data` 的 `80b6833`、远端 `docs/readme-glossary-update` 的 `29f3a71`，先与 `main` 对比再定，另一个是 `claude/canonical-trading-events-*`）。**先列清单，经确认后执行**（不可逆） | S | 所有者的确认 | 远端只保留 `main` 与进行中的分支 |
| D2 | **已完成**（本 PR，ADR 0020，`docs/data_identity.md`）：`data/binance/1d/_frozen_inputs.json` 为 30 个已跟踪 CSV 登记行数、范围与 SHA-256（LF 规范化），2 个有下载记录、28 个未核验；`scripts/data_inputs.py verify` 在 CI 与 `dev.py check` 中 | M | — | 缺口：28 个文件的来源仍未核验 |
| D3 | **已完成**（本 PR，ADR 0021）：`scripts/regenerate_lock.py`；CI 从锁文件安装并运行 `check_environment.py --strict-lock`、`verify_lock.py`、`regenerate_lock.py --check` | M | — | 首次 CI 运行待观察 |
| D4 | **已完成**（本 PR，ADR 0021）：Actions 每月、小版本与补丁分组、主版本单独成 PR；pip 版本更新继续不启用（Dependabot 不能运行再生成脚本） | S | D3（已完成） | — |
| D5 | **已完成**（本 PR，ADR 0023）：CodeQL 启用（不设必需检查）；dev container 保留并改用 `dev.py setup`，仍待有 Docker 的人验证；不增加 `install-smoke` 平台 | S | — | — |
| D6 | **已完成**（本 PR，ADR 0023）：`docs/architecture_inventory.md` 由 `scripts/architecture_inventory.py` 生成并在 CI 核对；交付图 `架构图/` 不改写，`path_moves.json` 记录它引用的 16 个已搬迁路径并被检查；文档维护约定在 `docs/README.md` | S | B1（已完成） | — |

### 当前状态（2026-10-14）

| 编号 | 状态 | 备注 |
|---|---|---|
| G0 | **已决：顺延** | ADR 0008；整理已完成，下一步是登记新身份（A5） |
| A1–A4 | 已完成 | 遗留：`v3.0.0` 标签待所有者创建并推送 |
| A5 | 未开始 | 依赖标签；先说明 `capital:` 配置变更 |
| B1–B5 | 已完成 | 见第 2 节；ADR 0011–0019 |
| B6–B8 | 未开始 | 遗留项；最好在 A5 之前做完会碰冻结范围的部分 |
| C1 | 已完成 | 约定在 CONTRIBUTING，季度记录在 `docs/quality.md`；下次回看 2027-01 |
| C2、C4 | 已完成 | #82、#81 |
| C3 | 已完成，持续项 | 硬门槛清单 128/200 个受检文件；其余 72 个随重构清零后用 `promote --write` 收进（ADR 0022） |
| C5 | 已完成 | 门槛 90%（本地 Windows 实测行覆盖 90.58%）；首次 CI（Linux）的实测值待观察，低于 90% 时补测试而不是降门槛（ADR 0024） |
| D1 | **待所有者确认清单** | 清单已重新生成：`outputs/branch_cleanup_proposal.md`（被 `.gitignore` 忽略，不入库）：55 个已合并的远端分支、50 个已合并的本地分支、8 条失效的 worktree 记录可以直接清理；5 个未合并的远端分支与 6 个未合并的本地分支要逐个决定。确认前不执行任何删除 |
| D2 | 已完成 | `_frozen_inputs.json`（ADR 0020）；28 个文件的来源仍是"未核验"，补齐需要重新下载并留下缓存清单记录 |
| D3 | 已完成 | 锁文件在 Linux/Windows × 3.11/3.13 都有现成轮子（仅 `multitasking` 为纯 Python 源码包）；CI 首次运行待观察（ADR 0021） |
| D4–D6 | 已完成 | CodeQL 首次运行后需要有人看一遍结果；dev container 仍待有 Docker 的人验证（ADR 0021、0023） |

## 5. 依赖与顺序

```mermaid
flowchart LR
  TAG["v3.0.0 标签（所有者）"] --> A5["A5 新身份登记"]
  B6["B6 状态所有权"] --> A5
  B7["B7 剩余 mixin"] --> A5
  B8["B8 收紧与拆分"] --> A5
  D3["D3 CI 从锁安装"] --> D4["D4 pip 更新"]
  D2["D2 数据身份"] --> M1["M1 进入 V4 功能开发"]
  D3 --> M1
  A5 --> M1
  D1["D1 分支清理（所有者确认）"] --> M1
  C3["C3 mypy 扩围"] --> C5["C5 覆盖率 90%"]
```

**接下来的顺序（建议）：**

| 阶段 | 内容 |
|---|---|
| **立刻** | 所有者创建并推送 `v3.0.0`（见发布约定 §3）；确认或修改 D1 清单（`outputs/branch_cleanup_proposal.md`）后执行 |
| **接着（互相独立，可并行）** | 观察本 PR 的 CI 首次运行（锁定环境安装、覆盖率 90%、CodeQL）；C3 每次一小批继续扩围；补齐 28 个数据文件的来源记录（D2 的缺口） |
| **登记之前尽量做完** | B6–B8：它们会碰冻结范围，登记之后就要走重新登记流程。B6 最重，可以拆开、只做其中价值最高的宿主；做不完的明确记为"观察期内不动" |
| **然后** | A5 新身份登记（先写 `capital:` 配置变更说明与协议，引用 D2 的数据清单摘要）→ M1 |

## 6. 进入 V4 功能开发的条件（M1）

下列全部满足后，再开始 V4 设计中的特征管线、回测两层化、选币统一、新策略与 ML 改进（它们的结论是否可信依赖这些底座）：

1. G0 已有结论并写成 ADR（A1，已完成：ADR 0008）；新身份已按 A4 的流程登记（A5，**未做**）。
2. 身份报告能识别移动（A2，已完成），并有发布标签约定且首个标签已创建（A3，约定已完成，**标签待打**）。
3. 数据身份完整（D2，**已完成**，另有 28 个文件来源未核验）、CI 在锁定环境运行（D3，**已完成**，首次 CI 运行待观察）。
4. `core/` 已完成拆分且无新增的反向依赖（B1–B3，**已完成**）。
5. 分支/worktree 已清理（D1，待确认），测试治理约定生效（C4，**已完成**）。

M1 之后的功能轨道（特征管线 → 回测两层化 → 选币统一 → 新策略 → ML v2）见[V4 设计](v4_architecture_design.md)第 9 节；它们要沿用"先护栏、再搬迁、再功能"的顺序。注意 V4 设计里的"选币统一"和"回测两层化"是**功能**工作，不在本路线图范围内。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 结构搬迁破坏冻结候选的可验证性 | G0 已决（顺延观察窗）；新身份登记前完成搬迁，登记后冻结范围不再动；每次搬迁都有身份报告和全量测试 |
| 搬迁引入向上依赖或循环 | import-linter 在 CI 必须通过；分层契约现在**没有例外**，测试禁止新增（ADR 0017） |
| 事件/状态的持久化格式被无意改变 | 事件编解码器按 `cls.__module__` 生成存储名，搬迁定义在 `core/foundation/domain.py` 里的类型会改变它；`core/events/codec.py` 的 `_WIRE_MODULES` 把它固定为旧名，`test_event_performance_contracts` 的线上哈希测试会拦下（ADR 0005、0016） |
| 批量改写路径时误改"哈希固定的输入" | 搬迁时必须排除：`config/` 下非 `.py` 文件（`params.yaml` 的哈希就是冻结的 `config_hash`）、`tests/fixtures/`、`docs/baseline/`、`docs/baselines/`、冻结证据和记录文件哈希的验收 JSON；`dev.py check` 里的 `archive` 检查和若干测试会发现，但不要依赖它们 |
| 协作类被浅拷贝 | 协作类缓存在宿主 `__dict__` 里，浅拷贝的副本会共用指向原宿主的协作类；用 `deepcopy`（会重定向 `host`）。现在仓库里没有这样的用法 |
| 棘轮基线变成"永久豁免" | C1 的清零约定与季度回看；规则清零后升为硬门槛；**`quality_ratchet.py update` 会接受新增，只能在 `check` 通过之后运行** |
| 并行测试或新检查带来偶发失败 | C4：偶发失败必须定位，必要时用 `quarantine` 标记隔离并设期限，不得靠重跑掩盖 |
| 一次性大改动难以评审 | 每个 PR 只含一种变化；搬迁 PR 要有依赖图与影响报告 |
| 叠加的 PR 没进 `main` | **不要把 PR 叠在中间分支上**：叠加的 PR 合并后只进了中间分支（#72、#73 就是这样没进 `main`，靠 #74 补上）。每个 PR 直接对 `main`，上一个合并后再做下一个 |
| 清理操作不可逆 | D1 先列清单经确认；不改写 Git 历史（ADR 0006） |

## 8. 维护方式

- 完成一项就把本文第 2 节的表格追加一行（编号、PR、依据），并在第 4 节改状态；不要删除历史行。
- 新决定写成 ADR（`docs/adr/`），本文只引用它。
- 与统一 Roadmap 的关系：本文不改阶段与任务状态；若某项工作需要登记为 SYS 任务，在[开发计划](development_plan.md)里按其规则登记，并在这里引用编号。
- 每个里程碑结束时回看第 3 节（先决问题）与第 7 节（风险）是否仍然成立。

## 9. 交接说明（给下一个维护者或新会话）

**工作方式（已在 #52–#82 中沿用）：**

- 流程：从最新 `origin/main` 拉分支 → 实现并本地跑 `python scripts/dev.py check`（`--no-tests` 跳过长测试）和完整测试 → 提交 → 推送 → 开 PR → 等 CI。**合并由仓库所有者操作**，AI 助手的合并操作会被权限系统拦下；严格分支保护要求分支是最新的。**每个 PR 直接对 `main`，不要叠加**（见第 7 节）。
- 必需检查共 5 项：`quality` 与 `install-smoke`（ubuntu/windows × 3.11/3.13）。Dependabot 只管 GitHub Actions，每月一次、小版本与补丁分组、同时最多 2 个 PR（ADR 0021）。
- 不可逆或对外可见的操作先列清单、经确认再做：删除分支、迁出文件、改仓库设置、创建标签或 Release。
- 提交信息末尾带 `Co-Authored-By` 行，PR 描述末尾带 `Generated with Claude Code` 行（由会话的归属规则给出）。
- 搬迁与重构的节奏（B1–B5 验证过）：先量（依赖图、`__file__`/`__module__`/pickle/动态导入的搜索）→ 用脚本机械改写、不手改方法体 → 先跑相关测试子集，再跑**完整**测试 → 写 ADR → 更新路线图与 CHANGELOG。**只跑子集会漏**：B4 第一次就是因为没跑完整套件，23 个失败在最后才出现。

**机器检查与它们的文档：** `scripts/dev.py list`；[质量门槛](quality.md)（棘轮、覆盖率、标记、不稳定测试与耗时预算约定）；[身份说明](identity.md)（行为身份、移动识别、`doc_index.json`、断链检查）；[发布约定](releasing.md)（版本、标签、绑定记录）；[架构边界](architecture_review.md)（分层）；`scripts/classification.json`（脚本生命周期）；[ADR](adr/README.md)（0001–0024）。

**本轮得到的教训（必读）：**

1. **没有保存搬迁工具。** B1–B5 用的机械改写脚本（按模块名改写路径、mixin → 协作类的代码生成、`run`/`main` 的阶段拆分）只存在于会话的临时目录，没有进仓库。做法已写在对应 ADR（0011–0019）里；要再做类似工作需要重写。如果还会继续做 B6，建议先把需要的工具写成 `scripts/` 里经过测试的脚本，再用。
2. **`ruff check` 默认不报未使用的导入，`import` 失败要靠真正导入或测试。** 删除 mixin 后残留的 `from … import XMixin` 没被 ruff 抓到，是运行时 `ImportError`。
3. **替身类要暴露和原来完全一致的方法集合。** 把测试里的 mixin 替身换成继承真实 `LiveTradingEngine`，会因为 `getattr(host, "_ensure_state_store", None)` 这类探测改变行为；用 `tests/collaborator_host.py::TickOrchestratorHost`。
4. **跳过 `__init__` 构造对象在测试里很常见**（`object.__new__(LiveBroker)`）。所以协作类用 `cached_property` 惰性创建，而不是在 `__init__` 里创建。
5. **持久化名与 `__module__`、`ROOT` 与 `__file__`**：搬迁时这两类东西会悄悄变（`request_budget` 的仓库根路径、`cli/` 的 `ROOT = parents[1]`、事件类型名）。每次搬迁前都 `grep` 它们。
6. **用变异验证特征测试**：先写测试、在现状上通过、再人为改坏被测常量确认测试会失败，最后才清理（C2 的 `seconds_per_year` 就是这样做的）。
7. **哈希固定的历史脚本不能动**：`scripts/classification.json` 里标为 `historical` 的 19 个脚本把自身哈希写进证据回执，搬迁或改 `ROOT` 会让历史批次无法核验或续跑；这就是 B5 没有搬迁 `scripts/` 的原因。

**已知陷阱：**

- 本地 Windows 上没有符号链接权限（WinError 1314）时，依赖符号链接的测试会被跳过（`tests/capabilities.py`）；Linux CI 上照常运行。
- 共享的本地环境是 `D:\QuantTradingV1\.venv`：没有 `pytest-xdist`（`-n 4` 会报错，串行约 5 分钟），脚本需要 `PYTHONPATH=.` 才能 `import core`；`lint-imports` 需要单独安装。联网不稳时 `pip install` 会挂起。
- 工作区是 CRLF、仓库内容是 LF：读取工作区文件做哈希前要按 git 规则规范化（`core/foundation/source_identity.py` 已处理）；批量改写文本文件时保留原有换行符。
- 在某些 shell 工具里，heredoc 中的反斜杠转义会被改写（出现过 `\0` 变成真正的空字节、`\\` 被吞掉）。需要写含转义的 Python 时，先把脚本写成文件再运行，或用 `chr(…)` 构造。
- 链接检查只看已被 git 跟踪的文件：新增文档要先 `git add`。
- 全量测试在 Windows 上曾偶发失败：`tests/test_data_versions.py::test_concurrent_identical_writes_publish_complete_objects_once`（`DataVersionStore._put` 读刚被另一线程换到位的文件时 `PermissionError`，负载下约 3/40）。已定位，修复在单独的 PR（#84）里；该 PR 合并之前，本地全量测试里单独这一项失败可以先重跑它确认。
- `scripts/` 新增脚本要同时更新 `scripts/README.md` 和 `scripts/classification.json`，否则测试失败。
- 冻结的历史证据（`docs/archive/2026-08*`、`2026-09*`、`docs/research/ml_selection_next_acceptance_20261005.json`、`docs/codex_mail_findings_20260920.json`、`docs/research/current_strategy_protocol.json`、`reports/` 回执，以及 `docs/research/paper_continuation_acceptance_20261003.json` 这类记录当时文件哈希的 JSON）不得改写；移动文档要在 `docs/doc_index.json` 追加一条。

**第一件事：** 请所有者创建并推送 `v3.0.0`（见[发布约定](releasing.md) §3，先 `python scripts/release_check.py v3.0.0`）；然后确认 D1 清单（`outputs/branch_cleanup_proposal.md`，不入库；它只来自 `git branch` / `git worktree` 的输出，过时了就重新生成，不要用旧数字）。再观察本 PR 的首次 CI 运行（锁定环境安装、90% 覆盖率、CodeQL），之后按第 5 节"接着"一栏选做。
