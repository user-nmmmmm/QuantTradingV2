"""Regression evidence for roadmap FIX-02/03/08/19/20 contracts."""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from analysis.research_validation import deflated_sharpe_ratio, evaluate_holdout_admission
from backtest.reporting import ReportGenerator
from backtest.reporting.render.charts import rolling_max_drawdown
from backtest.reporting.serialization import write_metrics_json
from core.diagnostics import build_diagnostics, calculate_lifecycle_coverage
from core.metrics import Metrics, calculate_cost_sensitivity, calculate_signal_funnel
from scripts.main_acceptance import validate_clean_source
from scripts.run_phase5_analysis import main as phase5_main, phase5_config
from scripts.roadmap_baseline import source_manifest, verify_source
from core.metric_result import MetricResult


@pytest.mark.parametrize("pnls", [[20.] * 28 + [-1.], [20.] * 29, []])
def test_small_or_no_loss_pf_never_passes_admission(pnls):
    index = pd.date_range("2025-01-01", periods=61)
    equity = pd.Series(np.arange(100., 161.), index=index)
    result = evaluate_holdout_admission(trades=[{"net_pnl": pnl} for pnl in pnls],
                                       equity=equity, benchmark=equity * 0 + 100)
    assert not result["gates"]["G13_pf_significance"]
    assert result["decision"] == "reject"


def test_missing_pnl_cannot_inflate_valid_sample_count():
    trades = [{"net_pnl": 100., "gross_pnl_theoretical": 100.}] * 20
    trades += [{"net_pnl": -1., "gross_pnl_theoretical": -1.}] + [{}] * 9
    curve = pd.Series([100., 105., 110.], index=pd.date_range("2025-01-01", periods=3))
    result = evaluate_holdout_admission(trades=trades, equity=curve, benchmark=curve * 0 + 100)
    assert result["decision"] == "reject"
    assert not result["gates"]["G13_pf_significance"]
    assert result["profit_factor"]["sample_size"] == 21
    assert len(result["input_integrity"]["invalid_trades"]) == 9


def test_dsr_annualization_preserves_probability_and_scales_both_terms():
    values = [0.01, -.005, .02, -.01, .005] * 20
    period = deflated_sharpe_ratio(values, trials=4)
    annual = deflated_sharpe_ratio(values, trials=4, periods_per_year=365)
    assert annual["probability"] == pytest.approx(period["probability"])
    for name in ("observed_sharpe", "expected_max_sharpe"):
        assert annual[name] == pytest.approx(period[name] * np.sqrt(365))


def test_strict_json_preserves_unavailable_values_and_legacy_adapter(tmp_path):
    path = tmp_path / "metrics.json"
    write_metrics_json(path, {"ProfitFactor": float("inf"), "nested": {"nan": float("nan")}})
    result = json.loads(path.read_text(), parse_constant=lambda value: pytest.fail(value))
    assert result["metrics"]["ProfitFactor"] is None
    assert result["nonfinite_values"]["metrics.ProfitFactor"]["reason"]
    assert Metrics.calculate_profit_factor([1, -1])["sample_size"] == 2


def test_regular_report_writes_same_schema_for_zero_trades(tmp_path, monkeypatch):
    from backtest.reporting.render import workbook
    reporter = ReportGenerator(str(tmp_path))
    for method in ("_plot_equity", "_plot_monthly_heatmap", "_plot_rolling_metrics", "_plot_pnl_distribution"):
        monkeypatch.setattr(reporter, method, lambda *args, **kwargs: None)
    monkeypatch.setattr(workbook, "write_workbook_report", lambda *args, **kwargs: None)
    curve = pd.DataFrame({"equity": [100., 100., 100.]}, index=pd.date_range("2025-01-01", periods=3))
    reporter.generate([], curve, report_profile="workbook", max_holding_days=17)
    document = json.loads((tmp_path / "metrics.json").read_text())
    assert document["schema_version"] == "quanttrading.metrics/v1"
    assert document["metrics"]["TotalTrades"] == 0
    assert document["metrics"]["Diagnostics"]["holding_period_audit"]["configured_max_holding_days"] == 17


def test_old_peak_leaving_rolling_window_no_longer_causes_drawdown():
    result = rolling_max_drawdown(pd.Series([200., 100., 110., 120.]), 3)
    assert result.iloc[2] == pytest.approx(-.5)
    assert result.iloc[3] == 0


def test_histogram_wins_and_losses_use_identical_edges(tmp_path, monkeypatch):
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure
    bins = []
    original = Axes.hist
    def capture(self, *args, **kwargs):
        bins.append(np.array(kwargs["bins"]))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Axes, "hist", capture)
    monkeypatch.setattr(Figure, "savefig", lambda *args, **kwargs: None)
    ReportGenerator(str(tmp_path))._plot_pnl_distribution([{"net_pnl": n} for n in (-100, -1, 1, 10)])
    assert len(bins) == 2
    assert np.array_equal(bins[0], bins[1])
    assert bins[0][0] <= -100 and bins[0][-1] >= 10


def test_funnel_excludes_exit_and_unlinked_orders():
    def event(key, kind, **payload):
        return SimpleNamespace(correlation_id=key, event_type=kind, payload=payload)
    events = [event("entry", "risk_decision", approved=True), event("entry", "order_intent", side="buy"),
              event("entry", "order", status="accepted"), event("entry", "fill"),
              event("exit", "order_intent", side="sell"), event("exit", "fill"),
              event("orphan", "fill")]
    result = calculate_signal_funnel(events)
    assert result["total_correlation_chains"] == 1
    assert all(row["count"] == 1 for row in result["stages"].values())
    assert result["excluded_exit_chains"] == 1
    assert result["unclassified_chains"] == 1


def test_legacy_cost_sensitivity_does_not_charge_slippage_twice():
    result = calculate_cost_sensitivity([{"gross_pnl": 10, "gross_pnl_theoretical": None,
                                         "slippage": 2, "commission": 1}])
    assert result["baseline_net_pnl"] == 9
    assert result["legacy_count"] == 1
    grid = {(r["commission_multiplier"], r["slippage_multiplier"]): r["net_pnl"] for r in result["grid"]}
    assert grid[(1, 2)] == 7


def test_csv_missing_theoretical_price_is_legacy_and_bad_costs_are_invalid():
    trade = {"gross_pnl": 10, "gross_pnl_theoretical": float("nan"), "slippage": 2, "commission": 1}
    result = calculate_cost_sensitivity([trade])
    assert result["baseline_net_pnl"] == 9
    assert result["legacy_count"] == 1
    result = calculate_cost_sensitivity([{**trade, "commission": float("nan")}])
    assert result["status"] == "invalid_input" and result["grid"] == []


def test_exit_action_is_excluded_before_fill():
    events = [SimpleNamespace(correlation_id="exit", event_type="risk_decision",
                              payload={"action": "sell", "approved": True}),
              SimpleNamespace(correlation_id="exit", event_type="order_intent", payload={"action": "sell"})]
    result = calculate_signal_funnel(events)
    assert result["total_correlation_chains"] == 0
    assert result["excluded_exit_chains"] == 1


def test_holding_diagnostic_does_not_invent_default_policy():
    assert build_diagnostics([], pd.Series([100., 100.]))["holding_period_audit"]["status"] == "not_modeled"


def test_fill_slices_of_one_close_event_do_not_inflate_lifecycle_denominator():
    legs = [{"strategy": "S", "close_event_id": "LOT-1:1"}] * 2
    result = calculate_lifecycle_coverage(legs, {"S": 1})
    assert result["overall_coverage"] == 1
    assert result["sample_size"] == 1
    assert result["blind_strategies"] == []


def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "app.py").write_text("pass\n")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.parametrize("name", ["conftest.py", "config.yaml", "shadow.py", "evidence/conftest.py"])
def test_untracked_inputs_block_acceptance_before_tests(tmp_path, name):
    root = git_repo(tmp_path)
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("input")
    with pytest.raises(ValueError, match="Acceptance inputs"):
        validate_clean_source(root, root / "evidence")


def test_exact_output_evidence_can_be_untracked(tmp_path):
    root = git_repo(tmp_path)
    out = root / "evidence"
    out.mkdir()
    (out / "result.json").write_text("{}")
    assert validate_clean_source(root, out)["untracked_output_files"] == ["evidence/result.json"]


def test_tracked_edits_are_rejected(tmp_path):
    root = git_repo(tmp_path)
    (root / "app.py").write_text("different = True\n")
    with pytest.raises(ValueError, match="app.py"):
        validate_clean_source(root, root / "evidence")


def test_ignored_business_code_is_not_an_approved_input(tmp_path):
    root = git_repo(tmp_path)
    (root / ".git/info/exclude").write_text("core/local.py\n")
    (root / "core").mkdir()
    (root / "core/local.py").write_text("RISK_MULTIPLIER = 999\n")
    with pytest.raises(ValueError, match="core/local.py"):
        validate_clean_source(root, root / "evidence")


def test_phase5_old_entrypoint_refuses_admission_without_writes(tmp_path):
    with pytest.raises(SystemExit) as raised:
        phase5_main(["--output", str(tmp_path / "result")])
    assert raised.value.code == 2
    assert not (tmp_path / "result").exists()


def test_phase5_config_rejects_unused_fields(tmp_path):
    import yaml
    path = Path(__file__).resolve().parents[1] / "config/params.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["phase5"]["partition"]["unused"] = 1
    modified = tmp_path / "params.yaml"
    modified.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match="unknown or missing"):
        phase5_config(modified)


@pytest.mark.parametrize("name", ["conftest.py", "core/new_module.py", "config/new.yaml", "tests/fixtures/new.json"])
def test_frozen_worktree_rejects_added_inputs(tmp_path, name):
    expected = source_manifest(tmp_path)
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("new input")
    with pytest.raises(ValueError, match="identity changed"):
        verify_source(tmp_path, expected)


def test_metric_v2_pandas_and_numpy_missing_values(tmp_path):
    document = write_metrics_json(tmp_path / "m.json", {
        "n": np.int64(3), "missing": pd.NA, "time": pd.NaT,
        "MetricResults": [MetricResult("x", None, "insufficient", reason="no sample").to_dict()],
    })
    assert document["metrics"]["n"] == 3
    assert document["metrics"]["missing"] is None
    assert document["metrics"]["time"] is None
    assert document["metric_results"][0]["status"] == "insufficient_data"
    assert MetricResult("bad", None, "invalid_input", reason="bad price").value is None
    with pytest.raises(ValueError, match="unknown"):
        MetricResult("bad", None, "silently_successful")


def test_final_partition_changes_do_not_change_optimizer_ranking(tmp_path, monkeypatch):
    from analysis import optimize
    from core.metrics import train_test_split_returns
    # Same pre-registered train data; the final 30% reverses the apparent full-sample winner.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(optimize, "_load_data", lambda *args: {"TEST": "unused"})
    chosen = []
    for sign in (1, -1):
        evaluations = []
        for entry, mean in ((20, .02), (30, .01)):
            returns = pd.Series([mean] * 7 + [sign * (1 if entry == 30 else -1)] * 3)
            evaluations.append({"name": f"entry={entry},exit=5", "Entry_Window": entry,
                "Exit_Window": 5, "Total_Ret%": 1, "Max_DD%": 0, "Sharpe": sign * entry,
                "Trades": 1, "Win_Rate%": 1, "returns": returns})
            assert len(train_test_split_returns(returns, .7)["train"]) == 7
        monkeypatch.setattr(optimize, "_evaluate_grid", lambda *args: evaluations)
        optimize.run_grid_search(["TEST"], "scenario", 10, "2025-01-01", "2025-01-10")
        output = next((tmp_path / "reports").glob("optimization_*.csv"))
        chosen.append(pd.read_csv(output).iloc[0]["Entry_Window"])
    assert chosen == [20, 20]
