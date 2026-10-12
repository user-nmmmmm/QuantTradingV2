# 源码身份、文档移动与断链检查

本页说明基础设施阶段 0.5 新增的三件事。它们**不替换**任何已有的冻结校验：旧的冻结记录继续用创建时的方案（逐字节哈希）验证；这里只是让一次改动对"冻结范围"的影响可见、可比较，并让文档移动不再破坏校验脚本。

## 1. 为什么需要行为身份

既有的源码身份（`research/ml_selection/protocol.py::source_identity`、`scripts/roadmap_baseline.py::source_manifest` 等）对文件字节做哈希，所以改一行注释、重排空行或统一换行符都会让身份变化。结果是：任何整理代码的改动都会被当成"冻结候选的源码变了"，需要重新登记，反而阻碍了无风险的整理。

`core/foundation/source_identity.py` 提供第二种方案 `code-token/v1`：

| 文件类型 | 如何哈希 | 改什么不会改变身份 |
|---|---|---|
| `.py` | 去掉注释和纯排版 token（空行、括号内换行、缩进宽度、空格）后的 token 流 | 注释、空行、空格、缩进宽度、换行符、行折叠 |
| `.yaml/.yml/.toml/.txt` | 字节（CRLF 统一成 LF） | 换行符 |
| 其他（json、csv…） | 字节原样 | 无 |

token 流相同意味着解析出的程序相同，所以任何可能改变行为的修改都会改变哈希。字符串内容和 docstring 属于语义的一部分（运行时可读取），**改它们会改变身份**。无法被分词的 `.py` 文件退回逐字节哈希。该方案不依赖 Python 版本：f-string 在 3.12 前后的分词方式不同，规范化时按原文还原；`tests/test_source_identity.py` 里的金标准哈希在 3.11 / 3.12 / 3.13 上必须一致。

## 2. 冻结范围

范围由 `config/frozen_scope.json` 声明：`core/`、`backtest/`、`strategies/`、`router/`、`composition/`、`live_trading/`、`research/ml_selection/`、`config/`，以及 `main.py`、`run_live.py`、`resolve_live_order.py` 和依赖固定文件。`analysis/`、`scripts/`、`tests/`、`docs/`、`dashboard/` 不在范围内。修改范围文件本身属于范围内的改动。

## 3. 使用

PR 上 CI 会在作业摘要里写出影响报告（不阻塞合并）。本地：

```bash
# 与 main 比较（含工作区未提交修改；只统计已被 git 跟踪的文件）
python scripts/identity_report.py --base origin/main --head WORKTREE

# 保存某个提交的身份，之后核对
python scripts/identity_report.py --head HEAD --write-receipt receipt.json
python scripts/identity_report.py --head WORKTREE --verify-receipt receipt.json --fail-on-change
```

报告把变化分成：**行为变化**（需要按原协议处理冻结候选）、**新增/删除**、**仅注释或排版**（身份不变）。回执不保存原始字节哈希，因此核对回执时无法区分"仅排版"，只给出行为是否变化。

回执核对只给出行为是否变化，不区分"仅排版"；回执里的 `body` 字段使它仍能识别移动（见 §6）。

局限：它只回答"范围内的行为是否变了"，不判断变了是否合理；不改写任何旧的冻结记录；未被 git 跟踪的新文件在工作区模式下不计入。

## 4. 文档移动索引

`docs/doc_index.json` 的 `moved` 记录已合并或归档的文档（旧路径 → 现路径）。`scripts/verify_roadmap_policy.py` 找不到旧路径时通过它解析，所以移动文档不会再让校验失败；断链检查也用它给出"已移动到…"的提示。**每次移动、合并或归档被跟踪的文档时，在这里追加一条**（只增不改；若目标又被移动，更新旧条目，测试会拒绝链式条目）。

## 5. 断链检查

`scripts/check_markdown_links.py`（CI 的 `quality` 作业会运行）检查被跟踪的 Markdown 中的相对链接是否指向存在的文件或目录，跳过：

- `docs/archive/2026-08*`、`docs/archive/2026-09*`（字节保真的历史快照）；
- 指向 `reports/`、`outputs/`、`data/`、`tmp/` 的链接（本地生成或忽略，干净检出里不存在）；
- 外部 URL、纯锚点、行内代码和代码块里的内容。

锚点（`#...`）不检查。`docs/link_check_baseline.json` 记录检查器引入时已存在的断链（目前为空）；新增断链会失败。批量修复后可用 `--update-baseline` 重写基线。

## 6. 移动与仅导入路径变化（A2）

搬迁一个模块会让逐字节/行为身份变化，但搬迁本身没有改变任何行为。为了让搬迁 PR 的报告可读，每个文件除 `norm`（行为身份）外还记一个 `body`：Python 文件是**去掉所有 `import` / `from … import` 语句后**的 token 流哈希（`yield from`、`raise … from` 不受影响），其他文件等于 `norm`。`body` 不参与整体身份哈希，所以不改变任何已有身份或回执。

比较时：

- 一个被删除的文件与一个被新增的文件 `body` 相同 → 报告为**移动**（先按同名文件配对，再按路径顺序；空文件不配对）；
- 同一路径上 `norm` 变了但 `body` 没变 → 报告为**仅导入语句变化**；
- 其余照旧：行为变化、新增、删除、仅注释/排版。

`changes_identity` 仍然严格：只要整体身份哈希变了就为真。新增的 `changes_behavior` 在"只有移动和导入路径变化"时为假，报告标题随之写成"CHANGED (moves / import paths only; behavior unchanged)"。**导入目标不由这份报告验证**：一个文件把 `from a import x` 改成 `from b import x` 会被当作"仅导入变化"，所以这类 PR 仍要靠 import-linter 分层规则和全量测试把关。用 #68（`ops/` 搬迁）实测：15 个文件显示为移动，4 个导入方显示为仅导入变化。

标签与回执的绑定见 [发布约定](releasing.md) §4。
