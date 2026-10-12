# 发布约定

本页说明版本号、`CHANGELOG.md`、发布标签，以及标签与行为身份回执如何绑定。决定及取舍见 [ADR 0009](adr/0009-release-convention.md)、[ADR 0010](adr/0010-release-bound-identity.md)。这里不改变任何策略研究结论，也不构成策略准入或实盘放行。

## 1. 版本号

语义化版本 `MAJOR.MINOR.PATCH`，预发布用 `-rc.N`。本项目是应用而不是被他人依赖的库，所以"兼容"指**运行与数据契约**：

| 位 | 何时增加 |
|---|---|
| MAJOR | 改变交易/回测行为、持久化格式（事件类型名、订单与状态存储，见 [ADR 0005](adr/0005-persisted-event-type-names.md)）、配置字段含义或控制台入口，使旧数据或旧配置不能原样使用 |
| MINOR | 向后兼容的新能力；纯结构搬迁（模块换包名、导入路径变化）也算 MINOR，因为导入方要跟着改 |
| PATCH | 缺陷修复、文档、测试、CI 与不在冻结范围内的工具 |

版本号**不**代替冻结身份：同一个版本号下，行为身份由 §4 的回执固定；是否需要重新登记冻结候选，按[身份说明](identity.md)和原协议判断，不看版本号。

## 2. CHANGELOG

[`CHANGELOG.md`](../CHANGELOG.md) 遵循 Keep a Changelog。日常 PR 只在 `## [Unreleased]` 下追加一行（Added / Changed / Fixed / Removed / Notes）；发版时把它改名为 `## [X.Y.Z] - YYYY-MM-DD` 并新开一个空的 `[Unreleased]`。只记对使用者或冻结范围有意义的变化，不重复 PR 细节。

## 3. 标签规则

- 标签名 `vMAJOR.MINOR.PATCH`（预发布 `vMAJOR.MINOR.PATCH-rc.N`），**带注解**（`git tag -a`），打在 `main` 的合并提交上。
- 发版 PR 只做三件事：`pyproject.toml` 的 `version`（PEP 440 写法：`3.0.0`、`3.0.0rc1`）、`CHANGELOG.md`、必要的文档。合并后再打标签。
- 标签一经推送不移动、不删除；写错就发下一个 PATCH。`evidence-archive-*` 这类证据标签不属于发布标签，不受此约定约束。
- 创建并推送标签、创建 GitHub Release 是对外可见的操作，由仓库所有者执行。

```bash
python scripts/release_check.py vX.Y.Z                 # 打标签之前：版本号与 CHANGELOG 一致吗？
git tag -a vX.Y.Z -m "Release X.Y.Z" <main 上的提交>
python scripts/release_check.py vX.Y.Z --verify-tag    # 标签带注解、内容一致、在 main 线上
git push origin vX.Y.Z
```

标签可以直接做影响报告的基准：`python scripts/identity_report.py --base vX.Y.Z --head WORKTREE`。

## 4. 标签与行为身份回执的绑定（A4）

新的冻结登记不再只记一个源码清单哈希，而是记录一份**绑定记录**（`release-bound-identity/v1`）：

| 字段 | 含义 |
|---|---|
| `tag`、`tag_commit` | 发布标签及其指向的提交 |
| `scheme`、`identity_sha256`、`files` | 该提交上冻结范围的行为身份（`code-token/v1`）及逐文件哈希，即现有的身份回执 |
| `scope_sha256` | 当时 `config/frozen_scope.json` 的哈希 |

```bash
# 生成（拒绝覆盖已有文件；--bind-release 要求标签带注解）
python scripts/identity_report.py --bind-release vX.Y.Z --write-binding reports/<批次>/identity_binding.json

# 核对：标签仍指向同一提交，且工作区或指定提交的身份与记录一致
python scripts/identity_report.py --verify-binding reports/<批次>/identity_binding.json --head WORKTREE --fail-on-change
```

核对失败的情形：标签被移动或删除、范围声明文件变了、冻结范围的行为变了。仅注释/排版的修改不算（沿用 `code-token/v1`）。

绑定记录**不替换、不放宽**任何旧校验：旧登记（例如 `followup-successor-03` 的 395 个文件清单）继续按原来的逐字节方式验证，本页只规定之后新登记怎么写。真正的新身份登记（`scripts/register_strategy_successor.py`）要等结构整理完成后再做，见 [ADR 0008](adr/0008-defer-forward-observation-window.md)。
