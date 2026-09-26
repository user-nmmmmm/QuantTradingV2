# 依赖与 Python 环境

现有 CI 工作流使用 Python 3.11；根目录 `.python-version` 指定 3.13.2。这两个入口尚未统一，因此修改依赖或解释测试结果时应记录实际 Python 版本，不把单一环境的通过结果外推到另一版本。

| 文件 | 当前作用 |
| --- | --- |
| `requirements.txt` | 精确固定直接运行依赖。 |
| `requirements.lock.txt` | 固定运行环境的直接及传递依赖版本；用于需要严格复现的安装。 |
| `requirements-dev.txt` | 引用 `requirements.txt`，再固定测试、覆盖率、静态检查工具；现有 CI 从此文件安装，并未直接从运行锁文件安装。 |
| `requirements.lock.sha256` | 检查运行锁文件是否被无声改写。 |

安装与核验：

```powershell
# 可复现的运行环境
python -m pip install -r requirements.lock.txt
python scripts/check_environment.py --strict-lock

# 开发/CI 工具；当前 CI 走这一入口
python -m pip install -r requirements-dev.txt
python scripts/verify_lock.py
```

`verify_lock.py` 核验精确版本和已提交的 SHA-256；`check_environment.py --strict-lock` 将当前环境与运行锁比较。开发工具的安装路径可能解析出与运行锁不同的传递版本，所以不能仅凭 `requirements-dev.txt` 安装成功宣称环境已按运行锁复现。更新运行依赖时，应在同一变更中更新锁文件和校验值，并在目标 Python 版本上验证安装、测试与报告生成。需要平台标记时将其直接记录在锁文件中。
