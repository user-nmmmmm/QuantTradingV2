"""Matrix verdicts consume sealed facts and never silently choose another run."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import evaluate_matrix as verdict
from scripts import run_backtest_matrix as matrix


def write_json(path, value):
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def seal_report(path, *, dd=-0.1, trades=0):
    path.mkdir(parents=True)
    (path / "data_inputs").mkdir()
    (path / "data_inputs" / "TEST.csv").write_text("timestamp,close\n2020-01-01,100\n")
    metrics = {
        "schema_version": "quanttrading.metrics/v1",
        "metrics": {"TradeInputIntegrity": {"status": "ok"}, "TotalReturn": 0,
                    "MaxDrawdownPct": dd, "TotalTrades": trades},
        "metric_results": [{"name": "ProfitFactor", "status": "insufficient_data", "value": None},
                           {"name": "OpportunityCost", "status": "not_modeled", "value": None}],
    }
    write_json(path / "metrics.json", metrics)
    write_json(path / "reconciliation.json", {"schema_version": "quanttrading.metrics/v1",
               "metrics": {"schema_version": "closed-trade-reconciliation/v1", "status": "pass"}})
    (path / "closed_trades.csv").write_text("trade_id,net_pnl\n")
    (path / "equity.csv").write_text("timestamp,equity\n2020-01-01,10000\n")
    manifest = {
        "schema_version": "2.0", "audit": {"coverage": {"status": "ok"}},
        "execution": {"result_digest": "f" * 64},
        "artifacts": {p.name: {"sha256": verdict.digest(p)} for p in path.iterdir() if p.is_file()},
        "data_snapshots": {"TEST": {"path": "TEST.csv", "sha256": verdict.digest(path / "data_inputs/TEST.csv")}},
    }
    write_json(path / "run_manifest.json", manifest)
    return path


def reseal(path):
    doc = json.loads((path / "run_manifest.json").read_text())
    for name in doc["artifacts"]:
        doc["artifacts"][name]["sha256"] = verdict.digest(path / name)
    write_json(path / "run_manifest.json", doc)


def test_no_trades_and_unmodeled_facts_are_valid_but_not_strategy_approval(tmp_path):
    result = verdict.evaluate_report(seal_report(tmp_path / "cell"))
    assert result["status"] == "pass"
    assert result["trades"] == 0
    assert result["live_admission"] is False
    assert "insufficient_trade_support_no_strategy_validity_claim" in result["warnings"]
    assert set(result["unavailable_metrics"]) == {"ProfitFactor", "OpportunityCost"}


@pytest.mark.parametrize("name", ["metrics.json", "equity.csv", "data_inputs/TEST.csv"])
def test_mutated_artifact_or_fixed_data_rejected(tmp_path, name):
    path = seal_report(tmp_path / "cell")
    with (path / name).open("a") as handle:
        handle.write(" ")
    with pytest.raises(ValueError, match="digest mismatch"):
        verdict.evaluate_report(path)


@pytest.mark.parametrize("metric", [
    {"name": "Bad", "status": "invalid_input", "value": None},
    {"name": "Bad", "status": "not_modeled", "value": 0},
    {"name": "Bad", "status": "ok", "value": None},
    {"name": "Bad", "status": "passed", "value": None},
    {"name": "Bad", "status": "ok", "value": True},
])
def test_invalid_metric_contract_rejected_even_if_artifact_is_sealed(tmp_path, metric):
    path = seal_report(tmp_path / "cell")
    doc = json.loads((path / "metrics.json").read_text())
    doc["metric_results"].append(metric)
    write_json(path / "metrics.json", doc)
    reseal(path)
    with pytest.raises(ValueError):
        verdict.evaluate_report(path)


@pytest.mark.parametrize("value", [0.1, -1.1, True, None])
def test_invalid_drawdown_units_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        verdict.evaluate_report(seal_report(tmp_path / "cell", dd=value))


def test_drawdown_fixed_threshold_is_blocking(tmp_path):
    result = verdict.evaluate_report(seal_report(tmp_path / "cell", dd=-.26))
    assert result["status"] == "fail"
    assert result["reason"] == "drawdown_threshold_exceeded"


def test_signed_drawdown_is_compared_as_positive_loss_budget(tmp_path):
    result = verdict.evaluate_report(seal_report(tmp_path / "cell", dd=-.20), maximum_drawdown=.25)
    assert result["status"] == "pass"
    assert result["max_drawdown"] == -.20
    assert result["max_drawdown_magnitude"] == .20


def test_top_level_pass_cannot_replace_standard_reconciliation(tmp_path):
    path = seal_report(tmp_path / "cell")
    write_json(path / "reconciliation.json", {"status": "pass"})
    reseal(path)
    with pytest.raises(ValueError, match="envelope"):
        verdict.evaluate_report(path)


def prepare_matrix(root):
    root.mkdir()
    cell = seal_report(root / "cell_0001")
    write_json(root / "config.json", {"symbols": ["TEST"], "timeframes": ["1d"],
                                      "windows": ["full"], "per_symbol": False})
    rows = [{"timeframe": "1d", "window": "full", "subject": "ALL", "exit_code": 0,
             "report_dir": str(cell)}]
    write_json(root / "results.json", rows)
    return rows


def test_matrix_uses_exact_coverage_and_keeps_research_separate(tmp_path):
    root = tmp_path / "matrix"
    prepare_matrix(root)
    result = verdict.evaluate_matrix(root)
    assert result["status"] == "pass"
    assert result["strategy_validity"] == "not_evaluated"
    assert result["live_admission"] is False


@pytest.mark.parametrize("kind", ["missing", "duplicate", "foreign"])
def test_matrix_missing_duplicate_or_unexpected_cell_rejected(tmp_path, kind):
    root = tmp_path / "matrix"
    rows = prepare_matrix(root)
    if kind == "missing":
        rows = []
    elif kind == "duplicate":
        rows.append(deepcopy(rows[0]))
    else:
        rows[0]["window"] = "bull2021"
    write_json(root / "results.json", rows)
    with pytest.raises(ValueError):
        verdict.evaluate_matrix(root)


@pytest.mark.parametrize("change", ["exit_code", "outside", "missing"])
def test_failed_or_unrelated_child_cannot_pass(tmp_path, change):
    root = tmp_path / "matrix"
    rows = prepare_matrix(root)
    if change == "exit_code":
        rows[0]["exit_code"] = 3
    elif change == "outside":
        rows[0]["report_dir"] = str(seal_report(tmp_path / "foreign"))
    else:
        rows[0]["report_dir"] = str(root / "absent")
    write_json(root / "results.json", rows)
    assert verdict.evaluate_matrix(root)["status"] == "fail"


def test_explicit_output_does_not_fall_back_to_concurrent_report(tmp_path, monkeypatch):
    monkeypatch.setattr(matrix.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr="", stdout=""))
    def forbidden(_):
        raise AssertionError("must not inspect other jobs' output")
    monkeypatch.setattr(matrix, "newest_report_dir", forbidden)
    result = matrix.run_one(["BTC/USDT"], "1d", "2020-01-01", "2020-02-01", 10000, 42, "",
                            report_root=tmp_path / "absent")
    assert result["error"]
    assert "report_dir" not in result


@pytest.mark.parametrize("args", [["--windows", "full", "full"], ["--start", "garbage"],
                                  ["--start", "2023-01-01", "--windows", "bull2021"]])
def test_invalid_matrix_dimensions_fail_before_creating_output(tmp_path, args):
    directory = tmp_path / "output"
    with pytest.raises(SystemExit) as exc:
        matrix.main(["--output-dir", str(directory), *args])
    assert exc.value.code == 2
    assert not directory.exists()


def test_main_output_collision_rejects_before_data_loading(tmp_path, monkeypatch):
    import main
    def forbidden(*a, **k):
        raise AssertionError("existing evidence must not trigger a new backtest")
    monkeypatch.setattr(main, "_load_requested_data", forbidden)
    assert main.main(["--output-dir", str(tmp_path), "--start", "2020-01-01", "--end", "2020-02-01"]) == 2
