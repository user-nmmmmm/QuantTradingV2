# 自动化回测操作方案

- 更新日期：2026-09-20。
- 对应任务：`SYS-19`，工程入口已实现，真实持续运行验收仍待证据；当前任务登记为 41 项、24 项已验收、17 项开放，整体保持部分完成。
- 当前依据：[自动化配置](../config/automation.json)、[编排入口](../scripts/run_automation.py)、[裁决入口](../scripts/evaluate_matrix.py)、[研究执行入口](../scripts/run_research_automation.py)。
- 原始草案仅保存在本地受限归档，不随公开源码发布；当前操作以本文和代码为准。

证据范围：第 7–8 节的调度、真实数据和研究结果是 2026-09-20 的历史记录，绑定当时的源码、配置及本地运行身份。文中 `reports/roadmap_v3/…`、`outputs/automation/…` 路径是未随公开源码发布的本地回执，不能从干净检出直接打开。当前提交或工作树要取得同类结论，需要重新运行并保存新的回执；本页不改变策略或实盘准入状态。

## 1. 已交付范围与验收边界

自动化层负责调用已有数据、回测、统计验证与容量接口，保存输入身份、退出结果、裁决和运行索引。它不创建另一套交易算法，也不修改 `config/params.yaml`、策略准入状态或前瞻协议。

`SYS-01` 已有本地冻结及复现验收，不能再标记为“R0 尚未冻结”。每次自动化另记录本次源码与配置身份，运行中身份变化会失败；此前冻结通过不自动覆盖后续修改。远程 CI 是否通过，要以实际远程运行回执为准。

当前具备以下任务：

| 任务 | 实际动作 | 验收意义 |
|---|---|---|
| `nightly-data` | 从配置的起点增量更新已收盘行情，核验缓存来源、覆盖、文件哈希及行数未回退 | 一次真实行情刷新事实；不等于独立第二来源确认 |
| `weekly-matrix` | 先刷新或核验近期缓存，再执行 full 报告矩阵、逐单元裁决及 manifest 回放 | 固定工程检查与回放通过；不授予策略准入 |
| `weekly-smoke` | 固定 180 根日线合成行情，生成 full 报告、裁决并回放 | 离线工程冒烟，永不计作真实运营日 |
| `monthly-universe` | 比较本地 PIT 成员表与外部提供的独立上市/退市观察，写差异报告 | 缺观察即不足；只比较，不自动修改正式成员表 |
| `monthly-optimize` | 对固定候选分别运行训练和验证，仅用训练均值选择候选 | 前瞻窗口之前的历史研究诊断 |
| `quarterly-robust` | 对固定候选统计验证，并对官方默认策略运行容量曲线 | 不按验证收益挑选赢家；容量仍是模型结果 |
| `status` | 从实际运行回执汇总连续真实数据刷新日 | 14 日计数观察，不代表全部自动化验收完成 |
| `prune` | 生成留存清理计划；显式 `--apply` 才删除符合条件的自有目录 | 仅清理自动化所有权范围内的历史产物 |

`monthly-universe` 已有只读比较入口；当前配置 `universe.independent_evidence=null`，尚未提供独立生命周期观察，因而不能产生通过结论。它不会自动更新 PIT 成员、上市/退市资格或生命周期文件，也不能把默认两币矩阵说成完成全历史动态选币研究。

## 2. 配置与调用

以下命令在仓库根目录执行，使用已安装锁定依赖的 Python 环境。Windows 可把 `python` 换成 `.\.venv\Scripts\python.exe`。

```powershell
python scripts/run_automation.py weekly-smoke --pin
python scripts/run_automation.py status
python scripts/run_automation.py nightly-data
python scripts/run_automation.py weekly-matrix
python scripts/run_automation.py monthly-universe
python scripts/run_automation.py monthly-optimize --synthetic --pin
python scripts/run_automation.py quarterly-robust --synthetic --pin
```

`nightly-data` 和真实矩阵会访问行情提供方；这些命令是操作入口，不代表已运行或已部署。`weekly-smoke` 始终使用合成数据。研究任务的 `--synthetic` 生成合成行情，同时把配置研究起点之后的窗口限制到最多 365 天；仍需有效且未打开的前瞻协议。

默认配置为 [config/automation.json](../config/automation.json)：

| 参数 | 当前默认 |
|---|---|
| 产物根目录 | `outputs/automation` |
| 标的 | `BTC/USDT`、`ETH/USDT`，与缓存 manifest 的键一致 |
| 周期 | `1d`；编排器支持 `1d`、`4h`、`1h` |
| 矩阵窗口 | `full`、`bull2021`、`bear2022`、`recent` |
| 数据起点 | `2020-01-01` |
| 矩阵资本 / 种子 | 100000 / 42 |
| 工程最大回撤阈值 | 0.25，按比例而不是百分数 |
| 单次任务总时间预算 | 14400 秒；子步骤共同消耗此预算 |
| 留存期 | 90 天；配置不得短于 14 天 |
| 研究窗口 | `2020-01-01` 至 `2026-09-18`，含首尾 UTC 自然日 |
| 研究协议 | 配置指定 `SYS-11` 的 `20260920-followup-successor-01` 前瞻协议，必须先存在且通过身份检查 |

使用 `--config <文件>` 可选择另一份同 schema 配置。配置只控制编排，不会改写交易参数。`--pin` 保留本次运行；`--skip-fetch` 只适用于 `weekly-matrix`，必须已有同源码和配置、24 小时内成功且封存校验通过的真实 `nightly-data` 回执，而且缓存再次通过时效、覆盖和哈希检查，当前 manifest 还须与封存缓存快照完全一致。

月度宇宙比较使用 `universe.file` 指定的 CSV。`universe.independent_evidence` 指向独立观察 JSON，其 `schema_version="universe-observation/v1"`、`independent=true`、`source_id`、带时区且非未来的 `observed_at` 必须齐全；`records` 中每行包含 `symbol`、`listed_at`、显式 `delisted_at`（仍上市为 null）和非空 `source_ref`。缺少、超过默认 45 天或覆盖不足输出 `insufficient_data`；身份、日期错误或实际差异输出 `fail`；有效覆盖且完全一致才为 `pass`。独立性声明与哈希仍需实际来源材料支持，脚本不会凭该声明认证外部事实。

输出根必须是当前仓库 `outputs` 的子目录，不能经符号链接或 Windows junction 跳转。非空而无所有权标记的目录不会被接管。整个自动化根使用排他锁；已有锁不会因时间较旧而被自动抢占，应先查明对应进程和回执状态。

## 3. 报告裁决与回放

自动化真实矩阵和冒烟均使用 `main.py --report-profile full`。只有完整 manifest、固定输入快照和审计产物才能进入裁决与回放；`compact` 或 `workbook` 输出不能仅凭复制一个缓存 manifest 就宣称可以精确重放。

[evaluate_matrix.py](../scripts/evaluate_matrix.py) 检查配置所要求的全部单元是否齐全且没有重复，子进程是否退出 0，报告是否在当前矩阵目录内，并核验：

- `run_manifest.json` schema 2.0，已登记产物与 `data_inputs` 快照哈希一致。
- `metrics.json` 标准 schema、有效交易输入、事件审计覆盖以及 `reconciliation.json` 逐笔对账通过。
- 指标名唯一，`ok` 值必须有限；`undefined`、`insufficient`、`insufficient_data`、`not_modeled` 等有效不可用状态使用 `null`，不会被伪装成零。`invalid_input`、未知状态或不可用状态携带非空数值会拒绝。
- 收益、回撤和交易数的基础数值有效；回撤超过配置阈值时工程裁决失败。少于 30 笔交易或研究收益为负写入警告，工程通过不等于统计支持充足。

独立重算已有矩阵裁决的接口为：

```powershell
python scripts/evaluate_matrix.py outputs/automation/runs/<run_id>/matrix --maximum-drawdown 0.25 --output outputs/review_verdict_new.json
```

`--output` 文件必须尚不存在。矩阵自动化会对每个通过检查的 full 报告调用 `main.py --replay-manifest`；回放非零退出会使整次运行失败。

当前裁决未实现“收益必须超过基准”或“与上周收益差不得超过 0.5 个百分点”这两个草案门槛。不同输入版本的差异不能充当同输入复现失败；本版以本次 manifest 绑定的输入回放结果为准。

## 4. 月度与季度研究协议

研究入口独立提供以下接口：

```powershell
python scripts/run_research_automation.py --task monthly-optimize --protocol reports/roadmap_v3/SYS-11/20260920-followup-successor-03/prospective_protocol.json --data-dir data/binance/1d --start 2024-01-01 --end 2024-08-27 --symbols BTC/USDT --output outputs/research_automation/new_monthly_run --synthetic --candidates-json '[[20,10],[30,10]]'
```

示例中的 `--protocol` 文件只存在于当时的本地证据目录；在其他检出运行前，需提供与当前配置身份匹配的实际协议文件，并使用新的输出目录。

`quarterly-robust` 使用同样参数，还可指定 `--capital-levels 10000 100000`；`--timeframe` 默认 `1d`。直接调用时输出目录必须是新目录，合成诊断仅支持最多 2000 根日线。当前编排器的研究任务使用 worker 默认候选与资本档位，不把 `automation.json` 的矩阵资本误作研究定仓资本。

执行边界如下：

1. 先调用 `validate_prospective` 验证 `strategy_review_forward/v1` 协议、注册哈希和时间边界。协议必须仍为 `pending_unseen_evidence`，没有 `.opened` 回执；协议配置哈希须与当前参数文件匹配。
2. `--start/--end` 是包含首尾的 UTC 自然日。整个运行区间都必须在 `test_start` 之前；缓存文件元数据及实际行也不能包含受保护窗口。**即使到了成熟日，自动化也不会打开最终样本。** 单次最终裁决仍属于独立治理流程。
3. 真实输入只读本地 `binance-cache/v2` 缓存，或 Phase-2 `run_manifest.json + data_inputs` 快照，检查身份、文件哈希、时间覆盖和 OHLCV。worker 不抓行情、不读取密钥；缓存中的派生指标会被排除后重新计算。
4. 研究按共同时间轴前 70% 原始 bar 为训练、后 30% 为验证；使用共同预热长度，每段至少需要 30 根可评估 bar。训练与验证分别运行引擎，验证从空仓开始，只借用训练末尾历史用于预热。候选列表在运行开始时固定并进入身份清单，最多 16 个合法且不重复的窗口对。
5. 月度只按训练均值选择；验证结果用于诊断，不参与排序。季度逐个评价固定候选，不选赢家；容量调用 `backtest.capacity.run_capacity_curve` 使用官方配置默认策略，不使用月度赢家。
6. 统计复用既有验证、Bootstrap、Monte Carlo 和 BH-FDR 接口。当前 Bootstrap 为 IID，Monte Carlo 重排的是验证收益序列；这些不能替代独立交易情景、真实盘口容量或未来样本证据。

默认研究目录目前是持续刷新的行情缓存。当缓存开始包含受保护的最终样本窗口时，研究任务会明确拒绝读取，即使请求的训练截止日更早。届时应由研究负责人提供经过审查、只包含允许区间的固定快照，更新配置并重新登记身份；不能删除边界检查来维持定时任务成功。

研究输出同时保存 `engineering_status` 与 `research_status`。工程 `pass` 可以伴随研究 `insufficient`；`synthetic_only` 永不作为独立研究或真实运营证据。当前源码不等于协议注册源码时，manifest 明确记录 `source_matches_registered_candidate=false`，历史诊断不会继承原候选准入身份。

## 5. 产物、索引与退出结果

每次编排运行获得独占目录 `outputs/automation/runs/<run_id>/`：

| 产物 | 内容 |
|---|---|
| `.automation-owner.json` | 仓库、schema 与运行所有权 |
| `run_log.json` | 任务状态、开始/结束时间、所有子步骤命令、退出码、耗时、裁决和失败原因 |
| `config_input.json` / `config.json` | 原配置及实际读取配置 |
| `source_identity.json` | 当次受控源码、配置、夹具及依赖文件的哈希 |
| `evidence_seal.json` | 回执及配置、源码清单、缓存身份等关键文件的封存校验；连续运行统计会核验 |
| `step_*.log` | 各步骤输出，失败步骤也保留 |
| `cache_identities.json` | 真实刷新后的缓存身份；仅相关任务生成 |
| `universe_input.csv`、`universe_diff.json`、`universe_independent_evidence.json` | 月度宇宙比较的正式表副本、差异及已提供的独立观察副本 |
| `report/` 或 `matrix/`、`verdict.json` | 冒烟或矩阵报告和工程裁决 |
| `research_protocol.json`、`research/` | 研究协议副本及 worker 的独立产物 |
| `ALERT.md` | 失败本地告警；不表示消息已送达任何人 |

研究目录进一步包含 `research_report.json`、`run_manifest.json`、`source_manifest.json`、`protocol_snapshot.json`、`resolved_config.json`、固定 `data_inputs/`，以及逐候选训练/验证的收益、权益和事实文件。缺输入或边界拒绝的运行可以只有失败回执，不能据文件数量推定执行成功。

根目录 `index.json`、`index.csv` 是从自有 `run_log.json` 回执重建的索引，分别原子替换。若在两份索引之间中断，以 JSON 投影为优先参考，下一次调用从运行回执重新生成；不凭索引补造运行日。`run_log.production_evidence=true` 在当前编排中仅表示非合成任务成功，不能解释为策略、账户或实盘准入已经通过；研究 worker 的 `production_evidence` 始终为 false。

| 入口 | 成功退出 | 失败退出与处理 |
|---|---|---|
| `run_automation.py` | 0 | 运行/配置失败为 1；参数解析错误为 2。超时终止该步骤进程树并保留回执；中断单独标记 |
| `evaluate_matrix.py` | 工程裁决通过为 0 | 裁决或输入失败为 1；命令行参数解析错误为 2 |
| `run_research_automation.py` | 工程 `pass` 为 0，研究可能仍不足 | 边界/身份/执行失败或输入缺失为 2，报告保留具体状态 |

本地告警写入失败运行目录和自动化根的 `ALERT.md`。当前没有外部告警送达、人工阅读回执或自动重试清除失败的证明。源码、配置变化和回放失败必须调查后用新运行目录重新执行。

## 6. 留存与清理

```powershell
python scripts/run_automation.py prune
```

该命令仅生成 `prune_plan.json`。只有显式追加 `--apply` 才执行并写 `prune_applied.json`。清理仅考虑当前所有权根下、已结束且超过配置留存期的运行；以下对象保留：

- 回执的 `pinned=true`、目录含 `PINNED` 文件、或出现在配置 `pinned_runs` 中。
- 根 Markdown、`docs`、`reports`、`config` 中仍引用其运行 ID。
- 其他自有运行仍引用的回执，包括矩阵使用的历史 nightly 输入。
- 未结束、仍运行、未到留存期，以及含符号链接或 junction 的路径。
- 无匹配所有权标记的目录，以及自动化根之外的报告、基线、归档。

索引可重建，不以“仍在索引”作为永久保留条件；正式验收回执应使用 `--pin` 或正式引用保护。已注册的历史基线和其他任务目录不会被这个入口接管清理。

## 7. 调度计划与离线 CI

[Windows 调度脚本](../scripts/register_automation_tasks.ps1) 默认仅打印计划：

```powershell
powershell -NoProfile -File scripts/register_automation_tasks.ps1
```

当前计划使用机器本地时间：

| 任务 | 计划时间 |
|---|---|
| `nightly-data` | 每日 01:37 |
| `weekly-matrix` | 周日 02:47；任务内自行刷新数据 |
| `weekly-smoke` | 周一 03:17 |
| `monthly-universe` | 每月 1 日 02:17；需要独立生命周期观察 |
| `monthly-optimize` | 每月 1 日 04:37 |
| `quarterly-robust` | 1、4、7、10 月 2 日 09:17 |

显式 `-Register` 才会调用 Windows Task Scheduler；**本次已注册六项任务并回读确认全部为 Ready**，回执见 `reports/roadmap_v3/followup/20260920-implementation/scheduler-registration-elevated.json` 与 `scheduler-readback.json`。首次默认权限执行被系统拒绝，取得所需执行权限后注册成功，原失败日志也保留。注册形式使用隐藏窗口、绝对工作目录和最小权限，需要当前 Windows 用户保持登录，不保存密码，也不强制覆盖既有同名任务。计划时间不会绕过编排锁、任务预算或研究边界。

2026-09-20 已手动通过一次真实 BTC/ETH 行情刷新（各2454根、2020-01-01至2026-09-19的已收盘日线），并通过四时间窗矩阵及逐项完整回放。前一次从未验证旧缓存迁移时，行数回退检查阻断了运行；旧缓存已单独归档，成功回执来自随后通过验证的刷新，没有覆盖该失败记录。当前仅积累1个真实UTC刷新日；缓存起点不代表交易所上市日期。

最终followup-successor-03版本已重新执行上述刷新/矩阵，并用同一真实缓存完成月度优化和季度稳健性/容量流程；两项工程执行通过，研究结论均为样本不足（每候选6个有效交易群组，要求至少30个）。月度universe检查因缺独立上市/退市证据退出1并留下告警。完整回执见最终自动化汇总（仅本地证据：`../reports/roadmap_v3/followup/20260920-implementation/final-automation-summary.json`），旧版本回执不用于新身份连续计数。

[backtest-automation.yml](../.github/workflows/backtest-automation.yml) 在 PR、main push、手动触发及每周 UTC 周日 19:17 运行离线回归、合成冒烟、完整回放并上传回执。CI 不抓币安数据，不提供交易所凭据。该工作流文件已交付；远程是否执行及通过仍须查看实际 CI 结果，不能把本地测试写成远程验收。

## 8. 仍需获得的真实证据

本地已完成一次封存的合成冒烟（仅本地证据：`../outputs/automation/runs/20260920T084254_994688Z_weekly-smoke_4a82469e/run_log.json`）：完整报告、工程裁决和 manifest 回放通过，运行期间源码与配置身份保持一致。该回执明确为 `synthetic=true`、`production_evidence=false`、`live_admission=false`；它只证明该源码版本的离线执行链路通过。

`status` 只统计每个 UTC 日期最后一次真实 `nightly-data` 的结果，并重新验证封存回执、原配置/有效配置、源码清单和缓存身份。每个标的必须具有完整已收盘 bar 覆盖，缓存采集时间须属于声称的 UTC 运行日；连续段要求同一源码与配置身份。合成、未来、未封存或封存被改写的回执不计入；缺日、当日最后一次失败或身份切换会打断连续计数。达到 14 日后，字段 `daily_data_observation` 可以通过，但完整自动化运营验收仍要求真实周矩阵和告警送达/人工回执证据。本次工程交付不制造已经流逝的 14 日。

上述 14 日行情刷新计数与 `SYS-17 / R7` 要求的至少 56 个连续自然日、至少两种市场状态、逐笔和日终账户对账不是同一验收。账户来源、真实运维、未来样本和灰度批准继续按[开发计划](development_plan.md)、[统一路线图](unified_roadmap.md)及[账户事实操作说明](account_fact_operations.md)推进。策略保持 `paused_revalidation`。
