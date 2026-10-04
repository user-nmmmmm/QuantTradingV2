# 前端工程说明 · 本地研究工作台 v2

更新日期：2026-10-03。工作台覆盖账户监控、离线回测、交易诊断、数据检查、实验档案与稳健性研究。启动步骤见 [README.md](README.md)。

## 技术与模块

浏览器使用原生 ES Modules、CSS tokens 和 SVG；Python HTTP 提供同源接口。前端没有 npm 构建步骤、运行时 CDN 或远程字体。运行策略仍需要项目 Python 依赖。

| 模块 | 职责 |
| --- | --- |
| web_index.html | 七个语义化工作区、稳定 DOM 挂载点和无障碍入口 |
| web_workspace.js | hash 路由、标题、视图生命周期与动态 import |
| web_api.js | JSON 请求、超时、取消过期请求与错误解码 |
| web_app.js | 账户快照、有效性判断、持仓和告警 |
| web_visual.js | K 线、权益/回撤、报告目录、键盘交互和会话事件 |
| web_charts.js | 研究面板共享 SVG 折线、指标卡和表格 |
| web_research.js | 单次回测参数、后台任务、月度分析、执行成交分页与导出 |
| web_strategy.js | 策略族表单、预设、参数校验与配置差异预览 |
| web_lab.js | 闭合交易诊断、实验档案、备注收藏与多实验对比 |
| web_market.js | 技术指标图、数据质量和计算口径 |
| web_robust.js | Walk-forward 参数、任务、分窗结果和参数热力图 |
| web_theme.js / web_theme.css | 首屏主题、浅深 tokens 与组件颜色 |
| web_style.css / web_visual.css | 基础监控与图表样式 |
| web_workspace.css / web_lab.css | 研究布局、导航、表单、档案与响应式组件 |
| web.py | 静态资源白名单、HTTP 路由、请求边界和 CLI |
| backtest_jobs.py / backtest_worker.py | 共享单任务调度、日志/超时、子进程和快照装载 |
| experiment_store.py | SQLite 状态、查询、研究记录与预设 |
| strategy_presets.py | 策略 schema、验证、配置覆盖与 SHA-256 快照 |
| visual_data.py / report_analysis.py | 有界 CSV、报告发现、执行成交、权益统计和缓存 |
| research_analytics.py | 闭合交易、记录式诊断、信号漏斗和阻断证据 |
| market_analysis.py | 技术指标、日线质量、共同区间与提交校验 |
| experiment_compare.py | 2–4 个报告的时间对比、参数差异与口径提示 |
| robust_research.py / robust_worker.py | 有界 Walk-forward、真实引擎和证据产物 |

CSS 按基础、图表、主题、工作区及研究扩展顺序加载。新增组件沿用 --bg/--surface/--ink/--muted/--accent 等 tokens，不继续叠加无关营销样式。

## 视图与事件

路由为 #overview、#backtest、#market、#operations、#experiments、#data、#robust，支持前进/后退。旧 #positions/#activity/#alerts 保留映射。普通页内锚点保持当前视图。仅 .workspace-view[data-view] 参与隐藏，根节点 data-view 只表示当前视图。惰性模块初始化应幂等，重试不能重复绑定事件。

| 事件 | detail | 用途 |
| --- | --- | --- |
| dashboard:view | {view} | 懒加载、重绘和轮询开关 |
| dashboard:status | 账户快照 | 状态与会话观察 |
| dashboard:theme | {theme} | 重绘当前可见图表 |
| dashboard:refresh | 无 | 刷新当前工作区 |
| dashboard:backtest-complete | {run_id} | 刷新目录并选择报告 |
| dashboard:backtest-loading/error | 加载/错误上下文 | 清除旧分析与成交 |
| dashboard:backtest-loaded | 完整报告 | 分析、诊断和成交共用当前报告 |
| dashboard:backtest-current | 无 | 惰性模块请求重播当前报告 |
| dashboard:clone-experiment | 参数对象 | 将档案参数填入下一次实验 |
| dashboard:market-loading/loaded | 行情上下文 | 指标与 K 线保持同一标的/范围 |
| dashboard:research-open | {id} | 打开已完成的稳健性结果 |

文本通过 textContent 渲染。缺失显示“—”，真实零保留零；读取中、失败、空数据、预热不足和禁用状态分开。序号隔离旧响应；超时必须显示可重试错误，不能一直停在“读取中”。

## HTTP 接口

API 同源，响应 no-store。写请求正文上限 **32 KiB**，使用 application/json 和 options 返回的 X-CSRF-Token，并校验 Host、Origin、Fetch Metadata。客户端不能提交命令或任意文件路径。静态资源白名单采用 ETag 与 no-cache 重验证。

| Method / path | 参数 | 返回或用途 |
| --- | --- | --- |
| GET /api/status | 无 | 账户快照和有限会话轨迹 |
| GET /api/markets | 无 | {markets} |
| GET /api/candles | symbol,limit | 历史 OHLCV，保留原接口 |
| GET /api/market-analysis | symbol,limit | OHLCV、逐点指标、最新值、公式和质量 |
| GET /api/data-quality | 可选 symbols 逗号列表、start,end | 缺失/重复/乱序、共同区间和选择校验 |
| GET /api/backtests | 无 | {runs}，最近最多 60 个报告及来源/参数 |
| GET /api/backtest | id | 权益统计、月度、基准对齐、参数和口径 |
| GET /api/backtest-trades | id,page,page_size | 执行成交分页 |
| GET /api/backtest-diagnostics | id,page,page_size | 指标状态、闭合交易、费用、归因、回撤和漏斗 |
| GET /api/backtest-options | 无 | 会话令牌、标的、默认值、开关和边界 |
| GET /api/backtest-jobs | 无 | {jobs}，最近任务 |
| POST /api/backtest-jobs | 基础参数及可选 strategy | HTTP 202 {job} |
| POST /api/backtest-jobs/cancel | {id} | 取消指定任务 |
| GET /api/strategy-catalog | 无 | 策略族与参数 schema |
| POST /api/strategy-preview | {strategy} | 规范化参数、配置差异与基础 hash |
| GET /api/strategy-presets | 无 | {presets} |
| POST /api/strategy-presets | name,strategy，可选 id,description | 保存通过验证的预设 |
| POST /api/strategy-presets/delete | {id} | 删除预设 |
| GET /api/experiments | q,status,tag,kind,favorite,limit,offset | 分页档案，每页最多 100 条 |
| GET /api/experiment | id | 单条实验 |
| POST /api/experiments/metadata | id 及 name,tags,notes,favorite 子集 | 更新研究记录 |
| GET /api/compare | ids，2–4 个逗号分隔标识 | 净值、回撤、参数差异和统计 |
| GET /api/research-options | 无 | Walk-forward 默认参数与预算 |
| GET /api/research-jobs | 无 | {jobs}，研究任务 |
| GET /api/research-result | id | 已完成研究结果 |
| POST /api/research-jobs | 基础参数和完整 Walk-forward 几何 | HTTP 202 {job}，共享执行调度 |
| POST /api/research-jobs/cancel | {id} | 取消研究任务 |

基础参数包括 source、symbols、start、end、capital、slippage_bps、seed。strategy 可选；规范形态例如：

    {
      "family": "trend_breakout",
      "parameters": {
        "entry_window": 30,
        "exit_window": 10,
        "use_obv": true
      }
    }

策略族为 configured、trend_breakout、mean_reversion。仅允许 schema 中的研究参数，并校验入场/退出窗口关系。均值回归提供 ATR 价格占比、RSI 阈值和 RSI 确认开关，参数在隔离 worker 内进入现有策略构造流程。

## 持久化、配置与调度

档案位于 reports/.dashboard/experiments.sqlite3，使用 WAL 和短事务，保存状态、参数、配置 hash/差异、日志与用户名称、标签、备注、收藏。最近 30 个任务载入服务内存，历史档案不会因为这个内存上限被删除。网页档案每页 15 条；预设最多 100 个，备注最多 4,000 字符，标签最多 12 个、各 40 字符。

提交时复制基础配置，应用通过验证的研究覆盖，以独占创建写入 reports/.dashboard/configs/<id>.yaml 并保存 SHA-256。worker 核验 hash 后装载；结束时报告目录另存 config.snapshot.yaml 和 dashboard_job.json。已提交配置不随后续表单编辑变化，网页预设不修改 config/params.yaml。

任务状态为 queued → running → succeeded/failed/timed_out/cancelled。服务重启把遗留 queued/running 标为 interrupted，不会自动续跑或误报成功。正常关闭会清理活动子进程。普通回测和 Walk-forward 共用一个执行槽，忙时新请求返回冲突。默认超时 1,200 秒，日志最多 160 行、各 2,048 字符，并节流持久化。

普通任务由 dashboard.backtest_worker 装载配置后运行现有 main.py，固定日线与 compact 报告。输出 reports/web_<UTC时间>_<标识>/，仅成功原子发布任务标记的网页目录可以作为成功报告读取。

配置快照固定配置内容，普通 compact 并未冻结全部代码与行情身份；复制参数创建新实验不等于严格重放。稳健性任务额外保存实际数据和研究证据，仍需与完整不可变运行环境区分。

## 统计、交易与证据

总收益为 E_last/E_first−1，回撤为 E_t/max(E_0..E_t)−1，CAGR 为 (E_last/E_first)^(365.25/elapsed_days)−1；零时长或溢出返回 null。周期收益取相邻有效权益点，不假设每天一个点；周期波动率是样本标准差、不年化。

月度按 UTC 自然月衔接前一个有观测月份末值。首月可能不完整，缺失月份不补零，跨缺失月份的变化归入结束月。基准匹配首尾时刻才计算区间收益和超额百分点，比较图保持同样对齐语义。

执行成交 trades.csv、闭合往返交易 closed_trades.csv、有因果标识的入场事件链是三个不同总体。

| 诊断 | 口径及未知行为 |
| --- | --- |
| 胜率 | net_pnl>0 的闭合交易 / 全部有效闭合交易，平盘仍在分母 |
| Profit factor | 正净盈亏之和 / 负净盈亏绝对和；无亏损时未知，不生成无穷大 |
| 期望盈亏 | 闭合净盈亏均值，保留不足样本/部分有效行状态 |
| 持有时长 | 记录的 UTC 出入场差，缺失或负时长不进入均值 |
| MAE/MFE | 保留记录字段原有单位，不凭闭合交易重建路径或换算收益率 |
| 费用 | 记录的手续费和滑点；不重复扣除已体现在填单价毛盈亏中的滑点 |
| 手续费/毛收益 | TotalCommission/GrossPnL，仅当记录的毛盈亏严格为正 |
| 集中度 | Top-N 净盈亏贡献/正的总净盈亏；HHI 使用正盈亏份额平方和 |
| 最大回撤时长 | 最大深度回撤事件持续天数，不是最长持仓时长 |
| Sharpe/Sortino/Calmar/年化波动率 | 只投影实际落盘数值、状态和样本；不推断年化时钟，未记录或明确不足时未知 |
| 敞口 | 优先记录的时间加权口径，否则保存的观测加权口径，返回精确字段来源 |

漏斗固定为 data → warmup → signal → routing → risk → execution。缺失阶段为 unknown，不从平坦权益、零闭合交易或退出原因推测通过或受阻。明确记录的零才显示零。指标携带 metric_status、source、sample_size、reason。

no_trade.blockers 仅包含明确健康开仓门控、正的形态抑制计数和生命周期终止，并保留快照/计数/时间范围。activity 最长静默区间基于闭合交易退出时间，不代表没有委托或未平仓头寸。报告活动一致性 findings 与明确门控事实分开，不宣称整段静默的唯一原因。

## 技术指标与数据质量

对完整受限历史计算后截取最多 240 个显示点。无预热值为 null；不前向填充行情。指标按有效观测计数，不改变交易引擎现有约定。

| 指标 | 初始化与公式 |
| --- | --- |
| SMA20/50 | 完整窗口收盘均值 |
| EMA12/26 | 首个完整窗口 SMA 初始化，之后 α=2/(N+1) |
| RSI14 | 14 个价格变化的 Wilder 均值；平盘50、仅涨100、仅跌0；需15个收盘 |
| MACD | EMA12−EMA26；Signal 为有效 MACD 的 SMA 初始化 EMA9；Histogram 为差值 |
| Bollinger20 | SMA20 ± 2 倍总体标准差，ddof=0 |
| ATR14 | TR 含高低差及前收盘跳空，首 TR 为高低差；14项均值初始化后 Wilder |
| ADX14 | 严格互斥 ±DM、Wilder 平滑和 DX，再作 Wilder14；首次需28个观测 |
| Stochastic | 14窗口 Fast %K，%D 为 SMA3；零高低差为null |
| OBV | 从0开始，按收盘涨跌加减当期量 |
| ROC12 | 100×(C_t/C_(t−12)−1)，单位为百分数 |
| CCI20 | (典型价−典型价SMA20)/(0.015×平均绝对偏差)，零偏差为null |
| Williams %R14 | −100×(窗口最高价−收盘)/(窗口最高价−窗口最低价) |

质量检查以 UTC 零时每日一根为目标，区分重复、乱序、无效时间、非法 OHLCV、非日线行和缺失日期。common_range 只是有效边界交集，complete 表示逐日覆盖。common_usable_range 是各品种共同的最长连续干净区间，同长度取较新者。

提交调用 validate_data_selection：所选日期内缺口、重复、乱序、非法 OHLCV、非日线行均不通过；区间外问题不会自动阻止干净选择。无法归属日期的错误时间戳会阻止严格验证。数据页不自动下载或修改原文件。

## 对比与稳健性

对比选择2–4个报告，分别把首个有效权益归一化为100，按日历时间绘图，不填补日期。每条曲线约800个显示点并保留最深回撤，统计用全样本。来源、标的、费用或区间差异明确提示，缺少身份的旧报告不自动认定完全可比。

Walk-forward 用真实引擎执行趋势突破参数网格，3–6候选、最多4个评估窗口。训练30–180根，验证/测试各10–90根，purge1–30根，额外满足策略预热；步长=max(warmup,test)。

每窗只按验证期 TotalReturn 或 SharpeRatio 选参数，测试不参与选择。首次划分用于完整预热，全部验证值不可用则跳过。只使用请求日期内末尾所需日线，测试窗不重叠。各窗从同样资金开始并结束平仓，曲线只复合实际 OOS 窗口收益、绘制窗口边界，不伪造间隙日收益。热力图按登记参数顺序诊断，不据测试结果重新选优。

reports/research_<UTC时间>_<标识>/ 保存网页 result.json、完整 walk_forward.json、data/*.csv、evidence/registration.json、evidence/attempts.jsonl 和配置快照。该流程为回顾性 Walk-forward，不声明未触碰独立留出验证。

## 资源与性能

- 账户前台15秒刷新、隐藏暂停。活动任务约2秒检查、空闲约15秒；切视图停止无关轮询，计算继续。
- 指标、诊断、档案、数据质量和稳健性按视图加载。普通请求默认20秒超时，批量质量检查可用60秒。
- 指针/resize 经 requestAnimationFrame 合帧，隐藏图表进入时再绘。标签按实际尺寸补偿字号，窄屏减少日期刻度。
- 执行成交及闭合交易默认25行/页、最多100行，表格独立滚动，不为整个CSV创建DOM。
- 报告CSV每份16MiB；权益/基准20,000行，执行/闭合交易100,000行。无效行也占预算；任务标记32KiB，诊断JSON8MiB，稳健性结果4MiB。
- 行情每份16MiB/20,000行；质量请求最多32标的、合计64MiB。缺失日期样本最多20个。
- 权益与行情缓存各8条/16MiB序列化数据，诊断8条/8MiB。按所有依赖文件的标识、大小、纳秒时间失效，创建/删除也失效；返回独立值。
- 数据库查询分页，任务日志和单项记录限长。持久档案总量没有自动清除策略，按本地研究保存需求维护。

这些是实际资源策略，不是跨硬件性能评分。延迟应分别衡量HTTP投影、SVG/DOM和引擎子进程。

## 验证与维护

    .\.venv\Scripts\python.exe -m pytest tests/test_dashboard_web.py tests/test_dashboard_jobs.py tests/test_dashboard_analysis.py tests/test_dashboard_experiments.py tests/test_dashboard_lab.py tests/test_dashboard_research_analytics.py tests/test_dashboard_market_analysis.py tests/test_dashboard_robust.py -q
    .\.venv\Scripts\python.exe -m ruff check dashboard
    Get-ChildItem dashboard\web_*.js | ForEach-Object { node --check $_.FullName }
    node --test tests/dashboard_budget.test.mjs tests/dashboard_frontend.test.mjs tests/dashboard_charts.test.mjs tests/dashboard_lab.test.mjs tests/dashboard_robust.test.mjs

稳健性测试使用新增的专用测试文件及真实引擎冒烟运行。某些沙箱不允许默认临时目录/回环socket，可把TEMP/TMP指向工作区独立临时目录，并在允许本地HTTP的环境运行接口测试；环境错误不能视为通过。

Python定向验证覆盖公式、预热、质量、未知/零区别、统计总体、缓存、持久化、配置隔离、接口与生命周期。Node回归覆盖竞争、超时、路由和图表。浏览器验收包含1440/1024/390px、浅深主题、七区导航、键盘、服务重启、档案编辑、预设、取消/失败/重试及不同口径对比。新增指标先定义分母、单位、时钟、缺失行为，再接UI。

## v2 验证记录 · 2026-10-03

- Python dashboard 定向回归：**134 passed，65 subtests passed**。覆盖旧接口兼容、真实 HTTP 边界、持久化、预设、报告投影、数据质量、指标公式及稳健性适配器。
- Node：**19 项通过**。除原有生命周期与图表行为外，增加旧报告直接打开、UTC 日期、稳健性幂等初始化、乱序刷新、隐藏轮询及失败清理。
- Ruff、全部网页 JavaScript 语法检查和 dashboard Python 编译检查通过。该范围不等于全仓库策略回归。
- 趋势突破与均值回归真实子进程分别完成，基础 YAML 字节保持不变；重建任务管理器后配置和成功状态恢复。
- 浏览器完成“保存预设 → 提交合成回测 → 自动载入报告”，本机该次任务约 12.1 秒。实验档案、诊断、指标切换和双实验比较已实际操作。
- 本地 BTC/ETH/AAVE 与合成 BTC/ETH/BNB 的默认滚动研究分别执行 6 候选 × 3 窗口、36 次真实引擎运行，保存数据快照及注册证据。零收益/零交易如实显示；这不是收益能力证明。
- 浏览器联调修复长表单按钮不可触达、旧报告目录限制、缺失参数混入旧值、行情失败残留加载、移动端网格与长文本溢出，以及无时区日期在 UTC+8 的标签偏移。
- 最终服务重启到默认 8765 端口，实际确认名称、标签、笔记、成功任务与预设恢复，复制参数正确。1440/1024/390px 浅深主题及七工作区检查完成；研究结果表格内部滚动，页面无横向溢出，最终浏览器控制台无错误。
- 代码稳定后从网页提交本地 BTC/ETH/AAVE 研究 `research_20261003_155626_a5551db8`：约 7.7 秒完成，6 候选、3 测试窗、36 次引擎执行，`data_unchanged=true`、`source_unchanged=true`。这些是本机功能验收结果，不是跨机器性能承诺。

档案编辑保存的是本机 SQLite 数据；复制参数采用当前基础配置及已记录覆盖，不宣称严格回放历史环境。对比额外核对基础配置与实验快照 SHA-256；缺失身份的旧报告提示口径未知。


## 页面精修与资源预算 · 2026-10-04

本轮继续沿用 `DESIGN.md` 的青绿色研究仪器风格，统一字号、边界、分隔和控件反馈。字号集中在 `web_theme.css`：说明 11px、字段 12px、正文 13px、正文强调 14px、区块标题 20px、关键读数 24px、页面标题 28–36px；控件高度 44px、控件圆角 7px。页面结构由 `web_workspace.css` 负责，研究组件由 `web_lab.css` 负责，不新增样式层或外部字体。

### 信息层次与交互

- 回测页增加配置、收益、风险、成交四个章节入口；风险诊断在原始成交明细之前。长表单保持自然滚动，个人预设管理按需展开。
- 诊断默认显示 6 项主指标，其余 17 项按交易表现、成本与回撤、风险与敞口分组。深度指标、来源证据、退出归因和回撤事件首次展开才创建内部 DOM；翻页保留展开状态，切换报告重置。
- 技术指标默认只显示当前视图读数，全部 19 项快照在原生 `details` 中保留。零值与未记录值继续区分；切换失败会清空当前和全部读数。
- 稳健性表单按数据范围、参数搜索、滚动窗口、执行设置分组；桌面双列，手机单列。隐藏的执行字段验证失败时自动展开，确保输入可聚焦。
- 数值按明确字段类型格式化，金额、价格、数量、百分比、UTC 时间和持仓时长分别显示。极小金额保留有效值，完整原值保留在单元格 `title`。原始成交接口仍返回 CSV 文本，只有 `csv: true` 且属于数值白名单的合法十进制字符串才转换；ID 和普通文本保持原样。
- 任务轮询仅在显示内容变化时重建行；日志单独更新。刷新保留操作焦点，研究日志保留展开状态，取消请求进行中防止重复提交。
- 宽表拥有独立滚动区、固定表头、键盘焦点和区域名称；K 线范围与告警筛选同步 `aria-pressed`。路由切换聚焦页面标题，移动导航自动显示当前项。

### 可执行的体积约束

`tests/dashboard_budget.test.mjs` 在本地直接读取文件，递归统计默认总览的静态模块依赖，明确排除按视图触发的动态导入。预算为：默认入口 HTML + CSS + 静态 JS **180 KiB**，所有网页 JS **180 KiB**，所有 CSS **90 KiB**；同时检查研究模块继续按需加载以及入口没有外部脚本/样式依赖。

本轮最终未压缩源码体积约：默认入口 **161.5 KiB**，全部 JS **147.6 KiB**，全部 CSS **77.2 KiB**。这是本地文件体积，包括原有功能；不包含 API 响应、图标、协议开销，不等于加载时间、LCP 或跨机器跑分。继续保留有界曲线、分页表格、隐藏页暂停轮询、同键请求取消和 rAF 合帧。

### 本轮验证范围

- **37 项 Node 测试通过**，覆盖资源预算、延迟渲染、CSV 数字与 ID 边界、UTC 时间、23 项诊断完整性、旧接口兼容、乱序响应、轮询焦点以及折叠表单提交。
- **5 项 dashboard HTTP 测试通过**；全部网页 JavaScript 语法检查通过。本轮没有修改回测引擎和策略计算，不将上一轮 134 项后端结果算作本轮重新执行。
- 实际查看 1440 / 1024 / 390px 布局与浅深主题。390 和 1024px 的七个工作区逐页检查，页面整体无横向溢出，移动导航当前项可见；表格在自身容器内滚动。
- 浏览器确认诊断默认 6 项、展开后新增 17 项，技术指标保留 19 项；从导航用键盘进入数据页后，Tab 可到“重新检查”操作。控制台未出现错误。
