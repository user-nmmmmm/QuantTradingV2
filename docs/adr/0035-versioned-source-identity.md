# ADR 0035：版本化 f-string 身份、独立研究范围与保守导入结论

日期：2026-10-15。状态：已采纳。承接 AUD-02 首批 [ADR 0034](0034-frozen-replay-and-reference-verification.md)。

## 问题

独立研究字节身份遗漏 CLI 及拆分后的运行模块；冻结范围未包含控制入口和包发现的 `pyproject.toml`。导入剥离后的 body 相同只表明语法分类，不能证明导入目标、模块身份或副作用不变。v1 为跨 Python 版本稳定保留 f-string 原文，因而普通表达式排版也会改变身份。

## 决定

1. 研究身份保持独立的工作树字节清单，覆盖未跟踪的 CLI、研究和运行模块，以及配置、打包和依赖输入；不受 frozen scope 缩减影响。原有外部身份只记录、不认证来源的限制保留。
2. 冻结范围增加整个 `pyproject.toml`，按 LF 规范化字节核对。入口映射、包发现、依赖声明和工具配置的变动都可见，不尝试从 TOML 中挑选“安全”字段。
3. 默认新方案为 `code-token/v2`。普通 Python token 规则沿用 v1；f-string 先恢复完整原文，再解析表达式 AST 并按节点与字段序列化，不使用随版本改变省略规则的 `ast.dump`。保留字面量值、debug `=` 输出原文、转换、格式及嵌套表达式；不执行源码。无法规范化时退回严格字节哈希。
4. `code-token/v1` 及其金标准不改。CLI 验证自动使用保存的方案；API 显式把记录方案传给 manifest 构建器；拒绝跨方案混用和验证时覆盖方案。新绑定/登记使用 v2 并记录实际方案。升级必须新建记录，已有标签、回执及历史字节不改写。
5. `import_only` 和 `moved` 仍提供可读分类，严格摘要仍包含导入和路径。`changes_behavior` 保守表示潜在影响，包含这两类变化；JSON 的 `behavior_status=not_verified` 与 Markdown 的 `behavior not verified` 明示尚无运行等价性证明。分层检查及测试不能自动替代语义审查。

## 验证与后果

回归使用合成文件、Git 仓库和登记输入，覆盖未跟踪文件的增改删、运行中 CLI/打包变更、TOML 的入口/发现/依赖变更、导入目标改变实际数值、搬迁改变模块名，以及 f-string 排版、字面量、debug、转换、格式和嵌套变化。旧/新回执与发布绑定分别核对；原记录文件必须不变。安装 CI 在 Linux/Windows × Python 3.11/3.13 运行身份与发布回归，包括两个版本的金标准。

范围扩大和默认方案升级会改变新候选身份，需要按冻结协议重新登记；本次实现不执行真实登记、不沿用已观察天数，也不形成策略准入结论。更高 Python 版本独有语法不保证能在旧解释器规范化。

公开工程 roadmap 从 #98 整合到本实现 PR，并记录 #99、#100 的已合并修复及本批剩余四项。其他 AUD 包维持原有退出条件；工程修复与历史证据重建分开。

依据：[Python 3.13 f-string 语义](https://docs.python.org/3.13/reference/lexical_analysis.html#f-strings)、[AST FormattedValue / JoinedStr](https://docs.python.org/3.13/library/ast.html#ast.FormattedValue)。
