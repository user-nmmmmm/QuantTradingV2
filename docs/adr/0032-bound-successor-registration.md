# 0032 新身份登记：绑定发布标签，配置变更走机器核对的声明（A5）

- 状态：已采纳（登记工具与程序；**实际登记尚未执行**，见"执行步骤"）
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) A5，[ADR 0008](0008-defer-forward-observation-window.md)，[ADR 0010](0010-release-bound-identity.md)，[发布约定](../releasing.md)，[身份说明](../identity.md)

## 背景
ADR 0008 把前向观察窗顺延，并规定结构整理完成后：先单独说明 `capital:` 配置块的变更并另写协议，再登记新身份，新窗口与成熟日从登记时间重新计算。ADR 0010 提供了"标签 + 行为身份回执"的绑定记录，并把"让登记脚本接入绑定记录"留给这一步。

现有的 `scripts/register_strategy_successor.py` 有三处不能直接用：

- 它按工作区字节清单（含 CRLF/LF 差异）冻结，正是 ADR 0003/0010 里说的"挡住无风险整理"的那种身份。
- 配置哈希一旦与父协议不同就直接拒绝（"需要另写协议"）。
- 它读 `config/params.yaml` 的**工作区**字节哈希；工作区是 CRLF、仓库是 LF，同一个提交在不同检出上得到不同哈希。

实测（2026-10-14）：

- 父协议 `followup-successor-03`：`status = pending_unseen_evidence`，`opened_at` 为空，`data_access_log` 为空，没有 `.opened` 回执；快照里的 `params.yaml` 与冻结的 `config_hash`（`49665830…`）一致。
- 当前 `config/params.yaml` 与父快照解析后的差别**只有** `allocation.capital`（提交 `ca78e9c` 新增，`enabled: false`，只在回测时由 `--smart-allocation` 逐次打开，正式候选不使用）。
- `v3.0.0` 标签（`619abfa`）早于 B6：`identity_report.py --base v3.0.0 --head HEAD` 报告冻结范围里 9 个文件行为改变（B6 的 `Matching`/`Liquidation`/`AccountSync`/`Recovery`/`StateExport`/`TickOrchestrator` 与两个宿主）。所以**不能在 `v3.0.0` 上登记**，要在 B6 合并之后的新标签上登记。

## 决定
- 新增 `scripts/register_bound_successor.py`（`register_bound_successor`）。**旧脚本及其校验原样保留**；旧登记继续按原来的逐字节方式验证。
- **绑定发布标签**：必须是带注解、`release_check.py --verify-tag` 通过（版本号与 CHANGELOG 一致、在 `origin/main` 线上）的标签；协议的 `code_hash` 取该标签的行为身份（`code-token/v1`）整体哈希，完整绑定记录 `identity_binding.json` 与回执一并写入输出目录。标签日期不得晚于登记时间。
- **配置哈希与换行无关**：取标签所指提交里 `config/params.yaml` 的 git blob，LF 规范化后求 SHA-256。
- **配置变更必须附声明** `config-change-statement/v1`（`docs/research/capital_config_change_statement_20261014.json`），并由脚本核对而不是信任：声明里的父配置哈希必须等于父协议冻结的哈希、新配置哈希必须等于标签处的哈希、父快照与标签处配置解析后的差别必须**恰好**等于声明的 `added` 路径，且每个新增块必须含 `enabled: false`。任何其他键的修改、删除、或新增块默认开启，一律拒绝。
- **父协议的检查与旧脚本相同**：必须未打开、无访问记录、无 `.opened` 回执、实验登记哈希未变；父协议文件登记前后字节不变，并复制为 `parent_protocol.json`。
- **新时钟**：用 `freeze_prospective` 在登记时间起算（观察边界为次日 0 点，禁运 30 天，观察 180 天，再加 20 天成熟），不回填，不继承旧窗口的已过天数；`admission` 仍为 `paused_revalidation`，回执写明登记不是策略验证也不是实盘放行。
- 输出只写入新目录（`exist_ok=False`），工具不创建、不移动标签。

2026-10-15 补充：登记与样本打开的竞争、完整目录发布和中断重试按 [ADR 0033](0033-atomic-bound-registration.md) 执行。本文的标签、配置变更和观察窗约束继续保留。

## 执行步骤（需要仓库所有者）
实际登记是不可逆的对外可见操作（启动新时钟并固定身份），不由助手执行。顺序：

1. 本 PR 合并（工具、声明、ADR、路线图）。
2. 发版 PR：只改 `pyproject.toml` 版本为 `3.1.0`、把 CHANGELOG 的 `[Unreleased]` 改成 `[3.1.0] - <日期>`、必要的文档（结构搬迁属 MINOR，见发布约定 §1）；合并。
3. 所有者按发布约定 §3 创建并推送带注解标签 `v3.1.0`（先 `python scripts/release_check.py v3.1.0`，后 `--verify-tag`）。
4. 登记（在 `origin/main` 的检出上运行；若 `config/params.yaml` 在标签之前变过，声明里的新哈希需要重新生成，脚本会拒绝过期声明）：
   ```bash
   python scripts/register_bound_successor.py --tag v3.1.0 \
     --historical-batch reports/roadmap_v3/SYS-11/20260920-followup-successor-03 \
     --config-change docs/research/capital_config_change_statement_20261014.json \
     --output reports/roadmap_v3/A5/<日期>-bound-successor-01
   ```
5. 登记后更新[统一 Roadmap](../unified_roadmap.md) 第 6 节的观察窗（ADR 0008 要求），并在路线图把 A5 标为已完成、写下回执摘要；`reports/` 被 `.gitignore` 排除，回执要像 `followup-successor-03` 那样另行保存。

演练（2026-10-14）：以 `v3.0.0` 与真实的父批次、真实的 `params.yaml` 跑通了全流程，输出放在临时目录并已删除，**不是登记**；用于确认真实输入能通过全部检查。

## 备选方案
- 修改旧脚本放宽配置检查：会放宽一个已有的冻结校验（路线图原则 3）。
- 在 `v3.0.0` 上登记：B6 之后的行为改变不在其身份内，登记的候选不是实际运行的代码。
- 把声明当作自由文本：无法防止"顺手改了别的配置"，所以改为机器核对差别。
- 由助手直接登记：登记启动不可撤销的观察时钟并固定身份，属于需要所有者确认的操作。

## 后果
- 登记之后，冻结范围（`core/`、`backtest/`、`strategies/`、`router/`、`composition/`、`live_trading/`、`signal_meta/`、`ops/`、`cli/`、`research/ml_selection/`、`config/`）的行为改动要走重新登记流程；仅注释与排版不算。
- 最早成熟日约为登记日 + 约 7.5 个月（30 天禁运 + 180 天观察 + 20 天成熟，从登记次日算起）。
- 取代 ADR 0010 中"`register_strategy_successor.py` 接入绑定记录留给真正的新身份登记"的待办：采用新增脚本而不是修改旧脚本。
