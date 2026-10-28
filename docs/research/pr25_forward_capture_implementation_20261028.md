# PR25-17 原策略与前向账户自动捕获接线（2026-10-28）

本轮实现默认关闭的本地原策略捕获，并在当前主线接通完整原引擎 bar 观察接口。它为未来真实输入准备可核验的来源链；当前实物回执仍全部来自合成工程样例，真实前向研究证据保持 pending，未采集真实数据、未发真实订单。

## 实际链路

`ForwardPolicyCapture(store, enabled=False)` 关闭时不创建目录、不更换策略、不导入签名实现。显式启用后，观察实际 `ResearchSelector.select` 的唯一一次调用、原候选 signal、实际特征行、完整选择审计及返回候选；订阅同一实际 Broker 的原 typed event pipeline，保留 signal、风险/预留、OrderIntent、order、fill 等原始 EventCodec 字节。

原 native intent 的 signal_id 与 causation_id 继续引用实际策略 signal event_id，不重写为 observation hash。核验将该真实 signal、原选择结果、原 entry_audit.order_id、原生 canonical command 的方向/数量/价格逐项连接。实际特征必须按 symbol/as_of/eligible/FEATURE_COLUMNS 与原 observation 一致；缺字段保持 pending，已有字段但值不同拒绝。冻结 model/policy 完整 payload、selector contract/gate/资本评分设置、已声明协议参数、策略/风险/分配器/路由设置及其源字节。

每条 journal 由捕获进程内生成的 Ed25519 私钥签名；私钥不序列化，close 后释放。固定根 SQLite registry 先登记公钥和冻结身份；registry/journal 禁止 UPDATE/DELETE；journal 同时保留序号、前哈希和独立 checkpoint。v2 binding 必须从实际原 store 的 registry、journal 和 checkpoint 重构。只读 replay 材料不能自签替换决策/数量/方向或追加伪造尾链。拥有原 host 管理权限、可改整套 registry/store 或检查进程内存仍超出本地完整性保证；此机制不是远程证明。

`forward-execution-binding/v1` 的调用者因果元数据不再提供 policy-origin 阳性。旧纯模拟、回顾、score-only、proxy 及事后补字段不能自动升格。v2 还核对原 public collection CSV/manifest 原字节、原 model/protocol/session、实际接收/生成时钟；actual intent event.observed_at 与 native bar 标签单独保留。原账户和重放逐 bar 的现金、持仓数量、费用、净值必须相等，时钟或执行不同不提供候选账户收益。

## 主线默认关闭接线

`BacktestEngine(..., bar_capture=None)` 保持原路径，不 import research。原 run 循环的 timing → market → risk → bookkeeping → terminal 提为 `_run_bar`；显式 observer 只包裹这一原 callable。配置方可传：

```python
observer = ForwardPolicyCapture(store, enabled=True).engine_observer(
    protocol_id=protocol_id, frozen_candidate_id=candidate_id,
    model_id=actual_parent_model_id, frozen_protocol=original_protocol,
    frozen_candidate=original_candidate, evidence_resolver=read_original_store,
    input_source_kind="recorded_forward",
)
engine = BacktestEngine(..., bar_capture=observer)
# 外部调用原 engine.run 后，observer.export_binding() 只导出实际捕获内容。
```

`read_original_store(event)` 必须返回原 observation、与实际 runtime event 基本 OHLCV 相同的 market_event、独立 public collection 引用，不在 hook 内采集或补造输入。recorded_forward 模式要求 store 原 protocol.json/candidate.json 原字节相同。导出保存为 execution_bindings/<binding_id>.json，现有 forward store evaluator 可直接读取；原账户 receipt 仍须经原 Broker 重放和完整来源核验。

## 恢复和账户范围

重复 event 在策略执行前拒绝；失败/中断尾不能被旧成功 checkpoint 隐藏；同 session 不会用新 registry 重启并复制原订单。完整库存/未决单、策略状态、RNG 和融资时钟恢复尚未实现，需先独立 reconciliation。当前仅支持初始平仓现金 spot、等 maker/taker 费率、确定性撮合；flat margin/perpetual 也明确拒绝。

已检查原 Portfolio 批次账本、margin snapshot 与 Broker funding/borrow 时钟。初始库存需要完整 lot/order/risk 状态；margin/funding 还需要融资 ledger、borrow/funding buckets、真实 mark/rate/settlement 接收和头寸事件顺序，不能靠填一份初始 positions 小修为完整账户。本轮未扩大这些证据边界。

完整 bar 的观察包括撮合、stop、风险和记账。最终 `_finish_tail` 在 bar 之外；若它产生交易，日志保留该活动并拒绝导出可升格账户，不声称已覆盖尾盘清算。部分 stop/强平命令的原政策因果来源与账户回放适配仍可能不足，保留 pending。

## 验证与实物范围

原工作树签名来源专项 **90 passed**，独立 peer **11 passed**；旧回执和合成原始捕获保留于 `reports/pr25_forward_capture_20261028/418fe470806d`。该样例实际执行 Strategy → ResearchSelector → PortfolioSignalAllocator → Broker，原 intent 10 单位，三个后续 bar 各成交 0.5；现金 9849.85、净值 9999.85。fake public fetcher、合成 pipeline clock 明确登记，portfolio_return=None。

当前主线首先完成移植来源专项 **92 passed**，真实 BacktestEngine + Router + native Strategy + RiskManager 集成 **2 passed**。default None、显式 off 与 enabled 的账户曲线和完整 EventCodec 事件逐字相同；覆盖后续 bar 部分成交与原风险拒绝。最后 policy 身份补强之前的主线广矩阵 **135 passed（83.14s）**，覆盖本次 capture、旧账户与 forward store、支持诊断及原 engine/fast-bar/memory/tail 测试。此前 133 passed、1 failed 的并行迁移尝试保留；当时 diagnostics.py/forward_evidence.py 在捕获期间改变，source gate 正确拒绝。最终复跑未放松来源核验。独立主线 engine peer 另有 **3 passed**，确认旧五阶段顺序、默认路径及未覆盖尾盘行为。

所有本轮专属源文件通过扩展 Ruff（F/E4/E7/E9/B，保留既有 B905 排除），无 >=100 行函数；whole repository function_length ratchet 通过，未扩质量 baseline。main lock 已有 cryptography==50.0.2；当前本地专项实际 Python 3.13.2 / cryptography 46.0.4，Python 3.11/lock 精确环境 CI 由统一验收记录，尚未在本专项冒称通过。

当前最终源码的主线原始实物回执：[原账户/绑定摘要](../../reports/pr25_forward_capture_20261028/main_6d2300c21281/receipt_summary.json)，[135 项 JUnit](../../outputs/pr25-forward-main-source-verified-20261028.xml)。该实物实际经过原 BacktestEngine/Router/Strategy/RiskManager，原风险将 40 裁剪至 29.99999997 单位，随后仅成交 1.5；独立原 public collection 和全部签名 journal/registry/checkpoint 保留。原账户和回放 cash=9849.85、equity=9999.85；输入仍为合成，portfolio_return=None。source/ 只复制本轮专属源码，完整捕获执行闭包提供哈希身份，未冒称全 study 依赖快照。

最后的必要身份补强改为按实际 selector.mode==policy 核对真实 policy.model_id，不依赖可缺省的 selected_candidate 标签。固定 Bernoulli 权重正反例不执行训练。该最终源码的完整受影响 capture 子集 **39 passed（54.88s）**，回执为 `outputs/pr25-forward-main-policy-identity-20261028.xml`；135 项回执与 `main_ce863b612441` 实物保留在补强前的原字节，不冒称覆盖新源码。新的 `main_6d2300c21281` 实物绑定最终源字节。完整同提交 CI 交由统一主线验收。engine mypy 仍为 baseline 允许的既有 union-attr 1 项，没有新增。
