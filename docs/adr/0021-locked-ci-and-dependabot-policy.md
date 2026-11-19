# 0021 CI 从锁文件安装；Dependabot 的范围

- 状态：已采纳
- 日期：2026-10-14
- 相关：[依赖与环境](../dependency_management.md)，[基础设施路线图](../infrastructure_roadmap.md) D3、D4，[ADR 0001](0001-editable-install-only.md)

## 背景
`requirements.txt` 只精确固定直接依赖，`requirements.lock.txt` 固定包括传递依赖在内的 73 个运行依赖，但 CI 之前只从 `requirements-dev.txt` 安装，锁文件仅被 `verify_lock.py` 检查"格式和摘要"。结果是 CI 绿了并不说明运行环境等于锁文件：传递依赖随时可能漂移。锁文件也只能手工改，没有再生成脚本，所以 Dependabot 的 pip 更新一直没法启用（它改不了锁文件和摘要）。

## 决定
- 新增 `scripts/regenerate_lock.py`：在干净的虚拟环境里安装 `requirements.txt`，`pip check` 之后读回解析结果，按规范化名称排序，写出带表头的 `requirements.lock.txt` 和 `requirements.lock.sha256`。`--check`（离线）核对：锁文件是规范形式、摘要匹配、`requirements.txt` 的每个直接依赖都在锁里且版本一致。
- CI 的 `quality` 作业改为 `pip install -r requirements.lock.txt -r requirements-dev.txt`，随后运行 `check_environment.py --strict-lock`（环境逐项等于锁）、`verify_lock.py` 和 `regenerate_lock.py --check`。开发工具仍在 `requirements-dev.txt` 里直接固定，不进运行锁。`dev.py setup` 与 dev container 使用同一安装命令，`dev.py check` 的 `env` 任务做同样的核对。
- 把现有锁文件重写为规范形式：加两行表头、`GitPython`/`gitdb` 两行调换顺序，**没有任何版本变化**，摘要随之更新。锁文件在 `config/frozen_scope.json` 的范围内，所以这次改动会出现在身份影响报告里；旧登记的身份与 `main` 本就相差 536 个文件（ADR 0008），新身份登记在整理之后，这一处不增加新的问题。
- Dependabot：GitHub Actions 改为**每月**一次，同时最多 2 个 PR；小版本与补丁合并为一个分组 PR，**主版本升级单独成 PR**，因此需要单独评审。pip 的版本更新**仍不启用**：Dependabot 不能运行 `regenerate_lock.py`，每个 PR 都会让 `--check` 失败。依赖升级由人在单独的 PR 里改 `requirements.txt` 并运行再生成脚本；仓库的安全警报保持开启，按同样方式处理。

## 备选方案
- 让 CI 只装 `requirements.lock.txt`、开发工具另外装：两次安装容易让后一次改动前一次的版本。合并成一条命令，让 pip 在同一次解析里满足两个文件。
- 启用 Dependabot 的 pip 并在工作流里自动再生成锁文件：需要给 CI 写权限去推送，超出当前约定（合并由所有者操作）。
- 生成带哈希的锁（`--require-hashes`）：更严格，但需要为每个平台的每个轮子登记哈希，当前运行依赖在 Windows 与 Linux、3.11 与 3.13 上轮子不同，维护成本高；留待有明确需求时再评估。

## 后果
- 锁文件与 `requirements.txt` 不一致、或 CI 环境漂移，会在 PR 里立刻失败，而不是静默通过。
- 在 Linux 上以 CI 的 Python 3.11 再生成锁文件：其他平台独有的依赖（例如 Windows 的 `colorama`）不会出现在锁里，pip 在该平台仍会作为依赖安装。
- 升级依赖的 PR 多了一步（运行再生成脚本），但它必须单独成 PR，并且评审可以直接读锁文件的差异。
- 2026-10-14 在改 CI 之前核对过：锁文件里 73 个固定版本在 PyPI 上，对 Linux x86_64 与 Windows × CPython 3.11/3.13 都有现成轮子（纯 Python 轮子或 manylinux/win_amd64 轮子），只有 `multitasking==0.0.12`（`yfinance` 的依赖）是纯 Python 源码包，现有 CI 本来就在构建它。这个核对没有替代 CI 的首次运行；若首次运行失败，先看是不是锁文件与 3.11 的差异。
- `install-smoke` 仍用 `pip install -e .`（按 `requirements.txt` 解析），它验证"干净克隆可安装"，与"锁定环境"是两件事，有意并存。
