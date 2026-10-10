# 文件保留与生成物规则

本规则记录 2026-09-26 的仓库文件整理结果。受保护的历史证据清单及 SHA-256 见 [`file_retention_manifest.json`](file_retention_manifest.json)。Git 忽略规则只控制新文件；已经跟踪的文件仍会出现在 Git 中，因此历史例外由清单列明。

| 路径 | 处理方式 | 用途与责任 |
| --- | --- | --- |
| `tmp/` | 本地工作目录，整体忽略。可复用代码需移到 `scripts/`、正式包或测试目录后再提交。 | 工作区快照、一次性审核脚本、测试输出和依赖安装副本；执行者负责确认是否仍需使用。 |
| `docs/references/regime_aware_multiaxial_signal_meta_layer.pdf` | 保留跟踪。 | 从 `tmp/pdfs/` 移出的研究原件；页面 PNG 是可重新渲染的中间产物，已清理。 |
| `reports/` | 新生成的目录、ZIP、日志和测试输出默认忽略；清单中的六个历史文本证据继续跟踪，三个交付 ZIP 已迁到 GitHub Release（见下）。 | 交付与验收证据；仓库维护者负责保留与核验。 |
| `data/binance/1d/` | 现有 30 个 CSV 和 `_manifest.json` 继续跟踪；新下载的行情默认忽略。 | 本地回测数据；只有清单实际列出的文件才能用其中的来源、范围和校验值核对。 |
| `.claude/settings.local.json`、`.vscode/settings.json` | 仅保留本机副本，不再跟踪。 | 本机工具权限与 Windows 虚拟环境设置；各开发者自行维护。 |
| `cua_probe.txt` | 已删除。 | 无代码引用的临时探针。 |

`tmp/merge_trend_v2_20260920/` 是旧工作区快照，现存 Python/PowerShell 源码在仓库正式位置有对应路径。其他 `tmp/` 子目录中仍有一次性路线图审核、验收和报告生成脚本；这些本地文件未被批量删除。部分测试目录无法读取，保留原状。将来如果某个脚本需要稳定运行，先核对其输入和副作用，再移入正式目录并加入脚本索引。

三个历史交付 ZIP（共约 60.4 MiB）已于 2026-10-10 迁出工作树：原文件先上传到 GitHub Release `evidence-archive-2026-09`，下载回验大小与 SHA-256 与清单一致后，才从 Git 跟踪中移除。清单 `external_archives` 记录文件名、大小、SHA-256 和下载地址；`check_repository_hygiene.py` 校验这些元数据，并在本地存在副本时核对哈希，同时禁止它们重新被跟踪。这些文件仍在迁出之前的 Git 历史中，未改写历史，所以克隆体积不会因此变小。使用时从 Release 下载到 `reports/`（已被 `.gitignore` 忽略），先核对哈希。不要只因为文件较大而删除历史交付物，也不要覆盖 Release 里的资产。

`data/binance/1d/` 的现有文件属于历史例外，当前工作区中的 BTC、ETH CSV 与 `_manifest.json` 已有其他进行中的改动，本轮整理未改写它们。新的行情下载仍由 `.gitignore` 排除；若要将新增数据纳入可复现基线，应先定义固定范围、提供来源与校验值，并明确加入版本控制。

当前 `_manifest.json` 仅列出 BTC/USDT 与 ETH/USDT 两个 CSV；其余 28 个已跟踪 CSV 尚无此清单中的来源、范围和 SHA-256 记录。仓库卫生检查只验证 30 个 CSV 与清单文件存在，并未逐一核对数据哈希。因此，这个可更新的下载缓存清单不能替代覆盖全部文件的冻结研究数据清单；正式复现需要为所用输入单独固定身份和校验值。
