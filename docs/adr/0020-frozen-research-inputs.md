# 0020 冻结研究输入清单与下载缓存清单分离

- 状态：已采纳
- 日期：2026-10-14
- 相关：[数据身份](../data_identity.md)，[基础设施路线图](../infrastructure_roadmap.md) D2，[ADR 0003](0003-behavioral-source-identity.md)，`core/market/data_versions.py`

## 背景
`data/binance/1d/_manifest.json` 是下载器的缓存清单：每次下载都会重写，目前只记录 BTC、ETH 两个标的，而已被 Git 跟踪的 CSV 有 30 个。其余 28 个文件没有任何文件级的哈希、范围或来源记录，替换其中一个不会被任何检查发现。缓存清单里的 SHA-256 还是下载机器上原样字节的哈希（Windows 写出的 CSV 是 CRLF），Git 里存的是 LF，所以这份哈希在 Linux 检出上本来就对不上。

## 决定
- 新增 `data/binance/1d/_frozen_inputs.json`（`frozen-research-inputs/v1`），**只**由 `scripts/data_inputs.py build` 写入，下载器不碰它。每个被采用的 CSV 一条：标的、行数、首末时间戳、SHA-256（CRLF 规范化为 LF 后）、`point_in_time: unknown`、来源核验状态。
- 来源状态只有两种：`download_manifest`（缓存清单为同样的字节和行数留下过记录）与 `unverified`（只固定了字节，来源无记录）。现状 2 个 / 28 个。不把无记录的文件说成已验证。
- `scripts/data_inputs.py verify` 在文件缺失、内容或行数/范围变化、已跟踪的 CSV 未登记时失败；放进 CI 的 `quality` 作业和 `dev.py check`。
- 旧的缓存清单、`file_retention_manifest.json` 的 CSV 计数检查、`data_versions.py` 的时点快照都**不改**：新清单与它们并存，只补上"哪些字节被采用"这一层。
- `.gitignore` 对 `data/binance/` 的整体忽略改为只放行这一个文件，其余下载仍然被忽略。

## 后续：独立佐证（2026-10-14）
28 个 `unverified` 文件无法补出下载记录（原始下载没有留下缓存清单）。改为向 Binance 公共日线接口取同一区间，逐行比对，把结果作为 `corroboration` 块写进清单（`scripts/data_inputs.py corroborate`），绑定文件摘要，不改 `provenance.status`（佐证弱于下载记录）。结果：2 个完全一致，27 个只差最后一根日线（半根 K 线），`MATIC_USDT.csv` 多出 83 行接口没有的日期。发现已记录在 `docs/data_identity.md`，数据本身未改动，处理方式留给研究决定。

## 备选方案
- 把 30 个文件都写进下载缓存清单：下载器会改写它，冻结和缓存混在一起，评审时分不清是下载更新还是输入被替换。
- 用原样字节哈希：Windows 与 Linux 检出的摘要不同，CI 无法核验。
- 只登记"来源已核验"的 2 个文件：另外 28 个仍会被无声替换；它们是研究实际使用过的输入，应当被固定，哪怕来源是未核验。
- 把清单放进 `core/market/`：这是一次性的仓库级核验，属于 `scripts/` 的支持脚本；放进 `core` 会改动冻结范围内的行为代码。

## 后果
- 新增、替换数据文件的 PR 必须同时更新清单和 `tracked_csv_count`，差异一目了然。
- 28 个 `unverified` 是已知缺口：要补需要重新下载并留下缓存清单记录，或找到原始来源；补齐后重新 `build`，状态自动变为 `download_manifest`。
- 清单只回答字节身份。时点可得性仍须由 `data_versions.py` 的带时间戳快照回答；新身份登记（ADR 0010）若要引用数据，应引用本清单的摘要。
