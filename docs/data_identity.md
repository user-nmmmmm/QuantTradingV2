# 数据身份：下载缓存清单与冻结研究输入清单

本页说明 `data/binance/1d/` 里两份清单各管什么、怎样核验、怎样新增或替换数据。决定及取舍见 [ADR 0020](adr/0020-frozen-research-inputs.md)；基础设施路线图 D2。它只回答"用的是哪些字节、来源有多少记录"，不改变任何研究结论，也不代表这些数据已满足时点（point-in-time）要求。

## 两份清单

| 文件 | 谁写 | 作用 | 可靠程度 |
|---|---|---|---|
| `_manifest.json` | 下载器（`fetch_binance_data.py` 等）每次运行都会重写 | **下载缓存清单**：记录标的、请求区间、覆盖状态。既有清单目前只列 BTC、ETH；旧记录用下载机器上的原样字节哈希，新记录显式声明 `hash_policy: sha256-lf/v1` | 说明"这次下载做了什么"，不是冻结的输入集合 |
| `_frozen_inputs.json` | 只有 `scripts/data_inputs.py build` | **冻结研究输入清单**：对每个被采用的 CSV 登记标的、行数、首末时间戳、SHA-256，以及来源核验状态 | 字节级固定；来源状态如实标注 |

两者故意分开：下载器可以随时改写缓存清单，而冻结清单只在评审过的 PR 里改变。

## 登记什么

每个文件一条记录：

```json
"BTC_USDT.csv": {
  "symbol": "BTC/USDT", "rows": 2454, "first": "2020-01-01", "last": "2026-09-19",
  "sha256": "…", "point_in_time": "unknown",
  "provenance": {"status": "download_manifest", "recorded_sha256": "…", "recorded_at": "…", "source": "…"}
}
```

- **哈希口径**：冻结清单和新下载记录均对 CRLF 规范化为 LF 后的字节取 SHA-256。旧下载记录没有 `hash_policy`，缓存验证、增量下载与来源判断只允许原样、统一 LF 或统一 CRLF 的等价摘要；任何内容改动仍失败。显式的新策略只按声明验证，未知策略拒绝，不回退到旧规则。原有 byte-exact 产物哈希不变。
- 新下载 CSV 显式写 LF。研究自动化与论文研究读取器仅对 `binance-cache/v2` 应用缓存兼容；报告快照和未版本化研究回执仍逐字节核验，不因出现 `hash_policy` 就放宽。
- **`provenance.status`**：
  - `download_manifest`：下载缓存清单曾为该文件记录过同样的字节和行数（目前 BTC、ETH）。这证明"这个下载器在这个时间下载过这些字节"，仍不证明历史上当时可得。
  - `unverified`（未核验）：字节被固定了，但没有任何记录说明它从哪来（目前另外 28 个）。**不要**把它写成已验证。
- **`point_in_time`**：一律 `unknown`。时点要求另有机制（`core/market/data_versions.py` 的带发布时间快照、`repair_binance_point_in_time_data.py` 的上市边界修复）；本清单不替代它们。

## 核验

```bash
python scripts/data_inputs.py verify          # 也在 CI 和 dev.py check 里运行
```

以下任何一种情况都会失败：登记的文件缺失、内容与摘要不符（被替换或被改动）、行数或首末时间变了、已被 Git 跟踪的 CSV 没有登记。工作目录里未被跟踪的下载缓存文件默认不管；加 `--strict-dir` 可要求目录里的所有 CSV 都登记。

## 独立佐证（corroboration）

`unverified` 的文件没有下载记录，但可以问另一个来源：Binance 公共行情接口今天给出的同一区间日线，和这个文件是否逐行一致。

```bash
python scripts/data_inputs.py corroborate            # 联网，只读 GET；可加文件名限定范围
```

新佐证先校验所有选定文件的实际摘要、行数、首末日期与冻结清单一致，再从**同一份不可变字节快照**解析日线并绑定实际摘要 `for_sha256`。文件已变时先拒绝，不能把新数据比较结果登记到旧摘要下；抓取结束再复查全部选定文件及清单，变化即拒绝。未知文件名在任何抓取前报错，CLI 返回 2，不写回清单；抓取中断或响应无效也不会写入部分结果。

新 `corroboration` 块包含 `comparison_policy: binance-daily-comparison/v2`、状态、行数、匹配行数、无效 bar 数、差异示例、检查时间与来源。`build` 在文件变化后丢弃旧佐证；`verify` 核对摘要和策略。**佐证不改 `provenance.status`**，也不证明历史时点可得。

`corroboration.status`：

- `match`：区间内日期完整、OHLCV 有效且逐项一致。
- `match_except_final_bar`：差异仅在最后一天、其他行全部一致、无多余或缺失日期；两侧 OHLCV 均有限且有效，开盘价相同、本地 high 不高于来源 high、low 不低于来源 low、成交量非负且不大于来源成交量。表示**符合部分末根形态**，不证明采集时刻或历史可得性。
- `mismatch`：其他不一致或损坏 bar，包括开盘价变化、越界 high/low/close、成交量过大/负值、非有限值、日期缺失；成功返回空列表也属于比较不匹配，不能称交易对不存在。
- `unavailable`：仅在 Binance 明确返回错误码 `-1121`（Invalid symbol）时记录；其他 HTTP 400、无效 JSON、错误页结构或不前进的分页均为错误，不伪装成该状态。[Binance 错误码](https://developers.binance.com/en/docs/products/spot/errors)

比较用十进制数值，`51.43` 与 `51.43000000` 相同。空、本地重复或乱序日期、非日线日期和无法解析的输入在抓取前拒绝。详见 [ADR 0036](adr/0036-bound-data-corroboration.md)。

**2026-10-14 旧流程的记录（30 个文件）：2 个完全一致（BTC、ETH，有下载记录）；27 个只记录末根差异（2026-08-27）；1 个不一致（`MATIC_USDT.csv`）。这些记录没有 v2 策略标记，保留原字节，不视为已经过本轮新规则重评。** `verify` 会单列 legacy 比较记录数量，验证文件身份不会自动升级旧分类。

- **27 个文件原被归为末根差异**。旧分类未验证 OHLCV 不变量，不能据此断言全部是未收盘 bar。需要重新运行受审阅的佐证流程，必要时另补采集时刻证据；BTC、ETH 的下载记录与其他 28 份来源缺口继续分开。
- **`MATIC_USDT.csv` 比公共接口多 83 行**（2025-01-01 起；MATIC/USDT 交易对已下架，接口没有这些日期），其余 1965 行完全一致。这 83 行的来源无法佐证。

本次修复及合成测试**没有改动冻结 CSV、既有下载清单或旧佐证记录，也没有重新联网佐证真实数据**。截掉末根、重下收盘数据、移除或替换 MATIC 的 83 行及采用版本的回执，仍由研究流程决定。

## 新增、替换或移除一个输入

1. 把文件放进 `data/binance/1d/` 并 `git add`（`build` 只登记已跟踪的文件）。
2. `python scripts/data_inputs.py build`，查看清单的差异：新增的一条是否合理，被替换的哈希是否是你要的。
3. 同一个 PR 里更新 `docs/file_retention_manifest.json` 的 `tracked_market_data.tracked_csv_count`（仓库卫生检查要求）。
4. 若新文件有真实来源记录（例如重新下载并生成了新的缓存清单），`build` 会自动标为 `download_manifest`；否则保持 `unverified`，不要手工改状态。

`build --check` 不写文件，只在清单落后于现状时返回 1。冻结的历史证据（`docs/archive/2026-08*`、`2026-09*`）不受影响。
