# ADR 0036：实际字节绑定、末根不变量与旧缓存换行兼容

- 日期：2026-10-15。状态：已采纳。
- 范围：AUD-03；承接 [ADR 0020](0020-frozen-research-inputs.md)，操作说明见[数据身份](../data_identity.md)。

## 问题

佐证流程原先读取当前 CSV，却直接用冻结清单中的旧摘要标记结果；还把任何仅末根差异判为未收盘 bar、把空成功响应和任意 HTTP 400 都当作未知交易对。未知文件名会静默跳过。旧 cache 用下载机器的原样字节摘要，LF 检出不能验证 CRLF 记录。

## 决定

1. 所有选定输入先从一次读取的 LF 规范化字节得到事实与 bar。摘要、行数、首末日期必须匹配冻结清单；先验证全部选定文件，再发请求。结果绑定实际快照摘要，完成后复查所有选定文件和清单；变化即失败，不发布部分结果。未知文件名在任何抓取前拒绝，CLI 错误返回 2。
2. 新比较使用 `binance-daily-comparison/v2`。只有最后一天有差异、其他日期及数值一致，且双方 bar 有效、开盘相同、本地价格范围被来源包含、成交量非负且不超过来源时，才标记 `match_except_final_bar`。这是形态相容，不证明采集时间。损坏、非有限数值或日期缺失为 mismatch；无效 CSV 日期、重复/乱序和不可解析输入先拒绝。
3. 成功空列表保留为空字典，比较时得到 mismatch；仅 HTTP 400 中明确的 Binance `-1121` 才表示 unavailable。其他错误、畸形分页、不前进分页均拒绝；后续空页保留先前已取得的行，交给完整日期比较发现缺口。
4. 缓存共用 `core/foundation/content_hash.py`。新下载记录显式写 `hash_policy: sha256-lf/v1`；缺少策略的旧记录仅允许原样、统一 LF、统一 CRLF 三种摘要。未知声明策略拒绝；价格、行内容、来源、市场、标的、周期和覆盖检查不放宽。通用 byte-exact 哈希与旧缓存文件不重写。
5. 已登记的 30 份 CSV、旧 cache 和佐证记录保持原样。无 v2 标记的比较只作为 legacy 记录保留，核验摘要不等于重新认证末根分类。重新佐证、采用数据版本及真实研究结论必须另外形成证据。

## 验证与后果

全部回归使用合成 CSV、内存 HTTP 响应和临时 cache，覆盖旧摘要、抓取期间变化、文件名错误、部分/损坏末根、空响应、错误码与分页、旧 LF/CRLF 记录、内容篡改、增量游标和新缓存策略。身份/发布安装矩阵同时运行数据回归，覆盖 Linux/Windows × Python 3.11/3.13。

旧清单无需迁移。新规范化缓存摘要在换行转换后可验证，不代表来源或时点自动可信；原 2 份下载记录与 28 份来源未知的边界保留。数据采用决定和修复版本的 A5 绑定/登记继续按原协议办理。

公开依据：[Binance 错误码](https://developers.binance.com/en/docs/products/spot/errors)、[日线 Kline 接口](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints#klinecandlestick-data)。
