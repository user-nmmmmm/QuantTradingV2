# 0001 只支持可编辑安装，不支持分发 wheel

- 状态：已采纳
- 日期：2026-10-10
- 相关：[PR #60](https://github.com/user-nmmmmm/QuantTradingV2/pull/60)，`pyproject.toml`

## 背景
项目没有打包元数据，76 个脚本、测试和入口文件各自用 `sys.path.insert/append` 凑导入，换一个工作目录就可能失效。许多模块又按**检出目录**定位 `config/`、`data/`、`reports/`（例如 `Path(__file__).parents[1]`），所以并不是一个与位置无关的库。

## 决定
- `pyproject.toml` 提供完整的 `[project]`，运行依赖从 `requirements.txt` 动态读取（版本固定仍只在 requirements 与锁文件里维护）。
- 支持的安装方式只有 `pip install -e .`（可编辑安装）；不支持把 wheel 装到别处。
- 删除 `sys.path` 补丁（71 个文件）；只有故意重放另一份源码树的 5 个脚本保留，并在测试里白名单。`tests/test_packaging.py` 禁止新增。
- CI 的 `install-smoke`（Linux/Windows × Python 3.11/3.13）验证干净克隆能安装并从仓库外导入。

## 备选方案
- 继续用 `sys.path` 补丁：脆弱且无法检查。
- 重构所有路径定位、做成可分发的包：工作量大，且与研究产物"位置即身份"的约定冲突，收益有限。

## 后果
新人需要先执行一次可编辑安装；CI 每个作业都要装。若将来要分发，需要先把路径定位集中到一个配置层，再取代本条。
