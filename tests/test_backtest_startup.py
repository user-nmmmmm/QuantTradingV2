"""Public backtest startup under the unchanged production health policy."""
import json

import pandas as pd
import pytest

from backtest.cli import DEFAULT_BACKTEST_SYMBOLS, build_backtest_parser
from backtest.engine import BacktestEngine
from config.config import config
from scripts import run_automation as automation


def test_default_backtest_symbols_meet_registered_health_universe():
    args = build_backtest_parser(10000).parse_args(["--source", "synthetic"])
    assert args.symbols == ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    assert args.symbols == list(DEFAULT_BACKTEST_SYMBOLS)
    assert config.get("strategy_health")["probation_min_distinct_symbols"] == 3


def test_explicit_two_symbols_are_preserved_and_formal_health_rejects_startup():
    args = build_backtest_parser(10000).parse_args(["--symbols", "BTC-USDT", "ETH-USDT"])
    assert args.symbols == ["BTC-USDT", "ETH-USDT"]
    assert config.get("strategy_health")["probation_min_distinct_symbols"] == 3
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 10000.},
                         index=pd.date_range("2020-01-01", periods=60, freq="D"))
    with pytest.raises(ValueError, match="Unreachable health recovery.*requires 3.*contains 2"):
        BacktestEngine().run({symbol: frame.copy() for symbol in args.symbols}, routing_log_enabled=False)


def test_real_weekly_smoke_creates_full_report_and_replays_manifest(tmp_path):
    """Run the actual subprocess boundary, not a mocked Runner or evaluator."""
    settings = automation.read_json(automation.ROOT / "config" / "automation.json")
    folder = tmp_path / "real_weekly_smoke"
    folder.mkdir()
    record = {"steps": []}
    runner = automation.Runner(folder, record, timeout_seconds=180)
    verdict = automation.execute_task("weekly-smoke", runner, settings)
    assert verdict["status"] == "pass", verdict
    assert [(step["name"], step["status"], step["exit_code"]) for step in record["steps"]] == [
        ("smoke", "passed", 0), ("replay", "passed", 0)]
    report = folder / "report"
    manifest = json.loads((report / "run_manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["data_snapshots"]) == {"BTC-USDT", "ETH-USDT", "SOL-USDT"}
    smoke_args = record["steps"][0]["command"]
    assert smoke_args[smoke_args.index("--report-profile") + 1] == "full"
    for name in ("metrics.json", "reconciliation.json", "equity.csv", "closed_trades.csv"):
        assert (report / name).is_file(), name
    replay_log = (folder / record["steps"][1]["log"]).read_text(encoding="utf-8")
    assert '"status": "passed"' in replay_log
    assert config.get("strategy_health")["probation_min_distinct_symbols"] == 3
