from __future__ import annotations

import ast
from pathlib import Path

import yaml

import main as backtest_entrypoint
from config.config import ConfigLoadError, ConfigLoader


ROOT = Path(__file__).resolve().parents[1]


def test_cli_contract_is_declared_outside_the_orchestrator():
    parser = backtest_entrypoint._build_parser()
    options = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    assert options == {
        "--help", "--days", "--start", "--end", "--capital", "--symbols",
        "--source", "--data-dir", "--seed", "--slippage", "--random_slip",
        "--disable-routing-log", "--exchange", "--market-type", "--timeframe",
        "--data-timezone", "--alignment-mode", "--benchmark-mode",
        "--benchmark-rebalance-cost-bps", "--universe-file",
        "--secondary-data-dir", "--require-secondary-audit", "--replay-manifest",
        "--report-profile",
    }
    parsed = parser.parse_args([
        "--source", "local", "--data-dir", "prices", "--start", "2025-01-01",
        "--end", "2025-02-01", "--symbols", "BTC-USDT", "ETH-USDT",
        "--report-profile", "full", "--disable-routing-log",
    ])
    assert parsed.source == "local"
    assert parsed.symbols == ["BTC-USDT", "ETH-USDT"]
    assert parsed.report_profile == "full"
    assert parsed.disable_routing_log is True


def test_workbook_and_pdf_depend_on_shared_metrics_not_each_other():
    workbook_source = (ROOT / "backtest/reporting/render/workbook.py").read_text("utf-8")
    imports = {
        node.module
        for node in ast.walk(ast.parse(workbook_source))
        if isinstance(node, ast.ImportFrom)
    }
    assert "backtest.reporting.risk_metrics" in imports
    assert "backtest.reporting.render.pdf" not in imports


def _legacy_payload() -> dict:
    payload = yaml.safe_load((ROOT / "config/params.yaml").read_text("utf-8"))
    payload["phase4"] = {
        "transition_action": payload["router"].pop("transition_action"),
        "max_holding_days": payload["router"].pop("max_holding_days"),
        "allocation_order": payload.pop("allocation")["order"],
        "stability_candidates": payload["state"].pop("stability_candidates"),
    }
    return payload


def test_legacy_phase4_config_is_migrated_without_runtime_drift(tmp_path):
    path = tmp_path / "params.yaml"
    path.write_text(yaml.safe_dump(_legacy_payload()), "utf-8")
    loaded = ConfigLoader(config_path=str(path))
    assert loaded.require("router", "transition_action") == "stop_new_entries"
    assert loaded.require("router", "max_holding_days") == 365
    assert loaded.require("allocation", "order") == "score_strategy_symbol"
    assert loaded.require("state", "stability_candidates") == [2, 3, 5, 10]


def test_conflicting_legacy_and_current_config_fails_closed(tmp_path):
    payload = yaml.safe_load((ROOT / "config/params.yaml").read_text("utf-8"))
    payload["phase4"] = {"max_holding_days": 180}
    path = tmp_path / "params.yaml"
    path.write_text(yaml.safe_dump(payload), "utf-8")
    try:
        ConfigLoader(config_path=str(path))
    except ConfigLoadError as exc:
        assert "conflicting legacy and current configuration" in str(exc)
    else:
        raise AssertionError("conflicting migration values must fail closed")
