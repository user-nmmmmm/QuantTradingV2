# 0006 历史交付 ZIP 放到 GitHub Release，不重写 Git 历史

- 状态：已采纳
- 日期：2026-10-10
- 相关：[PR #62](https://github.com/user-nmmmmm/QuantTradingV2/pull/62)，`docs/file_retention.md`，`docs/file_retention_manifest.json`

## 背景
三个历史交付 ZIP（合计约 60 MiB）被 Git 跟踪，是文件保留规则列出的历史证据，不能仅因体积删除，也没有外部归档位置。

## 决定
- 先上传到 Release `evidence-archive-2026-09`，下载回来核对大小和 SHA-256 与保留清单一致，之后才取消跟踪。
- 清单新增 `external_archives`（名称、大小、SHA-256、地址）；`check_repository_hygiene.py` 校验元数据，本地有副本时核对哈希，并禁止它们重新被跟踪。
- **不改写 Git 历史**：这些字节仍在旧提交里，克隆体积不变。

## 备选方案
- `git filter-repo` 清除历史：会改变所有提交 SHA，而文档和证据里记录了大量提交哈希。
- 保持现状：工作树和每次克隆都带着这些文件。

## 后果
获取证据需要访问 Release；Release 资产不得被替换。若将来必须缩小仓库体积，需要单独的历史改写方案并同步更新所有记录的提交哈希。
