# PR25 成本模型本地发布 adapter（2026-10-28）

已实现显式 v2 本地发布链：实际冻结训练输入、内部拟合、实际发布、独立配置验证器、严格消费者复算。原 v1 成本模型仍仅支持 retrospective，不能修改 available_at/evaluation_kind 或重算自哈希后进入 strict/prospective。2026-10-27 的原验收文件未修改。

该交付证明本地发布和消费的工程契约。真实外部成本、留后精度、市场容量仍为 **pending**；没有生产批准、实盘参数变更或发单。当前回执来自最新 main 的规范目录 stage；provider/publication/research 的格式、lint、mypy 和函数长度门禁已通过。跨模块 verifier 透传和全量 CI 仍由统一冻结回执判定，尚不能据此称远端 CI 已通过。原工作树 169 项回执另存为历史，不与 stage 源码混为同一身份。

## 发布与信任边界

`analysis/execution_cost_publication.py` 提供 `LocalExecutionCostPublicationAuthority` 和只读 `LocalExecutionCostPublicationVerifier`。authority identity、journal path 和至少 32 字节的独立秘密必须由应用配置，不能从模型、预测或决策 context 中接收。密钥不写入模型、预测或 SQLite journal。

1. `freeze_training(samples, protocol=..., fit_parameters=..., source_payloads=...)` 在实际本地 UTC 观察时刻冻结原始样本、固定协议、完整拟合参数、可选来源 payload 和真实执行源码/依赖快照。尚未成熟的训练目标拒绝冻结。历史下载数据的事件日期不能替代首次本地观察时间。
2. `observe_features({symbol, market_identity, features}, source_payloads=...)` 为预测输入生成独立的实际本地观察收据。context 必须引用 `feature_observation`，输入值必须一致，观察收据不得晚于决策。方向和 horizon 是情景参数，仍由模型支持域校验。
3. `publish(training_receipt, expires_at=...)` 使用实际当前 cutoff 调用原冻结 fitter，随后以实际完成时刻签发 v2。调用者不能传已有 model、training_cutoff、available_at 或 published_at。首次本地观察晚于旧历史窗口的训练不能产生旧窗口当时可得的模型。
4. SQLite `BEGIN IMMEDIATE` 原子分配序号、前序摘要、随机 ID 和收据。HMAC-SHA256 认证完整 canonical 内容；只读 verifier 重验整条收据链、实际来源代码/数值依赖、训练成熟、协议哈希，并从冻结训练重算模型。
5. `PublishedExecutionCostProvider(artifact, publication_verifier=...)` 在实际模型发布和实际输入观察后支持 strict/local 与 prospective/local。`conservative_trading_cost(..., publication_verifier=...)` 再从可信收据取模型，独立重算整条缓存预测后才进行原非负保守聚合。预测点值、区间、输入、支持域或来源标签自改并重算 hash 均不足以认证。

HMAC 证明此 authority 冻结的字节和本地主机声明的实际发布时间；主机管理员、系统时钟和签名密钥保管属于明确的信任根。本 adapter 不提供外部时间戳服务、硬件证明、密钥泄漏防护或供应商签名认证。收到的 `source_payloads`、历史样本时钟和 `verified_live_complete_cost` 标签不会自动变成外部真实成交证据。v2 始终保留 `real_venue_calibration=false` 与 `production_approved=false`。

`information_policy.knowledge='published'` 当前明确拒绝：本地私有 journal 不能冒充外部供应商公开发布时间。缺少 verifier、输入收据、足够支持域、正确单位/horizon 或有效期的 strict 预测均为 unsupported；消费者保留现有 mandatory 减险路径。

## 接口示例

```python
from secrets import token_bytes
from analysis.execution_cost_publication import LocalExecutionCostPublicationAuthority
from core.planning.execution_cost_provider import PublishedExecutionCostProvider

# 真实应用从独立密钥管理配置载入，不能从模型或预测读取。
authority = LocalExecutionCostPublicationAuthority(
    journal_path, authority_id="configured-local-publisher", signing_key=token_bytes(32)
)
training = authority.freeze_training(samples, protocol=protocol, fit_parameters={"method": "ridge"})
feature_receipt = authority.observe_features({
    "symbol": symbol, "market_identity": market_identity, "features": features
})
artifact = authority.publish(training, expires_at=expiry)
provider = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())
# decision_at 使用实际决策时间，必须在两份实际收据之后。
prediction = provider.estimate({**context, "evaluation_kind": "strict",
    "information_policy": {"mode": "strict", "knowledge": "local", "unknown": "exclude"},
    "feature_observation": feature_receipt})
```

## 实际验证

[新增专项](../../tests/test_execution_cost_publication.py) 与原 104 项执行证据、成本、报价和优化专项共同通过 **169 tests**。涵盖真实本地写入/只读重开、strict/prospective 正向消费、发布前 1ns、观察前 1ns、过期、未来特征、域外/方向/单位/horizon、自签 authority、篡改 journal、重签缓存预测、重签非可重现拟合、来源/依赖变动、时钟回退、缺失 verifier/输入收据，以及 v1 兼容拒绝路径。

- core provider 覆盖率：98.52%；publication 模块：92.31%；合计：95.82%。
- Ruff format（新增/成本模型模块）、F/E4/E7/E9/B（按仓库排除 B905）和 core provider mypy 通过；provider/publication/research 最大函数分别 98/64/86 行。calibration 保留原 legacy 格式，join/sample 长度 153/230 不增，readiness 降至 174；没有提高 baseline。
- [JUnit](../../outputs/pr25_execution_stage_20261028.xml)、[日志](../../outputs/pr25_execution_stage_20261028.log)、[coverage](../../outputs/pr25_execution_stage_20261028_coverage.json) 与[机器回执](pr25_execution_publication_20261028.json)。组合组有 36 个既有 SQLite fixture/ledger ResourceWarning；新增 publication 专项无连接资源告警。

复跑使用 `.venv/Scripts/python.exe scripts/run_portable_tests.py -q tests/test_execution_cost_publication.py tests/test_pr25_execution_costs.py tests/test_execution_calibration.py tests/test_execution_readiness.py tests/test_cost_aware_allocation.py tests/test_quote_observations.py`。源迁移后 `_RUNTIME_MODULES` 必须引用真实 canonical provider 源码，不能只绑定兼容转发文件；新 main 的源码变更必须生成新的冻结回执。

规范目录重构另做了与原工作树的完整产物差分：三臂 model 与 forecast、部分/零成交 shortfall 的总摘要均为 `8b16fbd75a8dc6b2e53092570eef411df370da5d2ef69233e0a66cbc08a5f312`。按职责抽出的输入、费用、融资、训练矩阵、支持域、留后评价和 qualified sample helper 保留这些完整结果。门禁命令与退出码见 [stage quality log](../../outputs/pr25_execution_stage_quality_20261028.log)。
