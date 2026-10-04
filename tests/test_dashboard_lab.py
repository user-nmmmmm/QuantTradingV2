"""Research HTTP integration and calendar-aligned experiment comparison."""
import json
import threading
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from dashboard.experiment_compare import compare_experiments
from dashboard.web import DashboardHTTPServer, DashboardSource


def report(root, name, values, start=date(2025, 1, 1)):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "equity.csv").write_text("timestamp,equity\n" + "".join(
        f"{start + timedelta(days=index)},{value}\n" for index, value in enumerate(values)), encoding="utf-8")
    return folder


def test_comparison_normalizes_capital_preserves_calendar_and_worst_drawdown(tmp_path):
    report(tmp_path, "first", [100, 110, 88, 120])
    report(tmp_path, "second", [1000, 1100, 1200], date(2025, 1, 2))
    result = compare_experiments(tmp_path, ["first", "second"])
    assert result["runs"][0]["points"][2]["drawdown"] == pytest.approx(-.2)
    assert [run["points"][0]["nav"] for run in result["runs"]] == [100, 100]
    assert result["runs"][1]["points"][0]["timestamp"] == "2025-01-02"
    assert any("different observed periods" in message for message in result["warnings"])


def test_comparison_sampling_does_not_change_metrics_or_omit_trough(tmp_path):
    values = [100] * 1800
    values[899] = 30
    report(tmp_path, "one", values)
    report(tmp_path, "two", values)
    result = compare_experiments(tmp_path, ["one", "two"])
    run = result["runs"][0]
    assert len(run["points"]) <= 801
    assert run["point_count"] == 1800
    assert min(point["nav"] for point in run["points"]) == 30
    assert run["metrics"]["max_drawdown"] == pytest.approx(-.7)


def test_comparison_exposes_changed_base_configuration_even_with_equal_inputs(tmp_path):
    for name, digest in (("one", "a" * 64), ("two", "b" * 64)):
        folder = report(tmp_path, name, [100, 110])
        (folder / "dashboard_job.json").write_text(json.dumps({
            "status": "succeeded", "parameters": {"source": "synthetic"},
            "base_config_sha256": digest, "config_sha256": digest,
        }), encoding="utf-8")
    result = compare_experiments(tmp_path, ["one", "two"])
    assert any(row["field"] == "base_config_sha256" for row in result["differences"])
    assert any("different configuration snapshots" in warning for warning in result["warnings"])


@pytest.mark.parametrize("ids", [["one"], ["one"] * 2, ["1", "2", "3", "4", "5"], ["../outside", "two"]])
def test_comparison_rejects_invalid_selection(tmp_path, ids):
    with pytest.raises(ValueError):
        compare_experiments(tmp_path, ids)


@pytest.fixture
def server(tmp_path):
    source = DashboardSource(tmp_path / "status.json", tmp_path / "alerts.jsonl",
                             data_dir=tmp_path / "data", reports_dir=tmp_path / "reports")
    source.data_dir.mkdir()
    service = DashboardHTTPServer(("127.0.0.1", 0), source, backtests_enabled=False)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    yield service, f"http://127.0.0.1:{service.server_port}"
    service.shutdown()
    thread.join(timeout=3)
    service.server_close()


def get(base, path):
    with urlopen(base + path, timeout=10) as response:
        return json.load(response)


def post(base, path, value, token="", origin=None):
    headers = {"Content-Type": "application/json", "X-CSRF-Token": token}
    if origin:
        headers["Origin"] = origin
    with urlopen(Request(base + path, data=json.dumps(value).encode(), headers=headers), timeout=10) as response:
        return json.load(response)


def test_history_import_annotations_and_filter_http(server):
    service, base = server
    report(service.source.reports_dir, "legacy", [100, 110])
    data = get(base, "/api/experiments")
    assert data["total"] == 1
    token = get(base, "/api/backtest-options")["csrf_token"]
    post(base, "/api/experiments/metadata", {"id": "legacy", "name": "Reviewed", "tags": ["baseline"], "notes": "checked", "favorite": True}, token)
    filtered = get(base, "/api/experiments?q=Reviewed&tag=baseline&favorite=true")
    assert filtered["total"] == 1 and filtered["items"][0]["notes"] == "checked"
    assert get(base, "/api/experiments?favorite=false")["total"] == 0
    assert get(base, "/api/experiment?id=legacy")["experiment"]["favorite"] is True


def test_preset_validation_and_write_boundary_http(server):
    _, base = server
    value = {"name": "Trend", "strategy": {"family": "trend_breakout", "parameters": {"entry_window": 30, "exit_window": 10}}}
    with pytest.raises(HTTPError) as error:
        post(base, "/api/strategy-presets", value)
    assert error.value.code == 403
    token = get(base, "/api/backtest-options")["csrf_token"]
    with pytest.raises(HTTPError) as error:
        post(base, "/api/strategy-presets", value, token, "https://external.example")
    assert error.value.code == 403
    saved = post(base, "/api/strategy-presets", value, token)["preset"]
    assert get(base, "/api/strategy-presets")["presets"][0]["id"] == saved["id"]
    preview = post(base, "/api/strategy-preview", {"strategy": saved["strategy"]}, token)
    assert preview["config_diff"]
    value["strategy"]["parameters"]["exit_window"] = 40
    with pytest.raises(HTTPError) as error:
        post(base, "/api/strategy-presets", value, token)
    assert error.value.code == 400
    post(base, "/api/strategy-presets/delete", {"id": saved["id"]}, token)
    assert get(base, "/api/strategy-presets")["presets"] == []


def test_diagnostics_missing_evidence_remains_unknown_http(server):
    service, base = server
    report(service.source.reports_dir, "missing_metrics", [100, 100])
    result = get(base, "/api/backtest-diagnostics?id=missing_metrics")
    assert result["no_trade"]["status"] == "unknown"
    assert all(stage["count"] is None for stage in result["funnel"]["stages"])


def test_data_quality_and_indicator_warmup_http(server):
    service, base = server
    (service.source.data_dir / "BTC_USDT.csv").write_text(
        "timestamp,open,high,low,close,volume\n2025-01-01,10,11,9,10,100\n2025-01-03,10,12,9,11,120\n")
    quality = get(base, "/api/data-quality?symbols=BTC%2FUSDT&start=2025-01-01&end=2025-01-03")
    assert quality["selection"]["valid"] is False
    assert quality["symbols"][0]["missing_days"] == 1
    analysis = get(base, "/api/market-analysis?symbol=BTC%2FUSDT&limit=60")
    assert analysis["latest"]["sma20"] is None
    with pytest.raises(HTTPError) as error:
        get(base, "/api/market-analysis?symbol=..%2Fsecret")
    assert error.value.code == 400
