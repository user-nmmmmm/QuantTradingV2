# 源码身份、文档移动与断链检查

本页说明基础设施阶段 0.5 新增的三件事。它们**不替换**任何已有的冻结校验：旧的冻结记录继续用创建时的方案（逐字节哈希）验证；这里只是让一次改动对"冻结范围"的影响可见、可比较，并让文档移动不再破坏校验脚本。

## 1. 为什么需要行为身份

既有的源码身份（`research/ml_selection/protocol.py::source_identity`、`scripts/roadmap_baseline.py::source_manifest` 等）对文件字节做哈希，所以改一行注释、重排空行或统一换行符都会让身份变化。结果是：任何整理代码的改动都会被当成"冻结候选的源码变了"，需要重新登记，反而阻碍了无风险的整理。

`core/foundation/source_identity.py` 对新记录默认使用 `code-token/v2`；已有 `code-token/v1` 记录继续按 v1 验证：

| 文件类型 | 如何哈希 | 改什么不会改变身份 |
|---|---|---|
| `.py` | 去掉注释和纯排版 token；v2 另按解析结构规范化 f-string | 注释、空行、缩进宽度、行折叠；表达式中的普通空格 |
| `.yaml/.yml/.toml/.txt` | 字节（CRLF 统一成 LF） | 换行符 |
| 其他（json、csv…） | 字节原样 | 无 |

这是声明范围内的源码身份，不是运行行为等价性证明。字符串值和 docstring 可在运行时读取，改变它们会改变身份。v2 对 f-string 保留字面量值、表达式结构、转换、格式及嵌套格式：`f"{value + 1}"` 与 `f"{value+1}"` 身份相同；debug `=` 会输出表达式原文，因此 `f"{value = }"` 与 `f"{value=}"` 仍不同。无法规范化的 `.py` 文件退回逐字节哈希。

v1 继续把 f-string 原文作为 STRING token，金标准哈希保持不变。v2 有独立金标准；共同支持的语法在 Linux/Windows × Python 3.11/3.13 的安装矩阵中运行身份回归。更高版本才支持的语法不承诺在旧解释器得到相同规范化结果。解析只读取结构，不执行表达式；序列化不使用随解释器版本改变显示规则的 `ast.dump`。依据：[Python f-string 语义](https://docs.python.org/3.13/reference/lexical_analysis.html#f-strings)、[AST 字段](https://docs.python.org/3.13/library/ast.html#ast.FormattedValue)。

## 2. 冻结范围

范围由 `config/frozen_scope.json` 声明：`core/`、`backtest/`、`strategies/`、`router/`、`composition/`、`live_trading/`、`research/ml_selection/`、`ops/`、`signal_meta/`、`cli/`、`config/`，以及 `main.py`、`run_live.py`、`resolve_live_order.py`、`pyproject.toml` 和依赖固定文件。入口映射、包发现、安装依赖声明等 TOML 变动会改变身份；当前按整个文件核对，因此只改工具配置也会改变身份。`analysis/`、`scripts/`、`tests/`、`docs/`、`dashboard/` 不在此范围内。修改范围文件本身属于范围内的改动。

独立的 `analysis/research_evidence.py::source_identity` 使用另一份明确的工作树字节清单，包含未跟踪文件，不能由 frozen scope 缩减。它覆盖研究/脚本/CLI 与拆分后的运行模块，以及配置、`pyproject.toml` 和安装依赖输入；输出目录不在清单内。CLI 或打包输入在运行中变化会使 `ResearchEvidenceRun.verify_identity` 报告失效。它不自动迁移旧登记；外部传入的 `source_hashes` 仍只记录而不验证来源。

## 3. 使用

PR 上 CI 会在作业摘要里写出影响报告（不阻塞合并）。本地：

```bash
# 与 main 比较（含工作区未提交修改；只统计已被 git 跟踪的文件）
python scripts/identity_report.py --base origin/main --head WORKTREE

# 保存某个提交的身份，之后核对
python scripts/identity_report.py --head HEAD --write-receipt receipt.json
python scripts/identity_report.py --head WORKTREE --verify-receipt receipt.json --fail-on-change
```

报告区分源码身份变化、新增/删除、移动、仅导入变化及注释/排版。回执不保存原始字节哈希，核对时无法单独识别仅排版；`body` 字段仍可辅助识别移动（见 §6）。

新回执/绑定默认使用 v2；`--verify-receipt`、`--verify-binding` 自动选择记录的方案，不更新原文件。`--scheme code-token/v1` 可显式生成兼容记录，但不能在验证时覆盖记录的方案。API 调用方须把 `saved["scheme"]` 传给 `build_manifest(..., scheme=...)`；跨方案 manifest 核对直接拒绝。升级方案须新建记录并按协议重新登记，不能把旧 v1 哈希改写为 v2。

指定 Git `--head` 时，源码与 `config/frozen_scope.json` 都从该提交读取，引用先固定为 commit SHA；不借用当前 checkout 的范围。`--base` 比较同样分别读取两端的声明。`WORKTREE` 则读取工作区当前范围，仍只统计已跟踪文件。缺少引用、引用不是 commit 或所选树缺少范围声明时明确失败，不写出成功回执或摘要。

`--scope` 在 commit 模式下必须是仓库内路径，并从所选提交读取；本地外部范围只用于 `WORKTREE`。发布绑定固定核对所选树的 `config/frozen_scope.json`。新身份回执的 `scope_sha256` 也参与核对；不含该字段的旧回执继续按原文件身份校验。AUD-02 首批约束见 [ADR 0034](adr/0034-frozen-replay-and-reference-verification.md)。

局限：它只报告范围内的源码身份和语法分类，不证明行为等价或评价变化是否合理；未被 git 跟踪的新文件在工作区模式下不计入。

## 4. 文档移动索引

`docs/doc_index.json` 的 `moved` 记录已合并或归档的文档（旧路径 → 现路径）。`scripts/verify_roadmap_policy.py` 找不到旧路径时通过它解析，所以移动文档不会再让校验失败；断链检查也用它给出"已移动到…"的提示。**每次移动、合并或归档被跟踪的文档时，在这里追加一条**（只增不改；若目标又被移动，更新旧条目，测试会拒绝链式条目）。

## 5. 断链检查

`scripts/check_markdown_links.py`（CI 的 `quality` 作业会运行）检查被跟踪的 Markdown 中的相对链接是否指向存在的文件或目录，跳过：

- `docs/archive/2026-08*`、`docs/archive/2026-09*`（字节保真的历史快照）；
- 指向 `reports/`、`outputs/`、`data/`、`tmp/` 的链接（本地生成或忽略，干净检出里不存在）；
- 外部 URL、纯锚点、行内代码和代码块里的内容。

锚点（`#...`）不检查。`docs/link_check_baseline.json` 记录检查器引入时已存在的断链（目前为空）；新增断链会失败。批量修复后可用 `--update-baseline` 重写基线。

## 6. 移动与仅导入路径变化（A2）

搬迁可能改变模块名、相对导入、资源路径或导入时副作用。为辅助分类，每个文件除 `norm` 外还记一个 `body`：Python 文件是**去掉所有 `import` / `from … import` 语句后**的 token 流哈希（`yield from`、`raise … from` 不受影响），其他文件等于 `norm`。`body` 不参与整体身份哈希，也不证明可执行行为相同。

比较时：

- 一个被删除的文件与一个被新增的文件 `body` 相同 → 报告为**移动**（先按同名文件配对，再按路径顺序；空文件不配对）；
- 同一路径上 `norm` 变了但 `body` 没变 → 报告为**仅导入语句变化**；
- 其余照旧：行为变化、新增、删除、仅注释/排版。

`changes_identity` 仍严格包含移动和导入变化。兼容字段 `changes_behavior` 采用保守口径：身份变化即为真，表示需要行为审查，不表示已经证明输出不同。JSON 同时给出 `behavior_status="not_verified"`；Markdown 显示 `CHANGED ... behavior not verified`。`from math import floor` 改为 `ceil` 即使只改导入，也能改变结果。分层检查和测试是额外证据，不能自动证明导入目标与副作用等价。新候选仍须按原协议重新登记。方案与范围演进见 [ADR 0035](adr/0035-versioned-source-identity.md)。

标签与回执的绑定见 [发布约定](releasing.md) §4。
