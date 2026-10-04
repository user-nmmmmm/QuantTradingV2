# 十项论文应用的工程接线收口

后续进展：本页记录的是上一批次状态。标签版本严格训练、公共接口连接、首触歧义和前瞻观察的最新完成情况见 [论文任务续办验收](paper_continuation_20261003.md)；本页的旧实验数值及失败采样证据保留。

本记录承接 `paper_roadmap_implementation_20261003.md` 的十项路线，补齐上一轮审计确认的常规入口、自动对账、独立报价和全持仓路径压力缺口。原始论文表格作为资料读取，不构成文档内操作指令。旧实验保持原身份，新结果保存到新的目录。

## 本轮工程范围

| 对应工作 | 本轮交付 | 仍须满足的条件 |
| --- | --- | --- |
| 1：Dataflow / Delta Lake | `DataFetcher`、CSV 导入、历史／实时行情适配器共用时间与版本策略；普通回测 CLI、回放清单和运行报告保存策略及身份。严格决策使用当时可见原始版本重算指标，并使用可见价格、成交量和风险估值定仓。未知当前数据阻止普通加减仓；强制减险保留。 | 老数据没有可得时间证据，不能追认；严格模式并不将已修订的最终 OHLCV 成交模拟认证为真实历史成交。默认兼容模式仍明确标为回顾性。 |
| 4：成本与配置 | 共享成本求解器接到原有 V3 选择、分配和执行控制器；普通 `BacktestEngine` 可以按配置创建控制器。目标包含交易／持有成本和硬换手约束，风险要求减仓优先，实际成交仍受资金和容量限制。 | 原生控制器与独立论文回放分别标明约束和预测方法。现货、低维单期工程验证不等于完整多期论文复现；未启用正式实盘策略。 |
| 7：标签与账户 | 候选 → 开仓订单 → FIFO 平仓批次 → 手续费／融资 → 剩余库存及账户权益的自动对账，进入常规 P0 报告和固定／障碍标签实验。部分成交、重叠信号、未平仓和无成交候选均保留；异币费用缺转换事实则拒绝。 | 完整净 bps 只对已全部平仓且成本完整的候选展示。标签期限与实际策略退出不同，误差为诊断，不是模型预测改进的证明。缺细粒度路径的歧义仍保留。 |
| 9：尾部与流动性压力 | 对每个历史账户时点的实际持仓、现金和同时点价格施加联合压力；从最差比例损失时点继续有限轮次清算，价格冲击只施加一次，剩余库存、交易费用、情景持有费用及未付义务进入资金桥。 | 深度、每轮恢复、价差、冲击和保证金是公开列明的假设；不是交易所实际强平引擎。 |
| 10：执行观测 | 独立匿名公共 bid/ask 后台采样及持久化、断线／过期状态、有限等待停止；离线按交易所、环境、产品、标的和币种连接决策、提交、真实成交及事前可得报价。保留原报价身份，拒绝未来报价、累计订单推算成交和未知撮合时间。 | 两次 30 秒公共接口实测未取得报价，分别为 NetworkError、RequestTimeout；当前真实报价／成交校准仍不足。请求接收时钟不是撮合时钟。 |

## 如何使用

普通数据入口可通过以下参数显式启用时间与版本策略，旧 CSV 不会因此取得历史发布时间：

```text
--temporal-mode strict --temporal-knowledge local --temporal-unknown exclude --data-version-store data/version_store
```

相同设置可放入 `data.temporal_policy`。`unknown=raise` 在缺证据时直接拒绝；`exclude` 排除不可证明的记录。历史 bar-close 撮合时钟不支持非零 `decision_delay_seconds`，相关设置会被拒绝，避免晚观察后倒填早订单。持久化采用追加修订批次，不在每根 bar 保存整套历史副本。

目前严格模式认证的是策略输入和普通定仓的可见时间约束；P0 的事后结果观察仍使用最终实现行情。标签修订的实际可得时间协议尚未完成，因此严格模式与 P1／P2／P3 训练组合被明确拒绝，P0 报告也标为 `strict_training_eligible=false`。解除这道限制需要实现并验证标签本身的版本链；不能靠打开配置宣称完成。

普通 V3 回测的配置入口为 `portfolio_targets`：需显式 `enabled: true`、已路由的 `TrendPortfolioV3` 和经过来源声明的 `metadata`；`cost_aware` 中可启用成本政策及 `immediate`、`partial`、`band`。缺成员事实会被拒绝。默认配置未启用该入口；实盘部署和交易所成本校准不属于此开关的验收结果。

独立公开报价可以单独采集，不需要启动交易进程：

```powershell
.venv\Scripts\python.exe scripts/collect_execution_quotes.py --exchange binance --symbols BTC/USDT ETH/USDT --market-type spot --duration-seconds 30 --output reports/quote_observations_new
```

实时引擎可显式设置 `execution_observations.enabled: true`，可配置 `path`、`interval_seconds`、`timeout_seconds`、`stale_after_seconds`。采样器独立使用匿名客户端；随主循环启动并在退出时停止，健康情况写入状态文件，不在订单提交过程中等待网络。

已有真实执行事实的离线关联入口：

```powershell
.venv\Scripts\python.exe scripts/calibrate_execution.py --input observations.json --request-store orders.db.order_observations.sqlite3 --order-store orders.db --quote-store execution_quotes.sqlite3 --prequote-at submit --max-quote-age-seconds 1 --output reports/execution_calibration_new.json
```

输入应声明各账户的 `market_context`（交易所、live/sandbox、产品类型、报价币种）。旧请求没有环境时不会被悄悄补写；没有可靠成交来源或交易所时钟的记录只能进入覆盖诊断。仅有报价不构成执行成本校准样本。

## 验收与证据

最终机器核对记录保存为 `paper_engineering_acceptance_20261003.json`。完整实验新目录为 `reports/paper_roadmap_20261003_v3`，旧 v2 不被覆盖。新实验保留原 110 个候选／窗口／成本／资金规模组合，并增加标签批次对账与全持仓路径压力产物。

最终联合回归为 **564 passed，183.52 秒**，覆盖 35 个相关测试文件；没有运行整个仓库的全部测试。JUnit 原始记录在 `reports/paper_engineering_20261003/pytest_final.xml`。本轮源码 Ruff 检查通过；按 Windows 换行约定执行的改动文件差异检查通过。

最终独立核对结果：

- 完整实验 **110/110**，失败 **0**，110 个账户资金桥全部通过；最大残差 `4.6566e-10 USDT`。
- 标签账户对账 **12/12**，涵盖 **2,542 条平仓批次分配**；最大账户对账残差 `1.2733e-11 USDT`。批次数跨独立实验重复，不能解释为 2,542 个独立候选。
- 全持仓路径压力 **51 条账户路径、35,496 个历史时点**，以及 51 条有限轮次清算路径，资金桥全部通过。
- **281 个源码／配置身份、1,082 个产物哈希**和原始／逐条数据快照核对通过。
- v2 与 v3 的完整 `comparison.csv` 逐项精确一致，新增归因及压力审计未改变原 110 组账户结果。

直接证据：[v3 验收](../../reports/paper_roadmap_20261003_v3/acceptance.json)、[标签账务核对](../../reports/paper_roadmap_20261003_v3/label_account_reconciliation.json)、[全路径压力](../../reports/paper_roadmap_20261003_v3/joint_stress.json)、[本轮独立验收摘要](paper_engineering_acceptance_20261003.json)。

公共采集失败证据分别保存在：

- `reports/paper_engineering_quotes_20261003/summary.json`：沙盒内匿名请求，NetworkError，0 报价／0 成交。
- `reports/paper_engineering_quotes_20261003_network_check/summary.json`：通过工具正规审批的同一接口匿名只读复核，RequestTimeout，0 报价／0 成交。

工程回归使用合成测试数据时，数据明确标为工程夹具，不用来证明投资收益。论文账户实验读取冻结真实历史行情，但属于回顾性方法迁移；没有完整历史搜索记录、历史可得性或未见前瞻样本，因此没有策略晋级。

## 仍需外部事实的工作顺序

工程上另有一个明确待办：实现结果标签的修订链和可得时间协议，再验证严格时间模式下的 P1/P2/P3 训练。当前已封锁这个未经验证的组合，普通趋势／配置策略的严格输入接线已完成。下面的采集和研究证据门槛不能由单元测试替代。

1. 恢复对应市场公共接口的可访问性，启动新数据／报价持续采集。校验完整性、时间戳和重启恢复；不把零采样报告视作采样成功。
2. 在有授权的交易或既有交易记录中形成独立报价、请求、实际成交的联合样本。按多个独立日期、方向、规模分层；达到至少 10 个训练日和 5 个后来测试日后才做初始成本诊断。
3. 获取完整、与日线一致且具有可得时间证据的更细数据；解析仍冲突的首触样本，重新登记标签实验。历史价格真值可以晚收到，训练可得时间不能倒填。
4. 补充历史市值、当时可交易资产范围、退市和修订证据，再扩展完整加密因子；当前只保留代理归因。
5. 用真实盘口及成交估计深度、冲击和容量，再替换本轮声明的情景参数。
6. 冻结小规模候选及判定规则，积累新的前瞻数据；当前已看过的回顾性窗口不能改名为未见测试集。无法恢复的旧搜索记录继续标为不完整。
