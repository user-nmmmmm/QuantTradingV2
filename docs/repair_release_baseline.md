# AUD-08：v3.1.1 修复发布基线与所有者 A5 步骤

这是已完成的 `v3.1.1` 发布与工程证据索引。`pyproject.toml` 版本为 `3.1.1`，
维护中的 CHANGELOG 将 #99–#113 的修复、重评、采用及依赖更新归入实际发布日期 `2026-10-19`。
[标签内的 CHANGELOG](https://github.com/user-nmmmmm/QuantTradingV2/blob/122489fab5d3f5b5d206ec77cd0c5dbc160e292a/CHANGELOG.md)
保留 `2026-10-17` 的历史准备文本，不移动标签或改写该快照。
所有者已于 2026-10-19 在最终提交 `122489fab5d3f5b5d206ec77cd0c5dbc160e292a`（#114 合并）上创建并推送
带注解标签 `v3.1.1`（tag object `2551bf22cd72ef4308262a4f925ecaa88951cc79`），并完成 A5 正式登记（第 5 节）。
工程测试、版本标签、身份绑定和 A5 正式登记分别记录。

**本次只更新公开文档，记录已发生的标签与登记；D1 全部保留，不删除分支或 worktree。**
登记之后 main 继续前进；#117 的 8 个冻结范围文件变化及 #120 合并后的维护基点见第 5 节。
最低 Python 现为 3.11.5，实际环境须按[依赖说明](dependency_management.md)安装更新。
2026-10-16 批准的[正式数据采用及研究上限](data_review/20261016_adoption.md)已随
[#109](https://github.com/user-nmmmmm/QuantTradingV2/pull/109) 合并到
[`321875d1`](https://github.com/user-nmmmmm/QuantTradingV2/commit/321875d1eb4cbf10104c627ca370276137b1e22e)：
27 份裁至 2026-08-26，MATIC 保留至 2024-09-09，其余 29 份研究结束日统一为 2026-08-26。
独立采用回执绑定数据提交 `3c00f84d5351ee9ea7e597eb17c7b93e3b2464ba`，
旧 review/未批准草案作为历史保留；main 的采用门禁已验证数据、回执和研究窗口。
`data_inputs.py verify` 通过只证明登记的文件身份一致；旧比较记录不能自动升级为新证据。
本次文档维护不启动新的观察时钟，不修改既有登记，也不改变策略准入状态；已完成的 A5 登记见第 5 节。

## 1. 修复基线与反例回归

原 AUD-08 修复索引的核对基点为 [9b20fcfa](https://github.com/user-nmmmmm/QuantTradingV2/commit/9b20fcfa6df1dbc3c08f8b8395f5122ffd339cc3)，
即 [#106](https://github.com/user-nmmmmm/QuantTradingV2/pull/106) 的合并提交；下表保留该批修复的来源。
2026-10-17 的发版准备基于 #113 合并后的 `f161d9539a3edd5c5e0390ab36cde69e9abd663d`；
实际发布最终采用 #114 合并提交 `122489fab5d3f5b5d206ec77cd0c5dbc160e292a`，见第 5 节。
旧带注解标签 `v3.1.0` 的 tag object 为 `268f51fb56535e926e4ed0066a0d238c907fd02e`，
仍指向 `ec4092131a18dd20a7b5605a50fffc99168fff75`；不得把它改指向修复后的代码。
按[发布约定](releasing.md)，该轮缺陷修复已使用 PATCH `v3.1.1`；后续候选必须使用新的版本。

下表只列本仓库公开实现及测试，实际运行结果以选定提交的 Actions 为准。

| 工作包 | 已合并的修复 | 反例与当前回归入口 |
|---|---|---|
| AUD-01 | [#99 / ef44a7d0](https://github.com/user-nmmmmm/QuantTradingV2/commit/ef44a7d0786f1eace0ecb0933482c59ec3e17102) | [test_bound_successor.py](../tests/test_bound_successor.py)：登记/打开竞争、不同父批次争用目标、中断不暴露半成品、失败后重试及已有输出拒绝覆盖 |
| AUD-02 | [#100 / f91e6bc9](https://github.com/user-nmmmmm/QuantTradingV2/commit/f91e6bc91ba4b32748d03608844c6d5d0e17de73)、[#101 / f242a557](https://github.com/user-nmmmmm/QuantTradingV2/commit/f242a55767ad5d0d72bc7dec0aba4cf3267e6dbb) | [test_frozen_replay.py](../tests/test_frozen_replay.py)、[test_source_identity.py](../tests/test_source_identity.py)、[test_release_check.py](../tests/test_release_check.py)：冻结导入来源、选定 head 的 scope、缺失引用拒绝、研究/打包身份、import-only 表述、v1/v2 与 f-string |
| AUD-03 | [#102 / 1278c50a](https://github.com/user-nmmmmm/QuantTradingV2/commit/1278c50a24d2373324f8f2aa8bef7984596912e7) | [test_data_input_integrity.py](../tests/test_data_input_integrity.py)：实际快照绑定、抓取前后变更拒绝、末根不变量、空响应/未知文件名、cache 换行兼容且拒绝内容变化 |
| AUD-04 | [#103 / 4e3669e5](https://github.com/user-nmmmmm/QuantTradingV2/commit/4e3669e57dc3148a017f470c0f0e165eeebcf70a) | [test_quality_ratchet.py](../tests/test_quality_ratchet.py)、[test_quarantine_policy.py](../tests/test_quarantine_policy.py)、[test_dev_tasks.py](../tests/test_dev_tasks.py)：只减不增、实际隔离数量/期限、完整本地/CI 门禁一致、pyproject 触发容器 |
| AUD-05 | [#104 / 6bced1a3](https://github.com/user-nmmmmm/QuantTradingV2/commit/6bced1a3d9273c18475a26317375092d61e0adc4) | [test_aud05_runtime_semantics.py](../tests/test_aud05_runtime_semantics.py)、[test_nonpositive_equity_metrics.py](../tests/test_nonpositive_equity_metrics.py)：零负权益、真实结尾与超过 100% 回撤、未定义收益、市场别名及宿主 hook 分派 |
| AUD-06 | [#105 / e778c56b](https://github.com/user-nmmmmm/QuantTradingV2/commit/e778c56b4639f9cd5e3bf192687e194ace0271ba) | [test_markdown_links.py](../tests/test_markdown_links.py)、[test_documentation_navigation.py](../tests/test_documentation_navigation.py)、[test_architecture_inventory.py](../tests/test_architecture_inventory.py)：引用式链接、真实布局、依赖说明和历史报告兼容入口 |
| AUD-07 | [#106 / 9b20fcfa](https://github.com/user-nmmmmm/QuantTradingV2/commit/9b20fcfa6df1dbc3c08f8b8395f5122ffd339cc3) | [test_repository_governance.py](../tests/test_repository_governance.py)：逐 ref/SHA/worktree 清单、状态/历史不足/漂移拒绝、本地输出限制；外部渠道与补审见第 4 节 |
| 选择器/归档后续 | [#111](https://github.com/user-nmmmmm/QuantTradingV2/pull/111)，[ADR 0042](adr/0042-captured-selector-bytes-and-retained-archive-identities.md) | [test_selector_snapshot_integrity.py](../tests/test_selector_snapshot_integrity.py)、[test_external_archive_integrity.py](../tests/test_external_archive_integrity.py)：验证、推断、回执与快照绑定相同字节，非便携/大小写冲突路径拒绝；独立归档身份约束 |
| 依赖与环境后续 | [#112](https://github.com/user-nmmmmm/QuantTradingV2/pull/112)、[#113](https://github.com/user-nmmmmm/QuantTradingV2/pull/113) | [test_dependency_runtime_compatibility.py](../tests/test_dependency_runtime_compatibility.py)：实际 HTTP/WebSocket、签名/报表，Unicode 域名、原生/纯 Python 请求头与选择器结果；环境及元数据共同要求 Python 3.11.5+ |

9b20fcfa 的主线运行：[tests](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/38703618818)、
[offline](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/38703619048)、
[CodeQL](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/38703618983)。
历史 #113 合并基点 `f161d9539a3edd5c5e0390ab36cde69e9abd663d` 的
[tests](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/39030639436)
为 4735 passed、15 skipped、111 subtests passed、90.73% 覆盖率，四组安装各 454 passed、ML 292 passed、前端 46 passed；
[offline](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/39030639413)
为 44 passed、6 subtests passed；
[CodeQL](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/39030639352) 和
[devcontainer](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/39030639367) 同一 SHA 成功，
[pip 依赖图更新](https://github.com/user-nmmmmm/QuantTradingV2/actions/runs/39030644223)也已成功。
带采集时间的 Dependabot 重扫回执保留本地，不用 CI 成功代替告警状态核对。
这些历史/基点运行不代替后续候选及其最终合并提交的检查。
正式选定提交应有同一 SHA 的 tests、offline、CodeQL 和 devcontainer 成功记录。
本次只改文档，不修改已有的 `pyproject.toml` 版本；devcontainer 的路径过滤不会自动触发。
没有同一 SHA 的容器记录时，在 Actions 选择目标分支手动运行 devcontainer，并核对运行的 head SHA。
命令契约见 [Git 标签说明](https://git-scm.com/docs/git-tag)、
[工作流运行查询](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository)及
[手动运行工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。

## 2. 受影响的历史证据

| 记录 | 本轮处置与后续要求 |
|---|---|
| 已推送标签与旧绑定 | 保留原对象、原方案和原字节；`code-token/v1` 记录按 v1 验证，不能用 v2 重算后覆盖。新登记使用新标签和 `code-token/v2` |
| 旧候选协议、实验登记与观察窗 | 保留原件；既往打开/访问记录不能清除。新登记从实际操作时间重新起算，不继承旧等待天数；旧协议不因新代码 CI 通过而重新有效 |
| 冻结历史回放 | 只执行冻结清单中的原驱动和模块；缺失来源继续拒绝。补来源或适配器须独立记录，不能用当前实现冒充原结果 |
| 30 份冻结 CSV 与旧佐证 | 原始字节与 legacy 记录保留在来源 Git 提交；#109 已按批准选择采用前缀，以独立回执绑定数据提交、采用摘要和研究窗口。新清单的 v2 匹配来自保存的公开响应重放，未知来源和历史可得性不升级 |
| 旧报表与指标 | 按生成时口径保留。公式 2.1 修复零负权益及未定义收益；如需比较，生成新目录中的独立结果并记录代码/输入/公式身份，不覆盖旧指标或混合两套口径 |
| 受保护归档与历史验收 JSON | 继续使用归档/卫生门禁；历史报告路径通过兼容入口解析。工程修复不把历史研究 fail、未知来源或未完成项改成 pass |

原 AUD-08/#107 的冻结范围变化只有 `pyproject.toml` 版本元数据。
#109 数据采用另改变 `cli/data.py` 的研究读取边界，属于冻结源码身份的行为变化；发布必须生成新绑定。
#111 还改变选择器推断、模型构建与快照的源码身份；#112/#113 改变直接依赖、运行锁及打包元数据。
本次收尾仅修改文档，不再改变冻结源码、输入或配置；旧标签的绑定不能覆盖这些后续变化。
本地的预备身份/模拟标签绑定仅用于核对工具链；正式绑定的 `tag_commit` 必须是所有者最终选定的合并 SHA。

## 3. 所有者操作：发布、绑定、再登记

**历史操作记录：第 3.1–3.3 节是 `v3.1.1` 发布准备时的步骤，已于 2026-10-19 完成，结果见第 5 节。**
以下保留当时的 PowerShell 模板、`v3.1.1` 变量及前置检查，供追溯，不是再次创建该标签或重跑 A5 的待办。
旧 ADR 0032 中的 `v3.1.0` 也是当时的发布计划。后续候选按第 3.4 节准备新的版本、绑定和登记。
任何命令非零、身份不符或前置证据未满足都应停止。

### 3.1 合并发版收尾，选定最终提交

1. #107–#110 的发布准备、重评、批准采用及前次文档收尾，以及 #111–#113 后续修复均已合并；先合并本次公开文档更新，运行
   `python scripts/dev.py data data-review data-adoption` 核对批准选择、数据提交和研究边界。
   `python scripts/data_inputs.py corroborate` 会联网并**写回冻结清单**，若需更新证据，必须另行审阅并形成新采用回执，
   不能在已打标签的检出上运行后继续沿用旧绑定。
2. 发版收尾或任何后续候选变更合并后，重新选定待发布的 main SHA，核对同一提交的完整 CI。
   如果 `v3.1.1` 已经推送而候选又改变，应准备下一个版本，不能移动标签或复用旧绑定。
   当时 CHANGELOG 使用 2026-10-17 准备日期；实际发版为 2026-10-19，维护条目现已校正，标签内的准备快照保留原样。
3. 在完整、干净的 Git 检出中执行。公开数据的技术采用回执见上面的采用记录；
   真实父批次、完整审计包及 A5 私密登记资料从本地保存位置取得，继续保留本地。

```powershell
$ErrorActionPreference = 'Stop'
$releaseTag = 'v3.1.1'
git fetch origin
if ($LASTEXITCODE -ne 0) { throw 'fetch failed' }
$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'use a clean checkout' }
$releaseSha = (git rev-parse --verify origin/main).Trim()
if ($LASTEXITCODE -ne 0) { throw 'origin/main is unavailable' }
git switch --detach $releaseSha
if ($LASTEXITCODE -ne 0) { throw 'checkout failed' }
python -m scripts.dev check --no-tests
if ($LASTEXITCODE -ne 0) { throw 'local checks failed' }
python scripts/release_check.py $releaseTag
if ($LASTEXITCODE -ne 0) { throw 'version/changelog mismatch' }
```

在 GitHub Actions 按 `$releaseSha` 核对四个工作流。也可用公开只读 API 严格检查该提交的最新运行：

```powershell
$runs = Invoke-RestMethod -Uri "https://api.github.com/repos/user-nmmmmm/QuantTradingV2/actions/runs?head_sha=$releaseSha&per_page=100"
foreach ($name in @('tests', 'Backtest automation (offline)', 'codeql', 'devcontainer')) {
    $run = $runs.workflow_runs | Where-Object { $_.name -eq $name -and $_.head_sha -eq $releaseSha } |
        Sort-Object -Property created_at -Descending | Select-Object -First 1
    if (-not $run -or $run.status -ne 'completed' -or $run.conclusion -ne 'success') {
        throw "missing, pending or failed workflow at ${releaseSha}: $name"
    }
}
python scripts/data_inputs.py verify
if ($LASTEXITCODE -ne 0) { throw 'input identity mismatch' }
```

API 检查通过后仍须核对独立的数据采用回执：`verify` 输出中的 legacy/unverified 数量不是研究放行。
若采用决定要求进一步修改候选，回到本节重新固定 SHA、版本与 CI。

### 3.2 创建并验证新标签和预登记绑定

确认远端和本地都没有同名标签，再创建带注解标签。已有标签时停止并核对原对象，不能强制覆盖。

```powershell
$remoteTag = git ls-remote --tags origin "refs/tags/$releaseTag" "refs/tags/$releaseTag^{}"
if ($LASTEXITCODE -ne 0 -or $remoteTag) { throw 'remote tag exists or could not be checked' }
git show-ref --verify --quiet "refs/tags/$releaseTag"
if ($LASTEXITCODE -ne 1) { throw 'local tag exists or could not be checked' }
git tag -a $releaseTag $releaseSha -m 'Release 3.1.1: audited engineering fixes'
if ($LASTEXITCODE -ne 0) { throw 'tag creation failed' }
python scripts/release_check.py $releaseTag --verify-tag --main-ref origin/main
if ($LASTEXITCODE -ne 0) { throw 'annotated tag verification failed' }
$bindingDir = "outputs/a5-preflight/$releaseSha"
New-Item -ItemType Directory -Path $bindingDir | Out-Null
$bindingPath = "$bindingDir/identity_binding.json"
python scripts/identity_report.py --bind-release $releaseTag --write-binding $bindingPath
if ($LASTEXITCODE -ne 0) { throw 'binding creation failed' }
python scripts/identity_report.py --verify-binding $bindingPath --head $releaseSha --fail-on-change
if ($LASTEXITCODE -ne 0) { throw 'binding verification failed' }
$binding = Get-Content -LiteralPath $bindingPath -Raw | ConvertFrom-Json
if ($binding.tag_commit -ne $releaseSha -or $binding.receipt.scheme -ne 'code-token/v2') {
    throw 'binding must name the selected commit and current scheme'
}
git push origin "refs/tags/$releaseTag"
if ($LASTEXITCODE -ne 0) { throw 'tag push failed; inspect existing objects before any retry' }
$published = git ls-remote --tags origin "refs/tags/$releaseTag^{}"
if ($LASTEXITCODE -ne 0 -or -not $published -or ($published -split '\s+')[0] -ne $releaseSha) {
    throw 'published annotated tag does not resolve to the selected commit'
}
```

标签与预登记绑定仍不是 A5 回执。标签检查只要求主线可达；上述额外 SHA 检查固定实际候选。
保存 tag object、commit、scope/identity 摘要、完整 CI URL 和数据采用回执到本地。
若推送后继续改候选，保留该标签，按新版本重新准备。

### 3.3 所有者执行一次 A5 正式登记

前置确认：真实父批次的协议仍未打开、无访问记录、无 `.opened` 回执；实验登记与父配置的摘要匹配。
`capital_config_change_statement_20261014.json` 必须仍与父快照及新标签配置吻合。
登记器会在共享进程锁内重新检查这些条件；事前阅读不能替代最终检查。
同时确认第 3.1 节的数据采用门槛已满足，保存 AUD-07 未完成项及其责任人/影响范围。
登记器本身不核验 GitHub CI 或真实数据采用回执；命令能够通过，也不能跳过这些前置检查。

```powershell
$currentHead = (git rev-parse --verify HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $currentHead -ne $releaseSha) { throw 'checkout changed since release preparation' }
$dirty = git status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'checkout must still be clean' }
python scripts/release_check.py $releaseTag --verify-tag --main-ref origin/main
if ($LASTEXITCODE -ne 0) { throw 'release tag verification failed' }
python scripts/identity_report.py --verify-binding $bindingPath --head WORKTREE --fail-on-change
if ($LASTEXITCODE -ne 0) { throw 'working candidate differs from the prepared binding' }
# 从本地保存位置取得真实父批次；新的干净检出不一定包含被忽略的 reports。
$historicalBatchPath = '替换为本地真实父批次的绝对路径'
$historicalBatch = (Resolve-Path -LiteralPath $historicalBatchPath).Path
$statement = 'docs/research/capital_config_change_statement_20261014.json'
$registrationDate = (Get-Date).ToUniversalTime().ToString('yyyyMMdd')
$registrationOutput = "reports/roadmap_v3/A5/$registrationDate-bound-successor-v3.1.1"
# 目录必须全新，不能用本地演练结果替代。
$parentBefore = (Get-FileHash -Algorithm SHA256 -LiteralPath "$historicalBatch/prospective_protocol.json").Hash
python scripts/register_bound_successor.py --tag $releaseTag --historical-batch $historicalBatch --config-change $statement --output $registrationOutput
if ($LASTEXITCODE -ne 0) { throw 'registration refused; inspect the cause and any existing complete receipt' }
foreach ($name in @('review_protocol.json', 'parent_protocol.json', 'config_change.json', 'identity_binding.json', 'prospective_protocol.json', 'acceptance.json')) {
    if (-not (Test-Path -LiteralPath "$registrationOutput/$name" -PathType Leaf)) { throw "missing registration artifact: $name" }
}
$acceptance = Get-Content -LiteralPath "$registrationOutput/acceptance.json" -Raw | ConvertFrom-Json
$formalBinding = Get-Content -LiteralPath "$registrationOutput/identity_binding.json" -Raw | ConvertFrom-Json
if ($acceptance.tag_commit -ne $releaseSha -or $acceptance.tag -ne $releaseTag -or
    $acceptance.identity_scheme -ne 'code-token/v2' -or $acceptance.code_hash -ne $binding.receipt.identity_sha256 -or
    $formalBinding.tag_commit -ne $releaseSha -or $acceptance.opened_at -ne $null -or
    $acceptance.admission -ne 'paused_revalidation' -or $acceptance.old_protocol_modified -ne $false -or
    $acceptance.new_observation_inherits_elapsed_days -ne $false) { throw 'registration receipt mismatch' }
$formalBindingHash = (Get-FileHash -Algorithm SHA256 -LiteralPath "$registrationOutput/identity_binding.json").Hash.ToLowerInvariant()
if ($acceptance.identity_binding_sha256 -ne $formalBindingHash) { throw 'formal binding hash mismatch' }
foreach ($entry in @(@('config_change_sha256', 'config_change.json'), @('parent_protocol_sha256', 'parent_protocol.json'))) {
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath "$registrationOutput/$($entry[1])").Hash.ToLowerInvariant()
    if ($acceptance.($entry[0]) -ne $actualHash) { throw "artifact hash mismatch: $($entry[1])" }
}
$protocol = Get-Content -LiteralPath "$registrationOutput/prospective_protocol.json" -Raw | ConvertFrom-Json
$registryHash = (Get-FileHash -Algorithm SHA256 -LiteralPath "$registrationOutput/review_protocol.json").Hash.ToLowerInvariant()
if ($protocol.code_hash -ne $acceptance.code_hash -or $protocol.config_hash -ne $acceptance.config_hash -or
    $protocol.experiment_registry_hash -ne $registryHash -or $protocol.protocol_hash -ne $acceptance.protocol_hash -or
    $protocol.status -ne 'pending_unseen_evidence' -or $protocol.opened_at -ne $null -or $protocol.data_access_log.Count -ne 0) {
    throw 'prospective protocol identity or state mismatch'
}
python -c 'import json,sys; from pathlib import Path; from analysis.strategy_review import validate_prospective; validate_prospective(json.loads(Path(sys.argv[1]).read_bytes()))' "$registrationOutput/prospective_protocol.json"
if ($LASTEXITCODE -ne 0) { throw 'protocol hash/window validation failed' }
python scripts/identity_report.py --verify-binding "$registrationOutput/identity_binding.json" --head $releaseSha --fail-on-change
if ($LASTEXITCODE -ne 0) { throw 'formal binding verification failed' }
$parentAfter = (Get-FileHash -Algorithm SHA256 -LiteralPath "$historicalBatch/prospective_protocol.json").Hash
if ($parentAfter -ne $parentBefore -or $acceptance.parent_protocol_sha256 -ne $parentBefore.ToLowerInvariant()) {
    throw 'parent protocol changed or parent identity mismatch'
}
```

另核对 `acceptance.json` 的配置/声明/父协议摘要与对应文件，
`prospective_protocol.json` 的 code/config/experiment registry 身份与登记回执一致，
状态为 `pending_unseen_evidence`、`opened_at` 为空、访问日志为空。
保存全部六个文件及校验摘要，至少保留一份独立的本地备份。
从**实际回执**读取 `registered_at`、`test_start`、`test_end_exclusive`、`mature_after`，
不得预填日期、沿用旧观察窗或继承已过等待时间。

成功后由所有者按真实回执维护阶段记录与观察窗。完整回执和私密证据保持本地；
若要更新公开文档，应先抽取经允许的工程摘要，不能把原始审计包或登记资料直接加入 PR。
失败重试时：锁竞争/发布前失败可在原因消除后重试；已有正式目录时先验证完整回执，
不能删除已登记目录再通过重跑改变时钟。

### 3.4 后续候选：新的发布与登记

`v3.1.1` 的标签和登记已完成。若所有者决定将后续 main 纳入新的观察候选，须按[发布约定](releasing.md)
选择新版本，写明实际发布日期，固定最终 main SHA，并核对同一 SHA 的 CI。
使用新版本对应的标签、新绑定及全新的登记输出目录；重新核对配置、父协议、数据采用和冻结范围身份。
第 3.1–3.3 节中的旧版本、SHA、目录和日期不能直接复用；新窗口及成熟日从新的实际回执读取。
不得移动 `v3.1.1`、修改旧绑定、覆盖或删除既有登记目录，也不得通过重跑沿用或改变已登记时钟。
工程测试通过不自动批准候选、开始登记或授予交易准入。

## 4. AUD-07 外部补证单列

详细操作见[所有者治理清单](repository_governance.md)，不因本发布准备或 CI 成功关闭：

| 项目 | 当前证据边界 | 退出条件 |
|---|---|---|
| 私密报告接收 | 已有启用状态与公开入口证据；普通报告者体验及通知投递仍需所有者核实 | 明确监控责任人、账号通知设置与私密接收证据；不公开私密报告 URL/账户信息 |
| D1 保留 | 本轮全部保留，不安排清理；清单、保留与治理记录只留本地 | 继续保留所有分支/worktree；以后若改变决定，重新核对逐对象清单并另行批准，不将缺失或 prunable 当作删除许可 |
| #47–51、#61、#62 独立审查凭证 | #111 的工程后续已合并；治理清单保留原始 head 和历史 quota 通知，不能把已修复实现与第三方独立审查凭证混为一项 | 如需独立复核，保留逐原始 SHA/范围、结果链接、发现处置及测试；未覆盖范围和历史研究限制继续列出，私密审计不公开 |

普通报告者体验、通知接收和独立审查凭证继续单列，不因本轮 CI 或依赖更新关闭。
#109 的数据采用已完成；28 份来源未核验与历史可得性未知仍保留。完整审计包、治理和登记资料继续本地保留。

## 5. v3.1.1 标签与 A5 登记（工程摘要）

以下只记录可公开的工程事实，完整回执、绑定文件和父批次资料仅留本地。

| 项目 | 记录 |
|---|---|
| 标签 | 带注解标签 `v3.1.1`，tag object `2551bf22cd72ef4308262a4f925ecaa88951cc79`，指向 `122489fab5d3f5b5d206ec77cd0c5dbc160e292a`（#114 合并）；2026-10-19 由所有者创建并推送，`release_check.py v3.1.1 --verify-tag --main-ref origin/main` 通过。`v3.1.0` 与 `v3.0.0` 未移动 |
| 选定提交的 CI | 同一 SHA 的 tests、offline、CodeQL、devcontainer 均 completed/success；本机 `dev.py check --no-tests` 全部通过 |
| 登记 | A5 正式登记于 2026-10-19 07:38 UTC，身份方案 `code-token/v2`，绑定标签 `v3.1.1`；父批次为 `followup-successor-03`，登记前后协议均未被打开，访问日志为空 |
| 观察窗 | `test_start` 2026-11-19，`test_end_exclusive` 2027-05-18，`mature_after` 2027-06-07（均 UTC，取自实际回执，不继承旧窗口的等待天数）；`admission` 为 `paused_revalidation`，研究状态 `pending_unseen_evidence` |
| 只读复核 | 六个登记文件齐全；回执与绑定、配置变更声明、父协议、实验登记的摘要互相吻合；父协议的当前摘要等于回执记录值；`validate_prospective` 与正式绑定校验通过。目录旁的 `.lock` 文件为 0 字节残留，不含数据 |
| 旧登记 | `v3.1.0`（`code-token/v1`）的登记保留原样，不替换；它不覆盖 #99–#113 的修复 |

**登记之后 main 已前进：** [#117](https://github.com/user-nmmmmm/QuantTradingV2/pull/117)（`fd3eaf22`）修改了
`core/broker/__init__.py`、`core/broker/matching.py`、`core/risk/entry_policy.py`、`core/risk/portfolio_governor.py`、
`core/trading/entry_risk.py`、`core/trading/execution_port.py`、`core/trading/position_management.py`、`strategies/base.py`。
对该登记的绑定运行 `identity_report.py --verify-binding --head fd3eaf22 --fail-on-change` 报告了这 8 个文件的身份变化，
行为未验证。按协议，针对旧身份冻结的候选在把这些改动视为同一候选之前必须重新登记；
上述 #117 检查仅覆盖当时的提交，不替代后续 main 的身份核对。2026-10-30 维护核对采用
[#120](https://github.com/user-nmmmmm/QuantTradingV2/pull/120) 合并提交
[`2d9710cc0c663bb8ccbef45620bfc5e8953d8d46`](https://github.com/user-nmmmmm/QuantTradingV2/commit/2d9710cc0c663bb8ccbef45620bfc5e8953d8d46)，
同一 SHA 的公开 CI 记录见[基础设施路线图第 10 节](infrastructure_roadmap.md#10-2026-10-15公开github复核)。
该维护基点不等同于已登记的 `v3.1.1` 候选。是否准备新版本并再次登记由所有者决定，
按第 3.4 节处理；不能移动 `v3.1.1`，也不能删除已登记目录重跑。
