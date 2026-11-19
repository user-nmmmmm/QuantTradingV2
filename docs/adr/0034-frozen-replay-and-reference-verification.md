# 0034 AUD-02 首批：冻结回放导入与所选引用绑定

- 状态：已采纳（工具修复；不执行真实历史研究或候选登记）
- 日期：2026-10-15
- 基点：main `ef44a7d0`，已包含 AUD-01（PR #99）
- 相关：[公开审查 #72](https://github.com/user-nmmmmm/QuantTradingV2/pull/72#pullrequestreview-5486517986)、[公开审查 #71](https://github.com/user-nmmmmm/QuantTradingV2/pull/71#pullrequestreview-5486473399)、[工程 Roadmap PR #98](https://github.com/user-nmmmmm/QuantTradingV2/pull/98)、[身份说明](../identity.md)、[发布约定](../releasing.md)

## 问题

把冻结源码目录放在 `sys.path` 首位不能隔离缺失模块或预加载包。旧批次使用 `core.*` 布局，当前历史驱动改用了 `signal_meta.*` 等新路径，可能导入当前 checkout；原有来源检查也未覆盖全部 policy 模块。两个驱动属于哈希绑定的 historical 文件，不能直接改写以冒充原批次。

身份 CLI 又从当前 checkout 读取范围，即使 `--head` 指定另一提交，也可能漏掉该提交扩大的范围。发布标签检查在缺失 `origin/main` 时直接略过祖先关系验证，导致证据缺失时返回成功。

## 决定

1. 新增维护入口 `scripts/run_frozen_replay.py`，校验 `revised_manifest.json` 登记的源码及输入摘要，且只执行该清单中登记的原 meta／control 驱动。保留当前两个 historical 文件的字节；不执行当前 checkout 的历史驱动，不机械改写冻结包的导入路径。
2. 实际执行使用新建的 `python -I` 子进程。项目命名空间由独立 finder 限定在冻结树内；缺少包／模块、模块未登记或摘要变化都拒绝，不继续搜索当前源码或 editable-install finder。加载器直接编译核验后的源码字节，忽略可能不属于冻结记录的 `.pyc`。所有按项目命名空间导入的源码模块，包括 policy 类型与 fingerprint，逐个记录相对路径和摘要。
3. 必须提供批次外的新 provenance 文件，记录清单、原驱动和实际模块摘要及完成／失败状态。control 输出可先含原流程所需的 `official_p0_projection.json`，其摘要另记且执行后复查；已有结果或无关文件拒绝覆盖。meta 驱动保留原有 `--batch` 和批次内缓存语义。`--verify-only` 只表示验包成功，不声称已经回放或验证模块依赖。
4. 批次缺少冻结驱动或依赖时报告来源不足。恢复这类批次需要另行登记并验收适配器，不能以当前版本补齐、重新标成旧身份。隔离入口处理可信历史驱动的标准项目导入，不作为任意不可信 Python 代码的安全沙箱；显式外部适配器及第三方依赖仍受原驱动和运行环境的独立证据约束。
5. 身份 CLI 先把所选引用解析并固定为 commit SHA，再从同一树读取范围与源码。比较的 base/head 分别使用各自声明；WORKTREE 使用当前文件。commit 模式的自定义范围必须位于仓库内并从该树读取；发布绑定只允许标准范围。写出的范围摘要来自同一输入，核对新回执时也检查该摘要；旧回执的原有文件身份检查保持兼容。
6. 缺失引用、非 commit 引用、缺失范围声明和 Git 祖先检查错误均使工具失败。发布检查必须取得主线引用，不退回 HEAD、不跳过验证。登记测试的合成仓库显式提供 `origin/main`，并单独验证删除引用后的拒绝行为。

脚本生命周期清单为冻结入口与两类历史驱动增加两项有理由的精确例外：这是执行已核验冻结副本的边界，不允许维护链导入当前历史实现。

## 使用与验证

```bash
# 先验包，不运行研究
python scripts/run_frozen_replay.py --batch /path/to/batch \
  --runner run_strategy_review_meta.py --verify-only

# 冻结 control 驱动必须在清单内；输出可预先放入原流程的 projection 输入
python scripts/run_frozen_replay.py --batch /path/to/batch \
  --runner replay_strategy_review_controls.py \
  --output outputs/control-replay --provenance outputs/control-imports.json
```

只有来源可核验、且原驱动自身的输入／缓存／协议校验通过，才会执行并得到完成状态。本轮测试全部使用合成 Git 仓库和小型冻结包，不重新运行真实研究。

回归覆盖旧／新包布局、缺失 policy 的 checkout 回退、未登记模块、篡改的源码／输入、加载时再次核验、伪造的同时间戳 bytecode、缺失冻结驱动、仅验包模式、scope 扩张／收缩、旧 checkout 验证新 head、缺失 head/base/scope/main-ref 以及祖先命令错误。

标准库依据：[隔离解释器选项](https://docs.python.org/3.11/using/cmdline.html#cmdoption-I)、[importlib 查找与加载接口](https://docs.python.org/3.11/library/importlib.html#importlib.machinery.PathFinder)。

## 范围

这是 AUD-02 的首批三项，不关闭 CLI 独立研究身份、打包配置范围、import-only 语义表述或 f-string 规范化等其余排期。历史记录和标签不改写；工具验证不构成策略准入或实际 A5 登记。
