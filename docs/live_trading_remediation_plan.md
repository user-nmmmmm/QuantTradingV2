# QuantTrading 实盘交易领域执行计划

> 文档状态：Active v1.1  
> 上位路线图：[`unified_roadmap.md`](unified_roadmap.md)（项目级阶段、优先级定义和放行门槛的唯一权威来源）  
> 回测领域对应文档：[`backtest_metrics_detailed_development_plan.md`](backtest_metrics_detailed_development_plan.md)  
> 当前结论：仅允许研究、回测和交易所 sandbox；禁止真实资金无人值守运行

## 1. 文档定位

本文是 `unified_roadmap.md` 中 R4（交易所与订单闭环）、R5（共享事件管线）、R6（监控运维）阶段的领域详细计划，把实盘安全整改拆解为可执行任务（G0–G10）、依赖关系和验收标准。本文不重复维护项目级阶段状态或优先级定义——优先级定义见 `unified_roadmap.md` §4；R0–R8 阶段状态只在 `unified_roadmap.md` 更新；本文的勾选仅表示各 G 阶段任务的证据是否齐备。

发生冲突时，按以下顺序处理：

```text
unified_roadmap.md
  → development_plan.md（回测领域）/ live_trading_remediation_plan.md（本文件，实盘领域）
  → g1_live_safety.md / g2_order_lifecycle.md 等阶段详情文档
```

## 2. 当前基线

- 测试基线以当前虚拟环境下 `pytest`/`unittest` 全量结果为准，历史通过数字不作为最新状态引用；
- 实盘引擎已有已收盘 bar 过滤、账户同步失败时停止交易、bar claim 和本地状态存储的初步实现；
- G1（安全启动与凭据治理）各项任务已完成，详见 §6 勾选状态与 [`g1_live_safety.md`](g1_live_safety.md)；
- G2 起的对账、恢复、监控闭环仍是当前阻断项，详见各 G 阶段任务清单。

## 3. 总体执行路径

```text
G0 冻结基线与安全边界
  ├─> G1 安全启动与凭据治理
  ├─> G2 订单提交结果与幂等闭环
  └─> G3 账户事实与估值对账
          │
          ├─ G2 + G3 ─> G4 统一领域模型与共享执行契约
          │                  │
          │                  └─> G5 故障恢复、监控与运维
          │                              │
          └──────────────────────────────┴─> G6 Sandbox/Paper 长跑

回测支线：G0 → G7 回测账本与指标可信度 → G8 样本外与稳健性
治理支线：G0 → G9 仓库、依赖与 CI 治理

G6 + G7 + G8 + G9 全部达标后，才允许进入 G10 小额真实资金灰度评审。
```

## 4. G0：冻结基线与安全边界

**映射阶段：R0；优先级：P0/P1。**

目标是确保后续整改建立在可复现、可审计且不会误触真实资金的基线上。

### 任务

- [ ] 为当前工作区生成变更清单，区分用户代码、文档迁移、测试基线和生成物；
- [ ] 保存当前测试套件的结构化结果和 Python/依赖版本；
- [ ] 固定三类回测 fixtures 的数据摘要、配置摘要和结果 hash；
- [ ] 明确当前禁止真实资金无人值守运行，并同步到部署文档；
- [ ] 规定每个整改任务必须包含失败测试、实现、验收证据和已知限制；
- [ ] 禁止在 P0/P1 未关闭前扩展 Alpha、ML 自动交易或自动扩大风险。

### 验收标准

- 同一固定输入连续运行三次，orders、fills、equity 和 metrics 结构一致；
- 测试命令、Python 版本和 resolved config 可复现；
- 无需连接外部交易所即可完成核心回归；
- 文档明确标注 sandbox-only 边界；
- 未提交工作不会被后续仓库清理误删或覆盖。

## 5. G1：安全启动与凭据治理

**映射阶段：R4/R8 前置；优先级：P0。**

目标是让最安全的模式成为默认值，并杜绝因遗漏参数直接进入真实交易。

### 任务

- [√] `run_live.py` 默认启用 sandbox；
- [√] 真实模式改为显式 `--live`，并与 `--sandbox` 互斥；
- [√] 移除 `--api_key` 和 `--secret` 命令行参数；
- [√] 凭据只允许通过环境注入或外部密钥管理器提供；
- [√] 启动日志不得输出凭据、签名、完整请求头或敏感账户载荷；
- [√] 增加交易所、账户类型、symbol 白名单和 base currency 启动检查；
- [√] 增加每单最大名义金额、每日最大新增风险和全局 kill switch；
- [√] 真实模式要求最小权限 API，禁用提现权限；
- [√] 未显式配置真实模式限额时启动失败，不使用宽松默认值。

### 验收测试

- 未传任何模式参数时只连接 sandbox；
- 命令行无法传入密钥；
- 缺少凭据、错误账户类型、非白名单 symbol 或超限配置时 fail closed；
- 日志和异常信息中不存在明文密钥；
- kill switch 激活后不产生任何新增风险订单。

### 退出条件

- 误操作不能直接进入真实环境；
- 所有危险配置都必须显式开启；
- 真实模式启动检查具有自动测试和人工检查表。

## 6. G2：订单提交、幂等与成交事实闭环

**映射阶段：R4；优先级：P0。**

这是本轮最重要的整改阶段。目标是保证任何订单都不会因为超时、崩溃或重试而丢失事实或重复提交。

### 6.1 统一下单结果

- [√] `LiveBroker.submit_order()` 返回结构化结果，不再返回隐式 `None`；
- [√] 区分 `accepted/open/partial/filled/canceled/rejected/unknown`；
- [√] 下单异常分类为网络、超时、限流、认证、交易规则、余额不足、交易所不可用和未知；
- [√] 调用方根据结果推进工作流，不能以“函数未抛异常”作为订单成功依据；
- [√] bar 只有在订单事实已安全持久化后才能标记为 processed。

### 6.2 幂等提交

- [√] 为每个 `OrderIntent` 生成确定性的 `client_order_id`；
- [√] ID 至少绑定 exchange、account、symbol、timeframe、bar、strategy、action 和序号；
- [√] 提交交易所前先写入 `OrderStore`，状态为 `submitting`；
- [√] 交易所支持时将 client ID 原样传入；
- [√] 同一 client ID 的重复请求不得创建第二张订单；
- [√] 超时后先查询订单，禁止盲目重新提交。

### 6.3 订单状态机与成交账本

- [√] 将 `core/domain.py`、`core/orders.py` 和 `core/order_store.py` 接入实盘主链路；
- [√] 所有状态转换经过 `validate_transition()`；
- [√] 交易所 open 状态不得记为成交；
- [√] 每个 fill 独立持久化，支持部分成交和多次成交；
- [√] requested qty、filled qty、remaining qty 和 average fill price 可对账；
- [√] cancel、fill 和 cancel-reject 竞态有明确规则；
- [√] `reduceOnly`、position side 和 hedge/one-way mode 显式建模；
- [√] 内存 `self.trades` 不再作为权威账本。

### 6.4 重启恢复

- [√] 启动时恢复所有非终态订单；
- [√] `processing` bar 有租约或 stale claim 恢复机制；
- [√] 崩溃发生在“提交前、提交后响应前、响应后持久化前、部分成交后”均可恢复；
- [√] unknown 状态禁止新增同方向风险，直到对账完成；
- [√] 无法恢复时进入 `RECONCILING/HALTED`，不得静默继续。

### 验收测试

- `create_order` 已接收但客户端超时，不重复下单；
- 提交前崩溃，重启后可安全继续；
- 提交后持久化前崩溃，重启后通过 client ID 找回订单；
- 部分成交后崩溃，恢复后 filled/remaining 正确；
- cancel 与 fill 乱序到达，最终状态和仓位一致；
- 交易所拒单后 bar 状态、订单状态和风险状态一致；
- 同一信号重复消费 100 次，交易所最多存在一张逻辑订单。

### 退出条件

- 每个 signal 可追溯到 intent、order 和 fills；
- 没有“日志显示失败但 bar 已完成”的路径；
- 没有以重试代替查询的 unknown 处理；
- 重启不会产生重复订单或遗失未知订单。

## 7. G3：账户事实、估值与周期性对账

**映射阶段：R1/R4/R6；优先级：P0/P1。**

目标是确保本地 Portfolio 不会把不完整的交易所响应误当成完整账户事实。

### 任务

- [ ] 账户快照区分 free、used/locked、total cash；
- [ ] 同步 open orders、spot assets 和 derivative positions；
- [ ] 保存交易所报告的 equity、margin balance、unrealized PnL 和本地重算值；
- [ ] 建立 cash、positions、orders、fills、fees 和 equity reconciliation；
- [ ] 衍生品 `fetch_positions()` 失败时 fail closed，禁止回退为 spot 语义继续交易；
- [ ] 缺价、陈旧价、未知合约乘数和未知仓位均标记为不可交易；
- [ ] 账户外部充值/提现与交易 PnL 分离；
- [ ] 明确 UTC 或配置化 risk-day 边界；
- [ ] daily start equity 从权威快照恢复，不以进程中途启动值代替；
- [ ] 对账差异超过容差时停止新增风险并告警。

### 验收测试

- Spot 挂单锁定 USDT 时权益不丢失；
- open buy/sell order 均能正确反映资金与风险占用；
- derivative position 接口失败时不构造虚假的空仓快照；
- 本地权益与交易所权益在容差内一致；
- 缺失任一持仓价格时快照失败，不使用均价静默替代；
- 进程在交易日中途重启，daily drawdown 基准保持一致；
- 外部入金不会被错误计为策略收益。

### 退出条件

- 账户事实完整性可机器判断；
- 事实不完整时默认停止新增风险；
- 日终和周期性对账均能输出结构化差异报告。

## 8. G4：统一领域模型与共享执行契约

**映射阶段：R4/R5；优先级：P1。**

目标是消除回测和实盘的订单状态、成交和风险语义分叉。

### 任务

- [ ] 只保留一套 `OrderStatus`；
- [ ] 定义统一 `MarketEvent/Signal/OrderIntent/Order/Fill/PortfolioSnapshot`；
- [ ] 定义 correlation ID、causation ID、strategy ID 和 run ID；
- [ ] 回测和实盘共用 signal → risk → intent 管线；
- [ ] 回测 Broker 和 CCXT Broker 仅作为 execution adapter；
- [ ] 统一 market/limit/stop、TIF、partial fill、cancel 和 reject 语义；
- [ ] 交易所特有字段留在 adapter payload，不污染核心领域模型；
- [ ] 旧字段保留受测试的迁移兼容层，并给出移除日期。

### 验收标准

- 同一事件流在回测和 replay 模式产生相同 signal 和 intent；
- 领域层不依赖 CCXT；
- 状态转换只有一份定义；
- 报告、账本和监控读取同一事实模型；
- 模式差异可以通过 adapter 测试说明，而不是隐藏在策略代码中。

## 9. G5：故障恢复、监控、告警与运维

**映射阶段：R6；优先级：P1/P2。**

### 9.1 引擎运行状态

- [ ] 引入 `STARTING/HEALTHY/DEGRADED/RISK_OFF/RECONCILING/HALTED/STOPPED`；
- [ ] `_tick()` 顶层有统一错误边界；
- [ ] 网络、限流和交易所故障采用有上限的指数退避与抖动；
- [ ] 连续失败超过阈值进入 risk-off 或 halted；
- [ ] 优雅停机时停止接收新任务、完成持久化并关闭数据库连接；
- [ ] supervisor 能区分 liveness、readiness 和 trading-enabled。

### 9.2 状态与遥测

- [ ] `live_status.json` 使用临时文件加原子替换；
- [ ] 状态输出包含 `schema_version`；
- [ ] 直接复用权威 `PortfolioSnapshot`，不再次以不同价格规则估值；
- [ ] 输出 price time、account sync time、data freshness、last processed bar 和 reconciliation status；
- [ ] 监控状态不得作为权威订单或成交账本；
- [ ] 增加心跳、订单延迟、同步失败、数据陈旧、对账差异和熔断指标。

### 9.3 告警与演练

- [ ] 告警支持触发、确认、恢复、抑制和升级；
- [ ] P0 告警至少覆盖 unknown order、unknown position、重复 ID、对账失败和 kill switch；
- [ ] 定义人工恢复 runbook；
- [ ] 定期演练断网、限流、交易所 5xx、数据库锁、状态损坏和进程崩溃；
- [ ] 备份和恢复 SQLite 状态，验证恢复点与恢复时间目标。

### 退出条件

- 任一关键故障都能被发现、分类、告警和安全停止；
- 状态文件不会出现半写 JSON；
- 操作员可以根据 runbook 恢复或保持系统停止；
- 故障不会自动转化为新增仓位风险。

## 10. G6：Sandbox 与 Paper Trading 长跑

**映射阶段：R7；优先级：P1。**

### 准入条件

- G1–G5 全部达到退出条件；
- P0 问题清零；
- 所有新增故障注入测试通过；
- 配置、数据库和日志已备份；
- 有明确值班人和停止流程。

### 长跑计划

- [ ] 单交易所、单账户类型、单 symbol 启动；
- [ ] 先 paper，再 sandbox 下真实 API 请求；
- [ ] 连续运行至少 2–4 周；
- [ ] 覆盖市场活跃、低流动性、跨日和周末时段；
- [ ] 每日执行 orders/fills/positions/cash/equity 对账；
- [ ] 每周执行一次故障注入和重启恢复；
- [ ] 保存信号、订单、成交、快照、告警和人工操作审计记录。

### 退出条件

- 无无法解释的重复订单；
- 无无法解释的持仓或现金差异；
- 所有 unknown 状态均能在规定时间内恢复或安全停止；
- 对账差异持续在容差内；
- 关键告警无漏报；
- 重启恢复演练全部通过；
- 连续 2–4 周没有未关闭的 P0/P1 事件。

## 11. G7：回测账本、成本与指标可信度

**映射阶段：R1/R2；优先级：P1/P2。**

本阶段沿用 `development_plan.md` 的 Batch 1–8 和回测指标详细计划，不在本文重新定义公式。

### 必须完成的关键项

- [ ] 统一 `MetricResult` 和 JSON schema；
- [ ] 指标区分 `ok/insufficient_data/undefined/not_modeled/invalid_input`；
- [ ] 建立权威 fill ledger 和 FIFO trade ledger；
- [ ] 对账 Gross PnL、commission、slippage、impact、funding、borrow 和 Net PnL；
- [ ] 完成 cash、position、realized/unrealized PnL 和 equity bridge；
- [ ] 配置缺失或 schema 不合法时失败，不静默切换到不同 fallback；
- [ ] 保存 resolved config、config hash、数据摘要和公式版本；
- [ ] 缺少 funding、borrow、spread 或合约规则时明确 `not_modeled`；
- [ ] 报告显示 N/A 及原因，不以 0 或无穷大伪装不可计算值。

### 验收性质

- 固定订单下成本增加时净 PnL 不增加；
- closed trades 汇总与已实现净 PnL 一致；
- 期初权益、资金流、PnL、成本和期末权益可桥接；
- 输入 DataFrame 不被指标函数修改；
- 多标的归因汇总与组合总账一致；
- 无交易、全赢、全亏、零波动、负权益和未恢复回撤均有明确定义。

## 12. G8：样本外验证与策略稳健性

**映射阶段：R3；优先级：P2。**

### 任务

- [ ] 严格分离 train、validation 和 test；
- [ ] 参数选择过程不得读取样本外结果；
- [ ] 实施 walk-forward；
- [ ] 进行参数邻域和稳定平台分析；
- [ ] 使用 block bootstrap 和 Monte Carlo；
- [ ] 记录 trials count 并进行多重检验修正；
- [ ] 按市场阶段、波动区间、symbol、交易所和方向分段；
- [ ] 进行手续费、滑点、spread、impact、funding、延迟和拒单敏感性分析；
- [ ] 与简单 buy-and-hold、cash 和风险匹配基准比较；
- [ ] 分解策略收益与 Beta、方向、波动和资金利用率暴露。

### 退出条件

- 策略结论不依赖单一时间区间或单一参数点；
- 样本外结果和调参过程可复现；
- 在保守成本与执行假设下仍有可解释优势；
- 高回报报告不能单独作为上线证据；
- 失败结果与成功结果同样保留，避免选择性报告。

## 13. G9：仓库、依赖、测试与 CI 治理

**映射阶段：R0/R6；优先级：P2。**

### 13.1 仓库卫生

- [√] 已跟踪的 `reports/*` 历史回测产物移出 Git 索引，`.gitignore` 挡住后续生成物；
- [ ] `live_status.json`、SQLite 状态、routing log 和普通运行报告默认不跟踪；
- [ ] 需要保留的基线报告移动到明确的 fixtures/baselines 目录；
- [ ] 删除 `.codex_patch_probe` 等临时探针；
- [√] 清理动作单独提交，不与业务逻辑修改混合；
- [√] 清理前确认当前大量未提交改动的归属，禁止覆盖用户工作。

### 13.2 依赖与任务入口

- [ ] 区分 runtime、development 和 optional 依赖；
- [ ] 明确 lock 文件生成工具、Python 版本和支持平台；
- [ ] 提供统一的 test、lint、typecheck、security 和 regression 命令；
- [ ] 验证 README 声明的 Python 3.9+，或收紧支持版本；
- [ ] 配置 CI 兼容矩阵；
- [ ] 增加覆盖率报告和关键模块最低门槛；
- [ ] 增加依赖漏洞和密钥扫描。

### 13.3 目录与能力边界

- [√] 移除未接入主链路的 `archive/` 旧实现和根目录 `all.txt`；
- [ ] 标记 production、research、generated 和 deprecated 目录；
- [ ] 研究脚本复用正式回测执行与成本模型；
- [√] `models/`、`dashboard/` 空壳包已删除（无代码引用，未来若需 ML/Dashboard 能力再重新设计）。

### 退出条件

- 干净检出后可以用一条文档化命令完成环境安装和测试；
- CI 自动执行测试、lint、类型和安全检查；
- 运行生成物不再污染 Git 状态；
- 新成员可以明确判断哪个入口和目录受正式支持。

## 14. G10：小额真实资金灰度评审

**映射阶段：R8；优先级：最终放行。**

达到本阶段不代表自动允许实盘，必须经过独立人工评审。

### 前置门槛

- [ ] G1–G9 的退出条件全部达到；
- [ ] P0/P1 未关闭项为 0；
- [ ] Sandbox/Paper 连续运行至少 2–4 周且无未解释差异；
- [ ] 回滚、kill switch、备份恢复和人工接管已演练；
- [ ] 使用专用账户、最小权限 API 且提现权限关闭；
- [ ] 风险负责人明确签字批准限额和停止条件。

### 灰度范围

- 单交易所；
- 单账户类型；
- 单 symbol；
- 最小订单规模；
- 禁止自动扩大限额；
- 有人值守；
- 每日人工对账；
- 任一未知订单、未知仓位或对账失败立即停止新增风险。

### 扩容原则

每次只扩大一个维度：资金、symbol、交易所、账户类型或无人值守时长。每次扩容都必须重新执行风险评审和观察期，不能因为收益良好自动放宽限制。

## 15. 建议任务拆分与提交顺序

| 顺序 | 建议任务 | 主要产物 | 依赖 |
| --- | --- | --- | --- |
| 1 | SAFE-01 安全默认模式与凭据治理 | CLI、启动检查、测试、部署文档 | G0 |
| 2 | ORD-01 结构化下单结果 | result contract、错误分类、测试 | G0 |
| 3 | ORD-02 OrderStore 接线与 client ID | 持久化、幂等测试 | ORD-01 |
| 4 | ORD-03 unknown/partial/recovery | 查询、恢复、故障注入测试 | ORD-02 |
| 5 | ACCT-01 权威账户快照 | cash/locked/positions/orders/equity | G0 |
| 6 | ACCT-02 周期与日终对账 | reconciliation report、阈值 | ACCT-01、ORD-03 |
| 7 | ARCH-01 统一领域模型 | event/order/fill/snapshot contract | ORD-03、ACCT-01 |
| 8 | OPS-01 状态机、原子快照与告警 | engine states、snapshot schema | ARCH-01、ACCT-02 |
| 9 | TEST-01 故障注入与恢复矩阵 | timeout/partial/crash/rate-limit tests | ORD-03、OPS-01 |
| 10 | BT-01 指标与 JSON 契约 | MetricResult、schema、兼容层 | G0 |
| 11 | BT-02 成交账本与资金桥接 | fills、closed trades、reconciliation | BT-01 |
| 12 | ENG-01 仓库与 CI 治理 | clean index、CI、统一任务入口 | G0 |
| 13 | PAPER-01 Sandbox/Paper 长跑 | 每日对账、事件记录、运行报告 | G1–G5、ENG-01 |
| 14 | RES-01 样本外与稳健性 | walk-forward、敏感性、基准归因 | BT-02 |
| 15 | LIVE-01 小额实盘评审 | 审批记录、限额、回滚与观察计划 | PAPER-01、RES-01 |

## 16. 每个任务的完成定义

每个任务只有同时满足以下条件才可标记完成：

- 有明确目标和非目标；
- 有输入、输出、状态和失败语义；
- 先提供能够复现问题的失败测试或 fixture；
- 实现不覆盖无关工作区改动；
- 单元、集成和相关固定回归全部通过；
- 关键财务或订单性质有对账测试；
- 配置和 schema 变化有版本与迁移说明；
- 监控、日志和错误信息可用于定位问题；
- 文档只描述已经实现的能力；
- 保存验收证据、测试摘要和已知限制；
- 无法计算或尚未建模的能力明确返回状态，不以默认值伪装成功。

## 17. 立即执行的下一批工作

建议下一批只处理以下四项，不同时展开高级指标、ML 或新策略：

1. **SAFE-01**：将 sandbox 设为默认、移除命令行密钥、增加真实模式启动门槛；
2. **ORD-01**：让 `LiveBroker.submit_order()` 返回结构化结果并停止吞掉失败；
3. **ORD-02**：将 `OrderStore` 和 client order ID 接入下单前持久化；
4. **TEST-01A**：建立“交易所已接单但客户端超时”的故障测试，证明不会重复下单。

这四项完成后，再并行推进账户快照/对账和订单恢复。任何策略收益优化都应等待 P0 订单闭环完成。

## 18. 项目级成功标准

本轮整改的成功标准不是回测收益提高，而是：

- 同一信号不会产生重复逻辑订单；
- 任一订单、成交和持仓事实都可追溯；
- 超时、崩溃和重启后系统能恢复或安全停止；
- 账户事实不完整时不会继续增加风险；
- 回测、replay 和实盘共享核心语义；
- 回测 PnL、成本、现金、持仓和权益能够对账；
- 样本外证据和成本敏感性足以支持策略判断；
- 运行状态可监控、关键事件可告警、故障可演练；
- 仓库、配置、依赖、测试和发布过程可复现；
- 在全部放行门槛满足前，系统始终保持 sandbox-only。
