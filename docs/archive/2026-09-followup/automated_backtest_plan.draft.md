# 自动化回测方案

- 版本：v1.0（草案）
- 日期：2026-09-20
- 依据：当前代码库（`main.py`、`backtest/`、`analysis/`、`scripts/`、`tests/`、`.github/workflows/tests.yml`）与现行文档（`docs/README.md` 权威顺序）
- 状态：待评审；实施前请以代码为准核对本文引用的命令行参数

---

## 1. 目标与非目标

### 1.1 目标

1. **一键/无人值守**：数据更新 → 回测执行 → 指标评估 → 回归比对 → 报告归档全链路自动化，人工只看结论与告警。
2. **可复现**：每次自动化运行落盘 `run_manifest.json`，可通过 `main.py --replay-manifest` 逐比特回放；批量运行记录配置与数据哈希。
3. **防过拟合纪律**：参数只能在 train/validation 段选择，holdout 单次裁决，全流程由 `analysis/research_validation.py` 的 HoldoutVault / walk-forward / BH-FDR / DSR 强制执行。
4. **变更门禁**：任何代码或配置变更自动触发引擎等价基线回归与固定 fixture 校验，行为变更必须显式确认并新增基线版本（永不覆盖旧基线）。
5. **回测=实盘同构**：自动化只编排现有共享领域内核（`core.runtime.EventProcessor`），不产生第二条决策链路。

### 1.2 非目标

- 不做 tick/盘口级撮合（日线 OHLC，冲击成本只按参与率近似，见 `docs/backtest_optimization_roadmap.md` §10）。
- 不引入贝叶斯/遗传等新型寻优算法（当前网格 + walk-forward 已满足治理要求；如需扩展见 §13 阶段 D）。
- 不做实盘自动上下线；自动化回测的结论进入评审流程，不直接改 `params.yaml` 的 `routing`/`strategy_governance`。
- 不在 CI 云 runner 上抓取币安数据（网络/代理限制，见 §9）。

---

## 2. 现状盘点

### 2.1 已有能力（可直接复用）

| 能力 | 实现 | 入口 |
|---|---|---|
| 单次回测（丰富 CLI、三种 report profile、退出码 0/2/3/4/5） | `main.py`、`backtest/engine.py` | `python main.py --source local --symbols BTC-USDT --days 365 --report-profile full` |
| manifest 回放比对（退出码 7=拒绝、8=不一致） | `main.py:152-261` `replay_manifest()` | `python main.py --replay-manifest reports/<dir>/run_manifest.json` |
| 批量回测矩阵（币种×周期×时间窗，子进程调 `main.py`，汇总 summary.csv/md） | `scripts/run_backtest_matrix.py` | `python scripts/run_backtest_matrix.py --timeframes 1d --windows full recent` |
| 参数网格寻优 + 真 walk-forward（双遍引擎、purge、BH-FDR） | `analysis/optimize.py`、`analysis/walk_forward.py` | `python analysis/optimize.py --walk-forward --wf-train 180 --wf-validation 60 --wf-test 60 --wf-purge 5 --jobs 4` |
| 研究治理原语（HoldoutVault、DSR、plateau、消融、准入裁决） | `analysis/research_validation.py`、`analysis/validation.py` | 库函数 |
| 双层回归基线（固定 fixture + 引擎等价快照，浮点容差 1e-9/1e-12） | `tests/test_backtest_regression.py`、`tests/baseline_harness.py`、`tests/engine_baseline_harness.py` | `python -m unittest tests.test_backtest_regression -v` |
| 基线再生成（零交易拒绝写入） | `tests/generate_engine_baseline.py` | `python -m tests.generate_engine_baseline` |
| 历史归档防篡改（pinned git blob SHA-256） | `scripts/verify_baseline_archive.py` | CI 步骤 |
| 数据增量抓取 + manifest 哈希 + 只含已收盘 bar 校验 | `scripts/fetch_binance_data.py` | `python scripts/fetch_binance_data.py --timeframe 1d --with-funding` |
| PIT 宇宙裁剪与退市强平 | `core/universe.py`、`config/universe_binance_spot_1d.csv` | `main.py --universe-file config/universe_binance_spot_1d.csv` |
| 主线离线验收（干净树门禁 + ruff/mypy/pytest 覆盖率 55% + ZIP 证据） | `scripts/main_acceptance.py` | `python scripts/main_acceptance.py run --output outputs/main_acceptance/<ts>` |
| 质量门 CI（push/PR 触发） | `.github/workflows/tests.yml` | — |
| 容量曲线 / 回撤预算 / 常驻止损 / 幽灵信号回放 | `backtest/capacity.py`、`drawdown_budget.py`、`protective_stops.py`、`signal_ghost.py` | 引擎内建 |

### 2.2 缺口（本方案要补的）

| 编号 | 缺口 | 对应章节 |
|---|---|---|
| G-1 | 无统一编排入口：数据更新、矩阵、回归、归档靠手工串联 | §4、§9 |
| G-2 | CI 不跑回测矩阵/参数寻优，回测回归只有 pytest 间接覆盖 | §9.3 |
| G-3 | 批量矩阵结果未与基线/准入门槛自动联动，无自动裁决与告警 | §7、§11 |
| G-4 | R0：当前工作区基线尚未冻结（见 `docs/unified_roadmap.md:49`） | §8.3 |
| G-5 | 报告目录无留存/清理策略，`reports/` 已超 150 个目录 | §10.3 |
| G-6 | 文档漂移：`docs/backtest_assumptions.md` §2 费率默认值过时（现双边 0.10%）、`docs/modules/router.md` 描述旧 `route()` 行为 | §14 附录 C |

---

## 3. 总体架构

自动化层只做**编排**，不实现任何交易/指标逻辑，避免违反 `tests/test_architecture_boundaries.py` 的边界（新增脚本一律放 `scripts/`，库代码放 `analysis/`，均单向依赖 `core`/`backtest`）。

```
┌────────────────────────────────────────────────────────────┐
│ 调度层  Windows Task Scheduler / CI cron / 手动              │
│   nightly_data → weekly_matrix → on_demand_research         │
├────────────────────────────────────────────────────────────┤
│ 编排层  scripts/（新增 run_automation.py 等，§9）            │
│   步骤编排 · 失败分支 · 告警 · 汇总                           │
├──────────┬──────────┬──────────┬──────────┬────────────────┤
│ 数据层    │ 执行层    │ 评估层    │ 回归层    │ 归档层         │
│ fetch_   │ main.py  │ metrics  │ fixture  │ reports/ 命名   │
│ binance_ │ matrix   │ 契约+门槛 │ 基线+引擎 │ outputs/ 汇总   │
│ data.py  │ runner   │ (§7)     │ 等价(§8) │ ZIP 证据(§10)   │
├──────────┴──────────┴──────────┴──────────┴────────────────┤
│ 既有内核（不改动）：BacktestEngine → EventProcessor → Router │
│   → Strategy → RiskManager → Broker（next-bar 撮合）         │
└────────────────────────────────────────────────────────────┘
```

---

## 4. 数据层自动化

### 4.1  nightly 增量更新

1. 执行 `python scripts/fetch_binance_data.py --timeframe 1d --with-funding`（如需 4h/1h 矩阵，追加 `--timeframe 4h` / `1h` 独立任务，避免单任务超时）。
2. 脚本自带保障：增量续抓、`_manifest.json`（schema `binance-cache/v2`）SHA-256 校验、不可信旧档归档 `_legacy_unverified/`、`validate_coverage()` 强制只含已收盘 bar。
3. 编排层新增校验（防止"静默旧数据"）：
   - 抓取消后检查 `data/binance/1d/_manifest.json` 中每个 symbol 的末根时间戳距当前 UTC ≤ 2 天；
   - 检查行数单调不减、哈希全部可验；
   - 失败即告警并中止当晚后续回测（ stale 数据宁可不跑）。
4. 代理：走 `QUANT_PROXY_URL` 环境变量或 `--proxy`，调度任务的环境变量由机器级配置注入，不写进仓库。

### 4.2 宇宙表维护

- 30 币 PIT 宇宙：`config/universe_binance_spot_1d.csv` 由 `scripts/repair_binance_point_in_time_data.py` 维护；每月第一天调度一次体检（上市/退市日期漂移），产出差异报告，人工确认后提交。
- 60 币生命周期：`config/universe60_lifecycle.json` 仅用于研究复验（`scripts/run_revalidation60.py`），不进 nightly。

### 4.3 数据快照

每次正式批量回测前，编排层把 `data/binance/<tf>/_manifest.json` 复制进当次产物目录（full profile 已有 `data_inputs/` 逐标的快照，矩阵批跑沿用 `--report-profile compact` + manifest 复制即可），保证事后可定位数据版本。

---

## 5. 单次回测标准化

所有自动化场景统一收敛到一条标准命令模板：

```bash
python main.py \
  --source local --data-dir data/binance/1d \
  --symbols <SYM...> --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --capital 10000 --seed 42 \
  --market-type spot \
  --universe-file config/universe_binance_spot_1d.csv \
  --alignment-mode union --benchmark-mode fixed \
  --report-profile <full|compact|workbook>
```

约定：

- **固定 `--seed 42`**；研究随机滑点用 `--random_slip` 时必须记录种子到清单。
- **正式（要被引用的）运行一律 `--report-profile full`**：保留审计链（`margin_ledger.csv`、`execution_audit.csv`、`run_manifest.json`、`data_inputs/` 等 20+ 产物）。
- 矩阵批跑用 `compact`（PDF + 核心 CSV），日常冒烟用 `workbook`。
- 退出码即契约：0 成功 / 2 参数错 / 3 无数据 / 4 空结果 / 5 产物缺失；编排层按码分支（§11）。
- 每次正式运行后自动执行一次回放校验：`python main.py --replay-manifest <dir>/run_manifest.json`，退出码非 0 视为复现性事故，阻断归档并告警。

---

## 6. 批量回测矩阵

### 6.1 标准矩阵（weekly）

复用 `scripts/run_backtest_matrix.py`：

```bash
python scripts/run_backtest_matrix.py \
  --timeframes 1d \
  --windows full bull2021 bear2022 recent \
  --capital 100000 --seed 42 --market-type spot \
  --output-root outputs/backtest_matrix
```

- 默认 30 币（PIT 宇宙）× 1d × 4 时间窗；4h/1h 作为月度扩展矩阵单独跑，控制单次时长。
- `--skip-fetch` 仅允许在 nightly 数据任务刚成功后使用；否则让矩阵自己先补数。
- 产物：`outputs/backtest_matrix/<ts>/{config.json, summary.csv, summary.md}`，编排层在 summary.md 头部追加数据 manifest 哈希与 git commit，形成自描述产物。

### 6.2 矩阵裁决（新增，补 G-3）

新增 `scripts/evaluate_matrix.py`（纯只读，输入 matrix 目录，输出 `verdict.json`）：

| 检查 | 阈值（默认，可在 config 调整） | 失败级别 |
|---|---|---|
| 每单元退出码 | 全部 0 | 阻断 |
| metrics.json 契约 | 全部 `status=ok`，无 NaN/Inf | 阻断 |
| 交易样本量 | 全窗合并 trades ≥ 30（PF 可信下限，见指标口径） | 警告 |
| full 窗总收益 | ≥ benchmark（fixed） | 警告 |
| bear2022 窗最大回撤 | ≤ `drawdown.lock` 阈值 25% | 阻断 |
| 与上一周同配置结果差 | 同数据版本下应逐比特一致；数据版本变化时收益差 \|Δ\| ≤ 0.5pp 否则人工复核 | 警告 |

裁决结果写回 `outputs/backtest_matrix/<ts>/verdict.json`，并作为告警输入（§11）。

---

## 7. 参数优化与统计验证纪律

### 7.1 三层协议（强制顺序）

1. **筛选层（月度或参数变更时）**：`analysis/optimize.py --walk-forward`，网格 `ENTRY_WINDOWS×EXIT_WINDOWS`（16 组合），每 split 只用 validation 半段打分，测试段拼接得 procedure 收益。
2. **稳健层（季度）**：`analysis/validation.py` 的 `validate_parameter_candidates()`（仅训练段选参 + bootstrap/MC 各 1000 次 + BH-FDR `fdr=0.05`）；候选需落在 `parameter_plateau` 平台上而非尖峰。
3. **裁决层（一次性）**：`analysis/research_validation.py` 的 `HoldoutVault` + `evaluate_holdout_admission()`，holdout 只开一次。

### 7.2 纪律红线（编排层强制，不靠自觉）

- **前瞻协议观察窗 [2026-10-20, 2027-04-18) 未成熟前，禁止开样本选参**（`docs/unified_roadmap.md:100`）；编排层在 optimize 任务前置检查当前日期，窗口内只允许 `--walk-forward` 的 train/validation 段运行。
- 优化产物必须落 `reports/walk_forward_<ts>.json` 并在头部写入 `HoldoutProtocol` 的 SHA-256 指纹，无指纹的产物不进评审。
- 当前策略治理状态为 `paused_revalidation`（9/19 共 626 次研究运行结论 fail：默认 −5.436%，滚动 4/11 盈利），自动化优化结果只作为复验证据，不自动改 `params.yaml`。
- `--oos` 事后切分模式只用于历史对照，不作为选参依据（其自身注释已声明泄漏风险）。

---

## 8. 回归基线与变更门禁

### 8.1 两层基线（已有，进 CI）

- fixture 层：`tests/fixtures/backtest/` 手写 bundle（schema 校验 + 三次物化一致 + 手算 PnL 往返）。
- 引擎等价层：`tests/fixtures/backtest/engine/engine_baseline_v3.json`，固定种子合成数据全链路快照；浮点 `rel_tol=1e-9 / abs_tol=1e-12`，其余字段严格相等，同进程双跑 bit 级一致。
- 任何 PR 必须通过：`python -m unittest tests.test_backtest_regression -v`。

### 8.2 行为变更协议

1. 开发者确认变更是有意的（在 PR 描述引用任务编号）。
2. 运行 `python -m tests.generate_engine_baseline` 生成**新版本**基线文件（v4、v5…），**永不覆盖旧版本**。
3. 测试默认比对最新版；旧版本保留供历史回放。
4. `scripts/verify_baseline_archive.py` 保护的 phase0 归档为只读历史，任何改动直接 CI 失败。

### 8.3 补 G-4：当前工作区基线冻结（R0）

按 `docs/unified_roadmap.md` R0 要求，实施本方案前先完成：

1. 工作区收敛到可提交状态，记录 git commit。
2. `python scripts/roadmap_baseline.py run --output outputs/roadmap_baseline_<ts>`（脏树快照版）或干净树后 `main_acceptance.py bundle`。
3. 三次独立固定回归（同种子、同数据 manifest）结果一致后，把 commit + 数据哈希 + 配置哈希登记进 `docs/baselines/` 新目录。
4. 冻结完成前，自动化产出的回测结论标注 `baseline: unfrozen`，仅作趋势参考。

---

## 9. 调度与编排

### 9.1 任务表（本地 Windows Task Scheduler，生产形态）

| 任务 | 频率 | 内容 | 时长预算 |
|---|---|---|---|
| `nightly_data` | 每日 01:30 | §4.1 数据增量 + 体检；成功后才解锁当周矩阵 | ~10 min |
| `weekly_matrix` | 每周日 02:30 | §6.1 标准矩阵（1d×4 窗）+ §6.2 裁决 + §10 归档 | 30 币×4 窗，~1-2 h |
| `monthly_optimize` | 每月 1 日 03:30 | §7.1 筛选层 walk-forward（受 7.2 红线约束） | ~2-4 h（--jobs） |
| `monthly_universe` | 每月 1 日 | §4.2 宇宙表体检 | ~10 min |
| `quarterly_robust` | 每季度 | §7.1 稳健层 + 容量曲线 `backtest/capacity.py` | 半日 |
| `on_demand_research` | 手动 | 研究协议批跑（strategy_review 系列），冻结协议后运行 | 不定 |

注：时间避开整点/半点（herd 避让），任务间用完成事件触发而非固定间隔。

### 9.2 编排入口（新增 `scripts/run_automation.py`，补 G-1）

单一入口，子命令对应任务表：

```bash
python scripts/run_automation.py nightly-data
python scripts/run_automation.py weekly-matrix [--skip-fetch]
python scripts/run_automation.py monthly-optimize
```

职责仅限：调用既有脚本（subprocess）→ 收集退出码 → 写 `outputs/automation/<task>/<ts>/run_log.json`（含每步命令、时长、退出码、产物路径）→ 触发告警（§11）。不实现任何回测逻辑。

### 9.3 CI 扩展（补 G-2）

- 保留现有 `tests.yml`（push/PR：环境自检 + ruff + mypy + pytest cov≥55% + 沙盒 e2e + 基线归档校验）。
- 新增 `backtest_regression.yml`：push 到 main 时跑引擎等价基线的独立 job（与 pytest 解耦，失败信息更聚焦）。
- 新增 `weekly_smoke.yml`（`schedule: cron`）：**不抓币安数据**，用 `tests/fixtures/` 合成数据 + `--source synthetic` 跑一条 180 bar 冒烟回测 + manifest 回放，验证"裸机可复现"。真实数据矩阵留在本地调度（数据缓存不入库）。
- 自托管 runner（可选远期）：若后续把数据缓存挂到 self-hosted runner，可把 weekly_matrix 搬进 CI；现阶段不做。

---

## 10. 报告、归档与留存

### 10.1 命名与目录（沿用既有约定）

- 单次：`reports/<YYYYMMDD_HHMMSS>_<days>d_<N>Syms_Ret<X.X>pct/`（`main.py:556-567` 自动生成）。
- 矩阵：`outputs/backtest_matrix/<ts>/`；编排日志：`outputs/automation/<task>/<ts>/`。
- 验收证据：`outputs/archives/`（ZIP + 索引）。

### 10.2 索引（新增）

`scripts/run_automation.py` 每完成一次正式运行，向 `outputs/automation/index.csv` 追加一行：`ts, task, git_commit, data_manifest_sha256, symbols, window, total_return, max_drawdown, trades, verdict, report_dir`。所有历史结论以此索引为入口，禁止直接在 `reports/` 里翻目录。

### 10.3 留存策略（补 G-5）

- `reports/`：自动化产生的 `workbook` 冒烟目录保留 30 天；`full`/`compact` 正式产物保留 180 天；被索引引用或验收引用的永久保留（索引行打 `pinned=true`）。
- 清理由 `scripts/run_automation.py prune` 执行，只删无索引引用的过期目录，删前输出清单，支持 `--dry-run`。
- `docs/baseline/phase0/` 与 `outputs/archives/` 不在清理范围。

---

## 11. 告警与失败处理

| 事件 | 检测 | 动作 |
|---|---|---|
| 数据陈旧/哈希失败 | §4.1 体检 | 中止当晚任务链，告警 |
| 单回测退出码 ≠ 0 | 编排层逐子进程收集 | 记入 run_log，矩阵继续跑其余单元，裁决按 §6.2 阻断 |
| 回放不一致（退出码 7/8） | §5 回放校验 | 阻断归档，告警，按复现性事故处理 |
| 矩阵裁决 fail（阻断级） | §6.2 verdict.json | 告警 + 索引行标 `verdict=fail` |
| 优化触碰 holdout 红线 | §7.2 前置检查 | 拒绝运行并告警 |
| 回归基线不一致 | pytest / CI | PR 阻断（既有机制） |

告警通道：首版落 `outputs/automation/<task>/<ts>/ALERT.md` + 控制台非零退出；后续可接邮件/IM webhook（配置项注入，不入库）。

---

## 12. 纪律与边界约束（自动化不得逾越）

1. 编排代码只放 `scripts/`（编排）与 `analysis/`（库），遵守 `tests/test_architecture_boundaries.py`：`core/` 不得回依赖上层，策略不得 import config。
2. 基线只增不改（§8.2）；phase0 归档只读。
3. 参数选择只在 train/validation 段；holdout 单次裁决；前瞻观察窗未成熟不可开样本（§7.2）。
4. 自动化不改 `params.yaml` 的 `routing` / `strategy_governance`；策略从 `paused_revalidation` 恢复走人工评审流程。
5. 正式运行必须 full profile + manifest 回放通过，否则结论不可引用。
6. 指标口径遵守 `docs/backtest_metrics_detailed_development_plan.md`：禁 NaN/Inf、禁写死 252、费用进现金账本而非报告阶段估算、PF 需 ≥30 笔。
7. 依赖变更必须走 `requirements.lock.txt` + `verify_lock.py` + `check_environment.py`（CI 已强制）。

---

## 13. 实施路线图

| 阶段 | 内容 | 验收标准 | 依赖 |
|---|---|---|---|
| P0 基线冻结 | §8.3 R0 冻结当前工作区 | `docs/baselines/` 新增登记，三次回归一致 | 工作区收敛 |
| P1 编排骨架 | 新增 `scripts/run_automation.py`（nightly-data / weekly-matrix / prune / index）；新增 `scripts/evaluate_matrix.py` | 手动跑通 nightly + weekly 全链，索引/裁决/run_log 落盘 | P0 |
| P2 调度上线 | Windows Task Scheduler 注册 §9.1 任务表（完成事件串联） | 连续 2 周无人值守运行，告警链路演练一次 | P1 |
| P3 CI 扩展 | §9.3 两个新 workflow | main 分支可见三个 workflow 全绿 | P1 |
| P4 月度闭环 | §7 优化/稳健层接入月度任务；§10.3 留存清理启用 | 首个季度产出完整 optimize→robust→容量报告链 | P2 |

可选远期（不在本方案承诺范围）：自托管 runner 数据缓存、贝叶斯寻优、告警 webhook、4h/1h 月度矩阵扩容。

---

## 14. 附录

### 附录 A：命令速查

```bash
# 数据
python scripts/fetch_binance_data.py --timeframe 1d --with-funding
# 单次正式回测 + 回放校验
python main.py --source local --symbols BTC-USDT ETH-USDT --start 2023-01-01 --end 2026-08-31 \
  --capital 10000 --seed 42 --universe-file config/universe_binance_spot_1d.csv --report-profile full
python main.py --replay-manifest reports/<dir>/run_manifest.json
# 矩阵 + 裁决
python scripts/run_backtest_matrix.py --timeframes 1d --windows full bull2021 bear2022 recent --capital 100000 --seed 42
python scripts/evaluate_matrix.py outputs/backtest_matrix/<ts>          # P1 新增
# 参数寻优（窗口期约束见 §7.2）
python analysis/optimize.py --symbols BTC-USDT --days 365 --walk-forward \
  --wf-train 180 --wf-validation 60 --wf-test 60 --wf-purge 5 --jobs 4
# 回归
python -m unittest tests.test_backtest_regression -v
python -m tests.generate_engine_baseline                                # 仅有意行为变更后
# 验收
python scripts/main_acceptance.py run --output outputs/main_acceptance/<ts>
```

### 附录 B：产物目录约定

| 路径 | 内容 | 留存 |
|---|---|---|
| `reports/<ts>_<days>d_<N>Syms_Ret*pct/` | 单次回测（按 profile 分档产物） | §10.3 |
| `outputs/backtest_matrix/<ts>/` | 矩阵 config/summary/verdict | 180 天 |
| `outputs/automation/<task>/<ts>/` | 编排 run_log、ALERT | 180 天 |
| `outputs/automation/index.csv` | 全局结论索引 | 永久 |
| `outputs/archives/` | 验收 ZIP | 永久 |
| `reports/walk_forward_<ts>.json` | 优化产物（含协议指纹） | 永久 |

### 附录 C：已知文档漂移（引用时以代码为准）

- `docs/backtest_assumptions.md` §2 费率默认值过时：`params.yaml` 现行为 maker/taker 双边 0.10%。
- `docs/modules/router.md` 描述的 `route()` 与 StateSwitch 强平已被 `collect_candidate` + 只撤单冷却取代。
- `docs/modules/strategies.md` 提到的 `trend_following.py` 已不存在；健康机制为可恢复的 `StrategyHealthMachine`。
- `README.md` 第 9 节记 TrendBreakout 为 `admitted`，与 `params.yaml` 的 `strategy_governance: paused_revalidation` 不一致，以配置为准。
