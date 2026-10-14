# 0023 静态分析、开发容器、平台矩阵与架构清单的结论

- 状态：已采纳
- 日期：2026-10-14
- 相关：[基础设施路线图](../infrastructure_roadmap.md) D5、D6，[架构清单](../architecture_inventory.md)，[质量门槛](../quality.md)

## 背景
路线图 D5 要求对静态分析（CodeQL）、dev container 的实际验证、`install-smoke` 的平台范围各给出"启用 / 不启用 + 理由"；D6 要求架构图与代码一致且有检查。依据（2026-10-14 实测）：仓库是公开的（CodeQL 免费）；`tests` 工作流最近 25 次运行全部成功；本机没有 Docker；`架构图/` 里的交付图渲染自早期版本，引用了 16 个已搬迁的模块路径，其渲染器是外部工具，视觉核验回执绑定 HTML 的字节哈希。

## 决定
| 项 | 结论 | 理由 |
|---|---|---|
| CodeQL | **启用**（`.github/workflows/codeql.yml`：Python 与 JavaScript，`build-mode: none`，推送到 `main`、PR 和每周一次；默认查询集） | 公开仓库免费；项目处理交易所密钥与下单，值得有一道独立于 ruff/mypy 的安全扫描。**不设为必需检查**：先观察误报量，结果在 Security → Code scanning 查看，不阻塞合并。若三个月内误报多于有效发现，改为只在每周计划任务上运行 |
| dev container | **保留，但仍标注"未实际运行"**；`postCreateCommand` 改为 `python scripts/dev.py setup`（与 CI 同一条安装路径） | 本机没有 Docker，无法验证镜像本身；容器内的命令与 CI 相同，剩下的风险只在基础镜像。有 Docker 的人第一次打开时验证，结果记录到本 ADR 的后续小节 |
| `install-smoke` 平台 | **不增加**，维持 ubuntu/windows × 3.11/3.13 | 近 25 次运行稳定；贡献者实际使用 Windows 与 Linux，没有 macOS 使用者；矩阵的上下界 3.11（CI 解释器）与 3.13（`.python-version`）已覆盖区间两端 |
| 架构图（D6） | 交付图 `架构图/` **不重新渲染也不改写**；新增自动生成的 `docs/architecture_inventory.md` 作为随代码更新的版本，并给交付图加路径移动表 `架构图/path_moves.json` | 渲染器不在仓库，且视觉核验回执绑定 HTML 哈希，改写会使回执失效。清单由 `scripts/architecture_inventory.py` 从 import-linter 契约和 Git 跟踪的文件生成，CI 用 `--check` 保证最新；检查同时验证交付图规格里每个源码路径要么存在、要么在移动表里指向存在的文件，所以以后再搬迁模块时会逼着更新移动表 |
| 文档数量（D6） | 约定写入 `docs/README.md`：一个主题一个文件、`doc_index.json` 只追加、生成文档不手工编辑、季度回看 | 不设硬上限，避免为数量而合并；用约定而不是计数门槛 |

## 后续：dev container 的验证（2026-10-14）
本机仍没有 Docker，但 GitHub 的 runner 有。新增工作流 `.github/workflows/devcontainer.yml`（`devcontainers/ci`）：在 `.devcontainer/`、`scripts/dev.py`、`requirements*.txt` 变化时、每周一次和手动触发时，构建容器、运行 `postCreateCommand`（`dev.py setup`），再在容器里运行 `python scripts/dev.py check --no-tests`。它不是必需检查。首次运行（PR 上的 `build-and-check`）约 2 分钟通过，所以上表中"仍标注未实际运行"的结论不再成立：dev container 现在由 CI 验证，不再依赖某个有 Docker 的人。

## 备选方案
- CodeQL 设为必需检查：误报会卡住所有 PR，先观察再决定。
- 重新渲染交付图：需要外部渲染器和浏览器视觉核验，且会让旧回执失效；自动生成的清单已经满足"与代码一致且有检查"。
- 把交付图的路径文本直接替换成新路径：改变 HTML 字节，使视觉核验回执的哈希失效，而没有重新核验。
- 增加 macOS：无使用者，成本是每次 PR 多一个慢速作业。

## 后果
- 每个增删模块或子包的 PR 需要运行 `python scripts/architecture_inventory.py --write` 并提交；`dev.py check` 的 `arch` 任务会提醒。
- 首次 CodeQL 运行后要有人看一遍结果并分类；有效发现按普通缺陷处理，误报在 Code scanning 中关闭并写明原因。
- 若将来要更新交付图，从 `架构图/quant_trading.architecture.json` 重新渲染并重新做视觉核验，同时清空 `path_moves.json`。
