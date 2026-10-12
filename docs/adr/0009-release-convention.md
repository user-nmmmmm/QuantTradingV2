# 0009 发布约定：语义化版本、CHANGELOG、带注解的标签

- 状态：已采纳
- 日期：2026-10-12
- 相关：[发布约定](../releasing.md)，[基础设施路线图](../infrastructure_roadmap.md) A3，[ADR 0006](0006-delivery-archives-outside-git.md)

## 背景
仓库没有任何发布标签（只有一个 `evidence-archive-2026-09` 证据标签），`pyproject.toml` 的版本还是 `3.0.0.dev0`，也没有变更记录。结果是"这是哪个版本"只能用提交哈希回答，身份影响报告（`scripts/identity_report.py --base`）也没有一个稳定、可读的基准；ADR 0003 已把"身份绑定发布标签"列为需要先有发布流程的后续。

## 决定
- 版本号用语义化版本 `MAJOR.MINOR.PATCH`（预发布 `-rc.N`）。因为本项目是应用，"兼容"按运行与数据契约定义：交易/回测行为、持久化格式、配置含义、入口变化 → MAJOR；向后兼容的新能力和纯结构搬迁 → MINOR；修复、文档、测试、工具 → PATCH。版本号不代替冻结身份，是否要重新登记仍按身份说明与原协议判断。
- 新增 `CHANGELOG.md`（Keep a Changelog）。日常 PR 只在 `[Unreleased]` 追加；发版时改名为带日期的版本标题。
- 发布标签 `vMAJOR.MINOR.PATCH`，带注解，打在 `main` 的合并提交上，推送后不移动、不删除。`pyproject.toml` 的版本（PEP 440 写法）、CHANGELOG 标题和标签三者必须一致，由 `scripts/release_check.py` 在打标签前和打标签后检查。
- 首个标签是 `v3.0.0`：把基础设施整理后的 `main` 固定为后续整理的基线。它**不是**新的冻结候选。
- 创建并推送标签、创建 GitHub Release 由仓库所有者执行；检查脚本只读。

## 备选方案
- 从 `0.1.0` 开始：与 `pyproject.toml` 已有的 `3.0.0.dev0` 和 V3 路线图的叫法不一致，会多出一套对照。
- 版本号由标签自动生成（setuptools-scm）：版本不再出现在文件里，离线检出和可编辑安装的行为更难预测；本项目不分发 wheel（[ADR 0001](0001-editable-install-only.md)），收益小。
- 不要 CHANGELOG，只靠 PR 列表：看不出哪些变化影响了冻结范围或运行契约。

## 后果
- 搬迁 PR 可以用 `--base v3.0.0` 报告影响；之后的发布走"发版 PR → 合并 → 标签"。
- 多了一个要维护的文件；`[Unreleased]` 为空的 PR 不会被拦下，靠评审提醒。
- 若将来需要分发 wheel 或对外发布，应新增 ADR 重新评估版本来源。
