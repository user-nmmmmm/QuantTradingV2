# 依赖与 Python 环境

完整质量/覆盖率及 ML 作业使用 Python 3.11；安装、导入、入口命令和指定运行回归覆盖 Ubuntu/Windows × Python 3.11/3.13。根目录 `.python-version` 指定本地 3.13.2，`pyproject.toml` 的 Python 下限和 mypy 目标为 3.11。矩阵不是每个组合的完整套件结果，记录测试时应写明实际版本和任务范围。前端检查另需 Node.js 22，CI 与开发容器已安装。

| 文件 | 当前作用 |
| --- | --- |
| `requirements.txt` | 精确固定直接运行依赖。 |
| `requirements.lock.txt` | 固定运行环境的直接及传递依赖版本；用于需要严格复现的安装。 |
| `requirements-ml.txt` | 可选 CPU 机器学习研究依赖，与 `pyproject.toml` 的 `ml` extra 一致；不表示研究获准交易。 |
| `requirements-dev.txt` | 引用 `requirements.txt`，再固定测试、覆盖率、静态检查工具；CI 与它一起安装运行锁（见下）。 |
| `requirements.lock.sha256` | 检查运行锁文件是否被无声改写。 |
| `scripts/regenerate_lock.py` | 锁文件的**唯一**生成方式（不要手工编辑）；`--check` 离线核对规范形式、摘要和直接依赖。 |

安装与核验：

```powershell
# 可复现的运行环境 + 开发工具；CI、dev.py setup 和 dev container 走同一条命令（ADR 0021）
python -m pip install -r requirements.lock.txt -r requirements-dev.txt
python -m pip install -e . --no-deps   # 注册项目本身；依赖仍由上面的 requirements 文件固定
python scripts/check_environment.py --strict-lock   # 环境逐项等于运行锁
python scripts/verify_lock.py
python scripts/regenerate_lock.py --check
```

`pyproject.toml` 的 `[project]` 通过 `dynamic` 读取 `requirements.txt` 作为运行依赖，`ml` 额外依赖与
`requirements-ml.txt` 一致；版本固定仍只在这些文件和锁文件中维护，修改时需同步两处。

`verify_lock.py` 核验精确版本和已提交的 SHA-256；`regenerate_lock.py --check` 核验锁文件是规范形式、并覆盖 `requirements.txt` 的每个直接依赖且版本一致；`check_environment.py --strict-lock` 将当前环境与运行锁逐项比较。CI 把运行锁和开发依赖放在同一次 `pip install` 里解析，再运行这三项，所以环境漂移会让 CI 失败。

## 升级依赖

升级依赖永远是单独的 PR：

1. 修改 `requirements.txt`（及对应的 `requirements-dev.txt` / `requirements-ml.txt`）里的版本。
2. 在 Linux、Python 3.11（与 CI 相同）上运行 `python scripts/regenerate_lock.py`（需要联网）；它会重写锁文件和摘要。不要手工编辑锁文件。
3. 阅读锁文件的差异：传递依赖的变化也在其中。在目标 Python 版本上验证安装、测试与报告生成。
4. 提交时写入 `CHANGELOG.md`（如有用户可见影响）。锁文件在冻结范围内，PR 的身份影响报告会显示这一变化。

没有在 Linux 上再生成时，Windows 独有的依赖（例如 `colorama`）可能出现或消失，这只影响该平台，pip 仍会把它作为依赖安装。

## 运行依赖边界

行情客户端、签名和图像输出使用的 `aiohttp`、`cryptography`、`Pillow`、`soupsieve` 和 `urllib3`
在直接依赖中明确固定，普通可编辑安装与运行锁使用同一组已测试版本。`Pillow` 的导入名为 `PIL`，
环境检查同时验证分发版本和正确的模块名。

项目的命令行仪表盘使用 `dashboard.__main__`，Web 仪表盘使用 `dashboard.web` 的标准库 HTTP 服务；
报告使用 Matplotlib 的 `Agg` 后端。未被这些入口使用的 Streamlit 依赖已移除，运行锁不再为它保留
GitPython、Tornado 或 PyArrow。未来接入需要这些库的新功能时，须重新声明依赖并验证对应入口。
在旧环境执行安装不会自动卸载不再需要的包；需要精确核对新依赖闭包时，应建立新的虚拟环境，旧环境和历史证据继续保留。
报表回归使用的 `jsonschema` 在开发依赖中显式声明，运行时安装无需为测试加载这条依赖链。

本次版本范围覆盖 Linux/Windows 的 Python 3.11/3.13 矩阵。Cryptography 50 的上游二进制包不再覆盖
32 位 Windows 或 Intel macOS；这些平台的源构建与 OpenSSL 维护须单独验证，不能由本仓库矩阵推断。

## Dependabot

只对 GitHub Actions 启用：每月一次，小版本与补丁合并成一个分组 PR，主版本升级单独成 PR 并单独评审。pip 的版本更新不启用，因为 Dependabot 不能运行再生成脚本，每个 PR 都会让 `regenerate_lock.py --check` 失败；原因与取舍见 [ADR 0021](adr/0021-locked-ci-and-dependabot-policy.md)。仓库的安全警报保持开启，收到后按上面的"升级依赖"流程处理。
