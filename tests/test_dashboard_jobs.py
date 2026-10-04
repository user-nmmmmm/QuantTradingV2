"""Offline job admission, process cleanup and local HTTP write boundaries."""

from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from dashboard.backtest_jobs import BacktestJobs, JobConflict
from dashboard.web import DashboardHTTPServer, DashboardSource


def parameters(**overrides):
    return {"source": "synthetic", "symbols": ["BTC/USDT"], "start": "2025-01-01",
            "end": "2025-07-01", "capital": 10000, "slippage_bps": 5, "seed": 42,
            **overrides}


class ControlledProcess:
    def __init__(self, command, *, completed=True, result=0, report=True):
        self.command = command
        self.result = result
        self.report = report
        self.killed = False
        self.returncode = None
        self.finished = threading.Event()
        if completed:
            self.finished.set()
        self.stdout = io.StringIO("line\n" * 250 + "x" * 5000)

    def wait(self, timeout=None):
        if not self.finished.wait(timeout):
            raise subprocess.TimeoutExpired(self.command, timeout)
        self.returncode = -9 if self.killed else self.result
        if self.returncode == 0 and self.report:
            output = Path(self.command[self.command.index("--output-dir") + 1])
            output.mkdir(parents=True, exist_ok=True)
            (output / "equity.csv").write_text("timestamp,equity\n2025-01-01,10000\n2025-07-01,10100\n", encoding="utf-8")
        return self.returncode

    def kill(self):
        self.killed = True
        self.finished.set()

    def poll(self):
        return self.returncode


def await_terminal(manager):
    worker = manager._worker_thread
    assert worker is not None
    worker.join(timeout=5)
    assert not worker.is_alive()
    return manager.list()[0]


@pytest.fixture
def manager(tmp_path, monkeypatch):
    service = BacktestJobs(tmp_path / "data", tmp_path / "reports", timeout=2)
    monkeypatch.setattr(service, "_required_symbols", lambda: 1)
    yield service
    service.close()


@pytest.mark.parametrize("changes", [
    {"source": "ccxt"}, {"source": "yahoo"}, {"symbols": ["../BTC/USDT"]},
    {"symbols": ["BTC/USDT"] * 5}, {"symbols": []}, {"symbols": ["BTC/USDT"] * 2},
    {"capital": float("nan")}, {"capital": float("inf")}, {"capital": 10**400},
    {"capital": True}, {"capital": 0}, {"slippage_bps": -1}, {"slippage_bps": 101},
    {"seed": True}, {"seed": 2.5}, {"seed": -1}, {"seed": 2**32},
    {"start": "20250101"}, {"end": "2025-01-10"}, {"end": "2030-01-01"},
    {"output_dir": "somewhere"}, {"command": "python"},
])
def test_rejects_unbounded_or_untrusted_parameters(manager, changes):
    with pytest.raises(ValueError):
        manager.submit(parameters(**changes))
    assert manager.list() == []


def test_success_uses_fixed_cli_and_exposes_only_completed_report(manager, monkeypatch):
    observed = {}

    def create(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return ControlledProcess(command)

    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", create)
    submitted = manager.submit(parameters())
    result = await_terminal(manager)
    assert result["status"] == "succeeded"
    assert result["run_id"] == submitted["id"]
    assert len(result["logs"]) == 160
    assert max(map(len, result["logs"])) <= 2048
    command = observed["command"]
    assert command[command.index("--report-profile") + 1] == "compact"
    assert command[command.index("--slippage") + 1] == "0.0005"
    assert "--market-type" not in command
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["stdin"] == subprocess.DEVNULL
    metadata = json.loads((manager.reports_dir / result["id"] / "dashboard_job.json").read_text())
    assert metadata["status"] == "succeeded"


@pytest.mark.parametrize("exit_code,report,error", [(3, True, "exited with code 3"), (0, False, "without an equity report")])
def test_nonzero_or_missing_artifact_never_succeeds(manager, monkeypatch, exit_code, report, error):
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen",
                        lambda command, **kwargs: ControlledProcess(command, result=exit_code, report=report))
    manager.submit(parameters())
    result = await_terminal(manager)
    assert result["status"] == "failed"
    assert result["run_id"] is None
    assert error in result["error"]


def test_startup_failure_releases_active_slot(manager, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("process unavailable")
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", fail)
    manager.submit(parameters())
    assert await_terminal(manager)["status"] == "failed"
    assert manager._active is None
    manager.submit(parameters())
    assert await_terminal(manager)["status"] == "failed"


def test_admission_cancel_and_shutdown_reap_process(manager, monkeypatch):
    processes = []
    started = threading.Event()

    def create(command, **kwargs):
        process = ControlledProcess(command, completed=False)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", create)
    first = manager.submit(parameters())
    assert started.wait(2)
    with pytest.raises(JobConflict):
        manager.submit(parameters())
    manager.cancel(first["id"])
    assert await_terminal(manager)["status"] == "cancelled"
    assert processes[0].killed and processes[0].returncode == -9
    started.clear()
    manager.submit(parameters())
    assert started.wait(2)
    manager.close()
    assert manager.list()[0]["status"] == "cancelled"
    assert processes[-1].killed and processes[-1].returncode == -9
    with pytest.raises(JobConflict):
        manager.submit(parameters())


def test_timeout_reaps_process(manager, monkeypatch):
    manager.timeout = 0.01
    processes = []

    def create(command, **kwargs):
        process = ControlledProcess(command, completed=False)
        processes.append(process)
        return process

    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen", create)
    manager.submit(parameters())
    result = await_terminal(manager)
    assert result["status"] == "timed_out"
    assert processes[0].killed and processes[0].returncode == -9


def test_history_is_bounded(manager, monkeypatch):
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen",
                        lambda command, **kwargs: ControlledProcess(command, result=1))
    for _ in range(33):
        manager.submit(parameters())
        await_terminal(manager)
    assert len(manager.list()) == 30


def test_local_defaults_and_coverage_validation(manager):
    manager.data_dir.mkdir()
    (manager.data_dir / "BTC_USDT.csv").write_text(
        "timestamp,open,high,low,close,volume\n2025-01-01,1,1,1,1,1\n2025-07-01,1,1,1,1,1\n", encoding="utf-8")
    options = manager.options()
    assert options["defaults"]["source"] == "local"
    assert options["defaults"]["end"] == "2025-07-01"
    assert options["cache_range"]["start"] == "2025-01-01"
    with pytest.raises(ValueError, match="does not cover"):
        manager.submit(parameters(source="local", end="2025-08-01"))
    with pytest.raises(ValueError, match="Unknown or invalid symbol"):
        manager.submit(parameters(source="local", symbols=["ETH/USDT"]))


def test_health_policy_minimum_is_enforced_without_lowering_it(manager, monkeypatch):
    monkeypatch.setattr(manager, "_required_symbols", lambda: 3)
    options = manager.options()
    assert options["required_symbols"] == 3
    assert len(options["defaults"]["symbols"]) == 3
    assert options["synthetic_symbols"] == ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT"]
    with pytest.raises(ValueError, match="at least 3"):
        manager.submit(parameters())


def test_policy_projection_honors_routing_and_research_override(tmp_path, monkeypatch):
    configuration = tmp_path / "config"
    configuration.mkdir()
    (configuration / "params.yaml").write_text(
        "routing: {TREND_UP: TrendBreakout, SIDEWAYS: Cash}\n"
        "strategy_health: {enabled: true, probation_min_distinct_symbols: 3}\n"
        "research:\n  strategy_health_overrides:\n    TrendBreakout: {probation_min_distinct_symbols: 4}\n")
    monkeypatch.setattr("dashboard.backtest_jobs.PROJECT_ROOT", tmp_path)
    service = BacktestJobs(tmp_path / "data", tmp_path / "reports")
    try:
        assert service.options()["required_symbols"] == 4
        assert len(service.options()["defaults"]["symbols"]) == 4
        with pytest.raises(ValueError, match="at least 4"):
            service.submit(parameters(symbols=["BTC/USDT", "ETH/USDT", "BNB/USDT"]))
    finally:
        service.close()


@pytest.fixture
def http_server(tmp_path, monkeypatch):
    source = DashboardSource(tmp_path / "status.json", tmp_path / "alerts.jsonl",
                             data_dir=tmp_path / "data", reports_dir=tmp_path / "reports")
    server = DashboardHTTPServer(("127.0.0.1", 0), source)
    monkeypatch.setattr(server.jobs, "_required_symbols", lambda: 1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


def post(base, payload, token="", **headers):
    return urlopen(Request(base + "/api/backtest-jobs", data=json.dumps(payload).encode(),
                           headers={"Content-Type": "application/json", "X-CSRF-Token": token, **headers}))


def test_http_token_origin_host_and_json_contract(http_server, monkeypatch):
    server, base = http_server
    with urlopen(base + "/api/backtest-options") as response:
        options = json.load(response)
    assert options["enabled"] is True
    assert options["defaults"]["source"] == "synthetic"
    for headers, token in [({}, ""), ({"Origin": "https://example.com"}, options["csrf_token"]),
                           ({"Host": "evil.example"}, options["csrf_token"]),
                           ({"Sec-Fetch-Site": "cross-site"}, options["csrf_token"])]:
        with pytest.raises(HTTPError) as error:
            post(base, parameters(), token, **headers)
        assert error.value.code == 403
        assert "error" in json.load(error.value)
    captured = []
    monkeypatch.setattr(server.jobs, "submit", lambda payload: captured.append(payload) or {"id": "job", "status": "queued"})
    with post(base, parameters(), options["csrf_token"], Origin=base) as response:
        assert response.status == 202
        assert json.load(response)["job"]["id"] == "job"
    assert captured == [parameters()]
    with urlopen(base + "/api/backtest-jobs") as response:
        assert json.load(response) == {"jobs": []}
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + "/api/status", data=b"{}", method="POST"))
    assert error.value.code == 405


def test_http_disabled_invalid_request_and_cancel(http_server):
    server, base = http_server
    token = server.jobs.csrf_token
    with pytest.raises(HTTPError) as error:
        post(base, parameters(capital=float("nan")), token)
    assert error.value.code == 400
    server.jobs.enabled = False
    with pytest.raises(HTTPError) as error:
        post(base, parameters(), token)
    assert error.value.code == 409
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + "/api/backtest-jobs/cancel", data=b'{"id":"missing"}',
                        headers={"Content-Type": "application/json", "X-CSRF-Token": token}))
    assert error.value.code == 404


def test_assets_revalidate_while_api_is_never_cached(http_server):
    _, base = http_server
    with urlopen(base + "/assets/app.js") as response:
        etag = response.headers["ETag"]
        assert response.headers["Cache-Control"] == "no-cache"
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + "/assets/app.js", headers={"If-None-Match": etag}))
    assert error.value.code == 304
    assert error.value.read() == b""
    with urlopen(base + "/api/status") as response:
        assert response.headers["Cache-Control"] == "no-store"
    with pytest.raises(HTTPError) as error:
        urlopen(Request(base + "/api/backtest-options", headers={"Host": "evil.example"}))
    assert error.value.code == 403


def test_trade_pagination_http_endpoint(http_server):
    server, base = http_server
    run = server.source.reports_dir / "example"
    run.mkdir(parents=True)
    (run / "equity.csv").write_text("timestamp,equity\n2025-01-01,100\n2025-01-02,110\n")
    (run / "trades.csv").write_text("symbol,side\nBTC/USDT,buy\nBTC/USDT,sell\n")
    with urlopen(base + "/api/backtest-trades?id=example&page=2&page_size=1") as response:
        data = json.load(response)
    assert data["total"] == 2 and len(data["rows"]) == 1
    assert data["rows"][0]["side"] == "sell"
    with pytest.raises(HTTPError) as error:
        urlopen(base + "/api/backtest-trades?id=example&page_size=101")
    assert error.value.code == 400
