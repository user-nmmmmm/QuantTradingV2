# ADR 0039：文档导航、引用式链接与历史报告入口

- 日期：2026-10-15。状态：已采纳。
- 范围：AUD-06；基于已合并 #104 的 main `6bced1a3d9273c18475a26317375092d61e0adc4`。

## 问题

README 模块树仍列出迁移前的 `core/*.py`，缺少 `ops/` 等实际包；依赖表漏列 Excel 的 `openpyxl`，环境说明没有区分完整质量作业与四组安装矩阵。Dashboard 的纯文本引用及模块导航标签仍指向已合并文件。原验收 JSON 和受保护归档里的旧报告路径需要保留可访问入口。链接检查器原先只识别行内 `](...)`，漏掉引用式 Markdown。

## 决定

1. README 的直接依赖表逐项对应 `requirements.txt`，补全 Excel 并说明 Node.js 22。安装命令使用运行锁与开发依赖，与质量 CI 一致；Python 3.11 的完整套件和 Ubuntu/Windows × 3.11/3.13 的指定回归矩阵分别表述。不改变依赖版本、锁文件或打包配置。
2. 模块地图使用实际包及 core 子包的相对链接，完整统计继续由架构清单生成。`architecture_inventory.py --check` 同时核对 README 导航块覆盖、Git 跟踪目标和模块文档标题路径；缺块、重复、遗漏新包或旧路径都会失败。已有架构图沿用迁移映射，不改图的历史交付字节。
3. 链接检查支持行内链接/图片，以及完整、折叠、快捷引用；先收集全文定义，再解析使用，标签大小写与连续空白归一化，重复定义取首个。保留使用处行号、迁移提示和零基线；相对目标解码 URL 转义，允许尖括号路径和常见带括号文件名。围栏/行内代码及 HTML 注释跳过；围栏只由相同字符且足够长的结束标记关闭。未绑定的方括号文本按 Markdown 字面文本处理。
4. 检查范围仍为 Git 跟踪的 Markdown；外部 URL、锚点有效性、原始 HTML 链接和生成物不在通用链接门禁范围。受保护的 2026-08/09 快照继续跳过。该工具不是完整的 CommonMark 渲染器。
5. `doc_index.json` 保持旧路径到当前文档的映射；映射本身不提供浏览器重定向。为 M-01～M-20、论文首轮执行、论文续办三条历史入口保留兼容 Markdown 页，转到合并文档已有的章节锚点。原合并研究报告、验收 JSON、受保护归档与原始实验产物不改写；历史文本里的旧报告名由兼容页承接。兼容页只负责跳转，不新增验收或交易准入结论。

## 验证与后续

回归覆盖引用的三种形式、定义顺序与重复、大小写/空白、图片、URL 转义、标题、围栏/代码/注释、列表续行和 CLI 非零退出及迁移提示；也覆盖依赖表与真实 pins 对齐、导航遗漏/旧路径/新子包和历史报告入口及锚点。文档与导航回归加入 Ubuntu/Windows × Python 3.11/3.13；原完整质量、离线、ML 与 CodeQL 作业保留。

公开依据：[#52 Dashboard 路径](https://github.com/user-nmmmmm/QuantTradingV2/pull/52#discussion_r4236987618)、[#52 历史论文报告](https://github.com/user-nmmmmm/QuantTradingV2/pull/52#discussion_r4236987622)、[#59 归档索引](https://github.com/user-nmmmmm/QuantTradingV2/pull/59#discussion_r4237200184)、[#64 引用式链接](https://github.com/user-nmmmmm/QuantTradingV2/pull/64#discussion_r4240133912)、[#68 运维迁移](https://github.com/user-nmmmmm/QuantTradingV2/pull/68#discussion_r4244242916)、[#76 analytics 迁移](https://github.com/user-nmmmmm/QuantTradingV2/pull/76#discussion_r4245070775)。#54 提到的安全文档当前已合并为实际存在的 `docs/live_safety.md`；私密披露渠道仍属于 AUD-07 的外部补证。本次不修改交易运行时、冻结范围、实际数据、发布绑定或登记状态。
