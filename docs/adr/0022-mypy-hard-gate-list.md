# 0022 mypy 硬门槛清单：已达标的模块不能再回退

- 状态：已采纳
- 日期：2026-10-14
- 相关：[质量门槛](../quality.md)，[基础设施路线图](../infrastructure_roadmap.md) C3，[ADR 0002](0002-quality-ratchet.md)，[ADR 0018](0018-mixins-to-collaborators.md)

## 背景
B4 把 mixin 改为协作类后，棘轮里的 mypy 存量从 869 降到 326。200 个受检文件（`core ops signal_meta backtest live_trading strategies router composition`）中已有 128 个零错误，但保护它们的只有棘轮：文件不在基线里就是"必须为零"，不过 `quality_ratchet.py update` 会把当前计数整体写回基线，一次不留神的 `update` 就能把新的错误当成存量收下。真正的硬门槛只覆盖 3 个契约文件。

## 决定
- 新增 `scripts/mypy_hard_gate.txt`：零错误模块的清单（现为 128 个，含原来的 3 个契约文件）。`quality_ratchet.py check` 在同一次 mypy 运行里额外检查：清单内任何一个文件出现任何发现、或清单里的文件不存在，都失败，**与基线无关**。`quality_ratchet.py gate` 只做这一项。
- `quality_ratchet.py promote` 列出"已经零错误但还没进清单"的模块，`--write` 把它们加入清单；一个 `slow` 测试保证清单是最新的（新达标的模块必须被收进来）。模块只能在说明理由的 PR 里从清单移除。
- 门槛使用与棘轮完全相同的整包 mypy 运行，不单独对清单子集跑 mypy：配置是 `follow_imports = "skip"`，一个模块的结果取决于命令行里还有哪些模块（子集运行会让个别 `# type: ignore` 变成"未使用"），子集运行与基线口径就不一致了。
- 原有的"关键运行契约"mypy 步骤（3 个文件，按本机平台运行）保持不变。

## 备选方案
- 把 128 个文件逐个写进 CI 命令的 `mypy` 参数：命令过长，且得到子集运行的上述口径问题。
- 只靠棘轮：见背景，`update` 可以放宽。
- 全包 `--strict`：缺口太大（326 项存量），不现实；逐模块达标再收进清单是渐进的路径。

## 后果
- 每清零一个文件，PR 里运行 `promote --write` 即可把改进变成不可回退的硬门槛；基线只剩真正的存量（72 个文件）。
- 清单中的模块以后不能靠 `# type: ignore` 之外的方式引入错误；确需 `type: ignore` 时与以前一样要带错误码和理由。
- 剩余的存量集中在 `signal_meta`、`core/market`、`strategies` 等，见 `scripts/quality_baseline.json`，随各自的重构清零。
