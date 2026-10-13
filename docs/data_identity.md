# 数据身份：下载缓存清单与冻结研究输入清单

本页说明 `data/binance/1d/` 里两份清单各管什么、怎样核验、怎样新增或替换数据。决定及取舍见 [ADR 0020](adr/0020-frozen-research-inputs.md)；基础设施路线图 D2。它只回答"用的是哪些字节、来源有多少记录"，不改变任何研究结论，也不代表这些数据已满足时点（point-in-time）要求。

## 两份清单

| 文件 | 谁写 | 作用 | 可靠程度 |
|---|---|---|---|
| `_manifest.json` | 下载器（`fetch_binance_data.py` 等）每次运行都会重写 | **下载缓存清单**：记录最近一次下载了哪些标的、请求区间、覆盖状态。可能只列出部分标的（目前 2 个：BTC、ETH），哈希是下载机器上**原样字节**的哈希（Windows 为 CRLF） | 说明"这次下载做了什么"，不是冻结的输入集合 |
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

- **哈希口径**：对 CRLF 规范化为 LF 之后的字节取 SHA-256，即 Git 里存的内容。Windows 工作区（CRLF）和 Linux 检出得到同一个摘要。下载缓存清单里的哈希是 CRLF 字节的哈希，所以不能直接拿来在 Linux 上比较；`build` 在判断来源时两种换行都会尝试。
- **`provenance.status`**：
  - `download_manifest`：下载缓存清单曾为该文件记录过同样的字节和行数（目前 BTC、ETH）。这证明"这个下载器在这个时间下载过这些字节"，仍不证明历史上当时可得。
  - `unverified`（未核验）：字节被固定了，但没有任何记录说明它从哪来（目前另外 28 个）。**不要**把它写成已验证。
- **`point_in_time`**：一律 `unknown`。时点要求另有机制（`core/market/data_versions.py` 的带发布时间快照、`repair_binance_point_in_time_data.py` 的上市边界修复）；本清单不替代它们。

## 核验

```bash
python scripts/data_inputs.py verify          # 也在 CI 和 dev.py check 里运行
```

以下任何一种情况都会失败：登记的文件缺失、内容与摘要不符（被替换或被改动）、行数或首末时间变了、已被 Git 跟踪的 CSV 没有登记。工作目录里未被跟踪的下载缓存文件默认不管；加 `--strict-dir` 可要求目录里的所有 CSV 都登记。

## 新增、替换或移除一个输入

1. 把文件放进 `data/binance/1d/` 并 `git add`（`build` 只登记已跟踪的文件）。
2. `python scripts/data_inputs.py build`，查看清单的差异：新增的一条是否合理，被替换的哈希是否是你要的。
3. 同一个 PR 里更新 `docs/file_retention_manifest.json` 的 `tracked_market_data.tracked_csv_count`（仓库卫生检查要求）。
4. 若新文件有真实来源记录（例如重新下载并生成了新的缓存清单），`build` 会自动标为 `download_manifest`；否则保持 `unverified`，不要手工改状态。

`build --check` 不写文件，只在清单落后于现状时返回 1。冻结的历史证据（`docs/archive/2026-08*`、`2026-09*`）不受影响。
