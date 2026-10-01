---
name: quant-backtest
description: 按本项目标准流程执行量化回测类任务：单次回测、批量矩阵、参数寻优（walk-forward）、manifest 回放校验、回归基线与离线验收，内置种子/费率/holdout/基线纪律红线
type: prompt
whenToUse: 当用户要求运行回测、批量回测矩阵、参数优化、回测结果回放复现、回测回归基线比对或再生成、回测相关验收打包时
---

# QuantTradingV1 回测标准作业流程

用户输入：$ARGUMENTS

先判定任务类型（single / matrix / optimize / replay / regression / acceptance / data），再按下面对应章节执行。所有命令在项目根目录 `D:/QuantTradingV1` 下运行。

## 通用红线（任何任务类型都不得违反）

1. 固定 `--seed 42`；使用 `--random_slip` 时必须把种子记入结论。
2. **正式（要被引用的）运行必须 `--report-profile full` 且事后回放校验通过**：
   `python main.py --replay-manifest <报告目录>/run_manifest.json`，退出码 7=拒绝、8=不一致，非 0 即复现性事故，结论不可引用。
3. 参数只能在 train/validation 段选择；holdout 只允许单次裁决（`analysis/research_validation.py` 的 HoldoutVault）。**前瞻协议观察窗 [2026-10-20, 2027-04-18) 内禁止开样本选参**，只允许 walk-forward 的 train/validation 段运行。
4. 不得自动修改 `config/params.yaml` 的 `routing` / `strategy_governance`；当前 TrendBreakout 处于 `paused_revalidation`，优化结果只作复验证据。
5. 回归基线只新增版本、**永不覆盖**；`tests/generate_engine_baseline` 仅在有意的行为变更后运行（零交易快照会被拒绝写入）。
6. 回测退出码即契约：0 成功 / 2 参数错 / 3 无数据 / 4 空结果 / 5 产物缺失；非 0 不得当作成功汇报。
7. 指标口径遵循契约 `{value, status, reason, ...}`：禁 NaN/Inf、禁写死 252、PF 需 ≥30 笔闭合交易才可信。术语口径以 `docs/glossary.md` 为准。
8. 跑回测前先确认数据新鲜度：`data/binance/1d/_manifest.json` 末根时间戳距当前 UTC ≤ 2 天，否则先跑 data 任务或提示用户。

## single — 单次回测

标准命令模板（CLI 优先、YAML 兜底）：

```bash
python main.py \
  --source local --data-dir data/binance/1d \
  --symbols <SYM...> --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --capital 10000 --seed 42 --market-type spot \
  --universe-file config/universe_binance_spot_1d.csv \
  --alignment-mode union --benchmark-mode fixed \
  --report-profile <full|compact|workbook>
```

- 正式运行用 `full`（保留审计链 20+ 产物：execution_audit.csv、run_manifest.json、data_inputs/ 等）；批量场景用 `compact`；日常冒烟用 `workbook`。
- 产物目录：`reports/<YYYYMMDD_HHMMSS>_<天数>d_<N>Syms_Ret<X.X>pct/`，由引擎自动生成。
- 正式运行后必须执行 manifest 回放（见红线 2）。

## matrix — 批量回测矩阵

```bash
python scripts/run_backtest_matrix.py \
  --timeframes 1d --windows full bull2021 bear2022 recent \
  --capital 100000 --seed 42 --market-type spot
```

- 默认 30 币（PIT 宇宙）× 1d × 4 时间窗；4h/1h 矩阵需先确认对应周期数据已抓取。
- 产物：`outputs/backtest_matrix/<ts>/{config.json, summary.csv, summary.md}`；汇报时附 git commit 与数据 manifest 哈希。
- 裁决口径：全部单元退出码 0；metrics 契约无 NaN/Inf；合并 trades ≥ 30；bear2022 窗 MDD ≤ 25%（drawdown.lock 阈值）。

## optimize — 参数寻优

```bash
python analysis/optimize.py --symbols <SYM> --days 365 \
  --walk-forward --wf-train 180 --wf-validation 60 --wf-test 60 --wf-purge 5 --jobs 4
```

- 网格固定为 ENTRY_WINDOWS×EXIT_WINDOWS（16 组合），只覆盖 TrendBreakout 系窗口参数。
- `--oos` 事后切分模式有泄漏风险，仅作历史对照，不作选参依据。
- 产物：`reports/walk_forward_<ts>.json`；前置检查红线 3（观察窗约束）。

## regression — 回归基线

```bash
python -m unittest tests.test_backtest_regression -v   # 双层基线校验
python -m tests.generate_engine_baseline               # 仅有意行为变更后：新增版本，不覆盖
```

- 引擎等价基线容差：浮点 `rel_tol=1e-9, abs_tol=1e-12`，其余字段严格相等。
- 历史归档防篡改校验：`python scripts/verify_baseline_archive.py`（CI 已强制）。

## acceptance — 离线验收

```bash
python scripts/main_acceptance.py run --output outputs/main_acceptance/<ts>
# 或打包 + 复验：bundle --output DIR --zip Z / verify Z
```

前提：工作区干净（脚本拒绝未提交改动）；覆盖率门槛 55%。脏树场景改用 `python scripts/roadmap_baseline.py run --output <dir>`。

## data — 数据更新

```bash
python scripts/fetch_binance_data.py --timeframe 1d --with-funding
# 代理：--proxy 或环境变量 QUANT_PROXY_URL
```

增量续抓 + SHA-256 manifest 校验 + 只含已收盘 bar；不可信旧档自动归档 `_legacy_unverified/`。

## 汇报要求

- 每次运行汇报：命令、退出码、产物目录、关键指标（总收益/MDD/夏普/PF/trades 数，注明 status）、git commit、数据 manifest 哈希。
- 结论只引用回放通过的正式运行；批量结果以 summary.csv + 裁决口径为准。
- 已知文档漂移（以代码为准）：`docs/backtest_assumptions.md §2` 费率默认值过时（现双边 0.10%）；`docs/modules/router.md` 描述的是旧 `route()` 强平行为（现行为只撤单+冷却）。
