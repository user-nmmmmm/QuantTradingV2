"""Durable experiments and strategy snapshots do not mutate shared config."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from dashboard.backtest_jobs import BacktestJobs, JobConflict, PROJECT_ROOT
from dashboard.experiment_store import ExperimentStore
from dashboard.strategy_presets import strategy_catalog, validate_strategy
from tests.test_dashboard_jobs import ControlledProcess, await_terminal, parameters


def service(tmp_path, monkeypatch):
    result = BacktestJobs(tmp_path / "data", tmp_path / "reports", timeout=2)
    monkeypatch.setattr(result, "_required_symbols", lambda: 1)
    return result


def test_completed_jobs_and_bounded_logs_survive_service_restart(tmp_path, monkeypatch):
    first = service(tmp_path, monkeypatch)
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", lambda command, **kwargs: ControlledProcess(command))
    submitted = first.submit(parameters())
    before = await_terminal(first)
    first.close()
    second = service(tmp_path, monkeypatch)
    try:
        restored = second.list()[0]
        assert restored["id"] == submitted["id"]
        assert restored["status"] == "succeeded"
        assert restored["logs"] == before["logs"]
        assert restored["elapsed_seconds"] == before["elapsed_seconds"]
        assert second.history()["total"] == 1
    finally:
        second.close()


def test_orphaned_running_job_is_interrupted_without_auto_resume(tmp_path, monkeypatch):
    store = ExperimentStore(tmp_path / "reports" / ".dashboard" / "experiments.sqlite3")
    store.save_job({"id": "web_orphaned", "status": "running", "kind": "backtest",
                    "created_at": "2026-01-01T00:00:00+00:00", "logs": ["last known line"], "parameters": {}})
    created = []
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", lambda *a, **k: created.append(True))
    restarted = service(tmp_path, monkeypatch)
    try:
        job = restarted.list()[0]
        assert job["status"] == "interrupted"
        assert job["run_id"] is None
        assert job["finished_at"] is not None
        assert "not resumed" in job["error"]
        assert created == []
    finally:
        restarted.close()


def test_experiment_metadata_search_and_legacy_registration(tmp_path):
    store = ExperimentStore(tmp_path / "experiments.sqlite3")
    store.register_report("legacy_2025", {"source": "local"}, "2025-01-01T00:00:00+00:00")
    store.update_metadata("legacy_2025", {"name": "趋势实验", "tags": ["BTC", "baseline", "BTC"],
                                         "notes": "成本压力 50%", "favorite": True})
    assert store.search(query="趋势", favorite=True, tag="BTC")["total"] == 1
    assert store.search(query="%", status="succeeded")["total"] == 1
    assert store.search(tag="BT")["total"] == 0
    assert store.get("legacy_2025")["tags"] == ["BTC", "baseline"]
    store.register_report("legacy_2025", {}, "2026-01-01T00:00:00+00:00")
    assert store.get("legacy_2025")["name"] == "趋势实验"
    reopened = ExperimentStore(tmp_path / "experiments.sqlite3")
    assert reopened.search(favorite=True)["total"] == 1
    assert reopened.recent_jobs() == []
    with pytest.raises(ValueError):
        store.update_metadata("legacy_2025", {"status": "failed"})
    with pytest.raises(ValueError):
        store.update_metadata("legacy_2025", {"favorite": "true"})
    with pytest.raises(ValueError):
        store.update_metadata("legacy_2025", {"tags": ["x"] * 13})
    with pytest.raises(FileNotFoundError):
        store.update_metadata("missing", {"name": "name"})


@pytest.mark.parametrize("selection", [
    {"family": "arbitrary_code"}, {"family": "configured", "parameters": {"risk": 1}},
    {"family": "trend_breakout", "parameters": {"entry_window": True}},
    {"family": "trend_breakout", "parameters": {"entry_window": 10, "exit_window": 20}},
    {"family": "trend_breakout", "parameters": {"use_obv": 1}},
    {"family": "mean_reversion", "parameters": {"atr_threshold_pct": float("nan")}},
    {"family": "mean_reversion", "parameters": {"rsi_oversold": 80}},
    {"family": "mean_reversion", "parameters": {"use_rsi": "false"}},
    {"family": "mean_reversion", "parameters": {"config_path": "outside.yaml"}},
])
def test_strategy_whitelist_rejects_invalid_values(selection):
    with pytest.raises(ValueError):
        validate_strategy(selection)


def test_presets_round_trip_and_delete(tmp_path, monkeypatch):
    first = service(tmp_path, monkeypatch)
    preset = first.save_preset({"name": "研究模板", "description": "参数不会写回共享配置", "strategy": {
        "family": "trend_breakout", "parameters": {"entry_window": 55, "exit_window": 20}}})
    first.close()
    second = service(tmp_path, monkeypatch)
    try:
        assert second.list_presets()[0] == preset
        assert preset["strategy"]["parameters"]["use_obv"] is True
        second.save_preset({"id": preset["id"], "name": "更新", "strategy": {"family": "configured"}})
        assert second.list_presets()[0]["name"] == "更新"
        second.delete_preset(preset["id"])
        assert second.list_presets() == []
    finally:
        second.close()


def test_snapshot_and_command_preserve_shared_yaml(tmp_path, monkeypatch):
    shared = PROJECT_ROOT / "config" / "params.yaml"
    original = shared.read_bytes()
    current = service(tmp_path, monkeypatch)
    observed = []

    def create(command, **kwargs):
        observed.append(command)
        return ControlledProcess(command)

    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", create)
    try:
        preview = current.preview_strategy({"family": "trend_breakout", "parameters": {"entry_window": 55, "exit_window": 20}})
        current.submit(parameters(strategy={"family": "trend_breakout", "parameters": {"entry_window": 55, "exit_window": 20}}))
        job = await_terminal(current)
        command = observed[0]
        assert "dashboard.backtest_worker" in command
        snapshot = Path(command[command.index("--config") + 1])
        assert snapshot.parent == current.reports_dir / ".dashboard" / "configs"
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == job["config_sha256"]
        assert yaml.safe_load(snapshot.read_text(encoding="utf-8"))["research"]["trend_breakout_parameters"] == {"entry_window": 55, "exit_window": 20}
        assert (current.reports_dir / job["id"] / "config.snapshot.yaml").read_bytes() == snapshot.read_bytes()
        assert any(row["path"] == "research.trend_breakout_parameters" or row["path"].startswith("research.trend_breakout_parameters.") for row in preview["config_diff"])
        assert shared.read_bytes() == original
        assert current.store.get(job["id"])["config_sha256"] == job["config_sha256"]
    finally:
        current.close()


def test_research_adapter_shares_durable_lifecycle(tmp_path, monkeypatch):
    current = service(tmp_path, monkeypatch)
    observed = []

    def build(identifier, payload, directory, config_path):
        observed.append((identifier, payload, directory, config_path))
        return ["python", "research_entry.py", "--output-dir", str(directory)]

    def create(command, **kwargs):
        process = ControlledProcess(command, report=False)
        output = Path(command[-1])
        output.mkdir(parents=True)
        (output / "result.json").write_text('{"status":"complete"}')
        return process

    current.register_task_type("robust", build, "result.json")
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", create)
    try:
        current.submit_task("robust", {"method": "walk_forward"})
        job = await_terminal(current)
        assert job["kind"] == "robust" and job["status"] == "succeeded"
        assert job["result_file"] == "result.json"
        assert observed[0][3].is_file()
        assert current.history(kind="robust")["total"] == 1
    finally:
        current.close()


def test_local_job_preflight_rejects_missing_or_invalid_daily_bars(tmp_path, monkeypatch):
    current = service(tmp_path, monkeypatch)
    current.data_dir.mkdir()
    path = current.data_dir / "BTC_USDT.csv"
    path.write_text("timestamp,open,high,low,close,volume\n2025-01-01,10,12,9,11,100\n2025-02-01,10,12,9,11,100\n")
    try:
        with pytest.raises(ValueError, match="missing daily candles"):
            current.submit(parameters(source="local", end="2025-02-01"))
        assert current.list() == []
    finally:
        current.close()
