"""在独立子进程中装载任务配置快照，再调用现有回测 CLI。

配置单例与策略工厂的适配只影响当前子进程，不应在网页服务进程内调用。
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def initialize_configuration(config_path: Path, expected_sha256: str | None = None) -> None:
    """在导入回测入口前装载配置；传入摘要时先核验快照内容。

    保留 config 单例的对象身份，让已导入该对象的模块读取同一份任务配置。
    均值回归的构造参数仅在本进程注入，后续止损与评分政策仍由装配工厂设置。
    """
    if expected_sha256 and hashlib.sha256(config_path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("Job configuration snapshot hash mismatch")
    from config.config import ConfigLoader, config

    isolated = ConfigLoader(str(config_path))
    config._config = isolated._config
    config.config_path = str(config_path)
    selection = (config.get("research") or {}).get("dashboard_strategy")
    if selection:
        from dashboard.strategy_presets import validate_strategy
        selection = validate_strategy(selection)
        if selection["family"] == "mean_reversion":
            from composition import factory
            constructor = factory.RangeStrategy
            parameters = dict(selection["parameters"])
            factory.RangeStrategy = lambda: constructor(**parameters)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Isolated dashboard backtest worker")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--config-sha256")
    args, remainder = parser.parse_known_args(argv)
    initialize_configuration(args.config, args.config_sha256)
    from main import main as run_backtest
    return run_backtest(remainder)


if __name__ == "__main__":
    raise SystemExit(main())
