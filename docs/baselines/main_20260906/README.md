# 主分支工程验收与证据归档：2026-09-06

## 验收对象与结论

- 提交：`8a89116ecaa0f6d4ae41354dacf4629ca806af75`，PR #33 的 main 合并提交。
- 源码 tree：`9849b4c5799f9ebf53ea898150e8105e9214b589`。
- 本次从该提交创建 `codex/main-acceptance-evidence`。交易代码与参数未修改。
- **主分支工程验收通过，完整证据包已本地归档并校验。**
- 不代表策略重新准入、真实 sandbox 运行、paper trading 或资金上线通过。
- 本目录的索引和摘要随代码纳入版本管理；完整 ZIP 仍仅本地保存，未异地发布。

## 验收检查

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| main push CI | 通过 | [GitHub Actions 34023399998](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/34023399998)，提交精确匹配；quality 各步骤 success |
| 本地环境 | 通过 | Windows、Python 3.13.2；10 个直接依赖版本核对通过。远程 CI 使用 Python 3.11，不混称同一环境 |
| 依赖锁 | 通过 | exact pins 与 SHA-256 校验通过；不表示本机所有间接依赖都经 strict-lock 安装核验 |
| Ruff | 通过 | 与主分支 CI 相同命令 |
| Mypy | 通过 | core/domain.py、core/runtime.py、live_trading/execution_adapter.py |
| 完整测试 | 通过 | 636 passed、1 skipped、46 subtests passed；1 条 ResourceWarning |
| 覆盖率 | 通过 | 86.38%，要求至少 55% |
| Sandbox suite discovery | 通过发现，未执行真实调用 | 1 skipped；主动设置 QUANT_SANDBOX_E2E=0，不读取凭据发起交易所操作 |
| 当前配置三次历史 A/B | 通过 | 三个独立进程、每次恢复关/开两臂；交易/权益/基准/报告摘要均一致 |
| 组合恢复隔离 A/B | 通过 | 与修复止损后的固定参考摘要一致 |
| 归档校验器测试 | 通过 | 8 passed；篡改、额外文件、重复条目、危险路径均拒绝 |
| ZIP 完整性 | 通过 | 203 个成员逐文件 SHA-256 与长度检查通过；另有外层 SHA-256 |

完整测试中的警告：`tests/test_p6_live.py::TestLiveTrading::test_engine_maps_trend_down_to_cash_for_spot`
触发未关闭 SQLite 连接的 ResourceWarning。检查退出码为 0，未被视为失败；应作为测试资源清理
后续事项，不隐瞒为“零警告”。原始日志随包保存。

## 固定历史结果

共同输入：30 个币种、2017-08-17 至 2026-06-30 日线、10,000 USDT、seed=42、
spot_margin。每个行情文件在加载时验证冻结 SHA-256；没有下载新行情或搜索参数。

| 实验臂 | 期末权益 | 累计收益 | 最大回撤 | PF | 完整交易数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 当前配置、组合恢复关 | 19,561.35 | 95.61% | 10.58% | 4.23 | 83 |
| 当前配置、组合恢复开 | 19,561.35 | 95.61% | 10.58% | 4.23 | 83 |
| 健康门控关闭的隔离诊断、组合恢复关 | 23,979.54 | 139.80% | 15.89% | 2.83 | 120 |
| 健康门控关闭的隔离诊断、组合恢复开 | 28,616.10 | 186.16% | 17.63% | 2.36 | 221 |

当前配置仍存在 2023-02-11 的策略 MANUAL_LOCK；186.16% 不是当前默认配置收益。
四个实验臂均会计对账通过。完整样本已经用于工程排查，不冒充独立 holdout。

## 归档位置与完整性

- 包：`outputs/archives/main_8a89116_20260906.zip`
- 外层索引（本目录）：[`archive_index.json`](archive_index.json)
- 本地配套索引：`outputs/archives/main_8a89116_20260906.index.json`
- ZIP 字节数：11,907,550。
- ZIP SHA-256：`95450541417d70e9b4f71e2215b2e0ef57f9ce14073a9cf9c36af3a37324d558`。

包内布局：

```text
source.zip                  已验收 Git 提交的源码快照（不含新验收工具）
inputs/run_manifest.json    原始输入来源与哈希；不是当前实验配置
inputs/data_inputs/         30 个冻结行情 CSV
evidence/                   当前验收、远程 CI 事实、日志、三次 A/B 与隔离 A/B
tools/                      本次验收工具及其测试，独立于被验收源码
bundle_manifest.json        所有成员的长度与 SHA-256
README.md                   包内复现步骤与权限/验证边界
```

没有打包工作区数据库、密钥、虚拟环境、`cua_probe.txt` 或历史报告全集。
小索引应进入 Git；大包保留在本地 archives，后续如需异地发布，须明确存储目标、
访问权限、保留期限及行情分发许可。当前未上传，不承诺外部保留期限。

## 复现与恢复

先对照版本管理中的索引核验外层 ZIP 哈希，再运行：

```powershell
.\.venv\Scripts\python.exe scripts/main_acceptance.py verify outputs/archives/main_8a89116_20260906.zip
```

在独立目录解压外层包，再将 `source.zip` 解压到 `source/`。使用 Python 3.11+ 建立环境并
安装包内源码的 `requirements-dev.txt`，从该 `source/` 目录执行：

```powershell
python scripts/run_p0_recovery_backtest.py --source-manifest ../inputs/run_manifest.json --output ../reproduced --verify-reference ../evidence/current_1
```

隔离诊断增加 `--isolate-portfolio-breaker`，参考目录改为 `../evidence/isolated`，输出需换新目录。
输入不再依赖原机器的 reports 路径。包不含解释器和离线 wheel 集，因此不等于离线依赖镜像。

本机已完成独立解压目录恢复重放，两个实验臂的四类摘要全部匹配，证据见
[`portable_replay.json`](portable_replay.json)。
该检查使用现有 Python 环境，不冒充另一台机器或全新依赖环境的验证。

## 保留策略

1. 保留本包和此前修复前/修复后的历史对照，不覆盖、不删除。
2. `archive_index.json`、本说明和恢复校验摘要作为小体积版本管理证据。
3. 包校验失败不得发布为有效基线；改变源码、参数或行情后应创建新的验收版本。
4. ZIP 自带清单用于完整性检查，不是数字签名；真实性依赖审查后的 Git 索引或可信发布渠道。
5. 本次不做 Reports 大规模清理，不解除风控或部署交易服务。
