"""The ordinary backtest selector switch is explicit, durable and isolated."""
import io
import json
from pathlib import Path

import pytest

from dashboard.backtest_jobs import BacktestJobs
from dashboard.visual_data import list_backtests, load_backtest
from core.reproducibility import sha256_file


def parameters(**overrides):
    return {"source": "synthetic", "symbols": ["BTC/USDT"], "start": "2025-01-01",
            "end": "2025-07-01", "capital": 10000, "slippage_bps": 5, "seed": 42,
            **overrides}


def make_bundle(directory, version="initial"):
    """Small real package inventory; these tests do not run model inference."""
    directory.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("model", "policy"):
        model_id = f"{name}-{version}"
        path = directory / f"{name}.json"
        path.write_text(json.dumps({"model_id": model_id, "weights": [version]}), encoding="utf-8")
        files[name] = {"path": path.name, "sha256": sha256_file(path), "model_id": model_id}
    manifest = directory / "current.json"
    manifest.write_text(json.dumps({"schema": "frozen-coin-selector/v1", "timeframe": "1d",
        "candidate": {"selected_candidate": "rl", "model_id": files["policy"]["model_id"],
                      "parent_model_id": files["model"]["model_id"]}, "files": files}), encoding="utf-8")
    return manifest


@pytest.fixture
def dashboard_project(tmp_path, monkeypatch):
    """Every project lookup stays in a checkout without reports or market data."""
    root = tmp_path / "registered_project"
    configuration = root / "config" / "params.yaml"
    configuration.parent.mkdir(parents=True)
    configuration.write_text("account: {mode: spot}\nrouting: {TREND_UP: Cash}\n", encoding="utf-8")
    monkeypatch.setattr("dashboard.backtest_jobs.PROJECT_ROOT", root)
    return root


@pytest.fixture
def manager(tmp_path, monkeypatch, dashboard_project):
    import backtest.coin_selector as selector
    monkeypatch.setattr(selector, "DEFAULT_BUNDLE", make_bundle(tmp_path / "current_selector"))
    service = BacktestJobs(tmp_path / "data", tmp_path / "reports")
    monkeypatch.setattr(service, "_required_symbols", lambda: 1)
    yield service
    service.close()


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", "on", [], {}])
def test_selector_requires_a_json_boolean(manager, value):
    with pytest.raises(ValueError, match="use_selector must be a boolean"):
        manager.submit(parameters(use_selector=value))
    assert manager.list() == []


def test_default_is_off_and_cli_switch_changes_only_selector_argument(manager, tmp_path):
    assert manager.options()["defaults"]["use_selector"] is False
    manager._jobs["cli_job"] = {"kind": "backtest", "_config_path": tmp_path / "frozen.yaml",
                                "config_sha256": "a" * 64,
                                "_selector_bundle_path": tmp_path / "selector" / "manifest.json"}
    original = manager._validate(parameters())
    assert original["use_selector"] is False
    commands = [manager._command("cli_job", manager._validate(parameters(use_selector=enabled)))
                for enabled in (False, True)]
    assert manager._command("cli_job", original) == commands[0]
    flag = commands[0].index("--coin-selector")
    assert [command[flag + 1] for command in commands] == ["off", "on"]
    assert commands[0][:flag + 1] == commands[1][:flag + 1]
    bundle_flag = commands[1].index("--selector-bundle")
    assert commands[1][bundle_flag + 1] == str(manager._jobs["cli_job"]["_selector_bundle_path"])
    assert "--selector-bundle" not in commands[0]
    assert commands[0][flag + 2:] == commands[1][flag + 2:bundle_flag] + commands[1][bundle_flag + 2:]
    for field in ("model_path", "selector_model_path", "selector_bundle", "coin_selector"):
        with pytest.raises(ValueError):
            manager._validate(parameters(**{field: "untrusted"}))


class FinishedProcess:
    """Complete a bounded CLI job without loading any engine or model."""
    def __init__(self, command):
        self.command = command
        self.stdout = io.StringIO("")
        self.returncode = None

    def wait(self, timeout=None):
        directory = Path(self.command[self.command.index("--output-dir") + 1])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "equity.csv").write_text(
            "timestamp,equity\n2025-01-01,10000\n2025-07-01,10100\n", encoding="utf-8")
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.mark.parametrize("enabled", [False, True])
def test_selector_survives_job_metadata_restart_history_and_result(manager, monkeypatch, enabled):
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen",
                        lambda command, **kwargs: FinishedProcess(command))
    submitted = manager.submit(parameters(use_selector=enabled))
    manager._worker_thread.join(timeout=5)
    assert not manager._worker_thread.is_alive()
    job = manager.list()[0]
    assert job["status"] == "succeeded"
    assert job["parameters"]["use_selector"] is enabled
    metadata = json.loads((manager.reports_dir / job["id"] / "dashboard_job.json").read_text(encoding="utf-8"))
    assert metadata["parameters"]["use_selector"] is enabled
    if enabled:
        assert job["selector_model_id"] == "policy-initial"
        assert metadata["selector_bundle_sha256"] == job["selector_bundle_sha256"]
        assert metadata["selector_model_id"] == job["selector_model_id"]
        assert "_selector_bundle_path" not in job
    else:
        assert "selector_bundle_sha256" not in job
    assert manager.history()["items"][0]["parameters"]["use_selector"] is enabled
    assert list_backtests(manager.reports_dir)[0]["parameters"]["use_selector"] is enabled
    assert load_backtest(manager.reports_dir, job["id"])["parameters"]["use_selector"] is enabled
    manager.close()
    restored = BacktestJobs(manager.data_dir, manager.reports_dir)
    try:
        assert restored.list()[0]["id"] == submitted["id"]
        assert restored.list()[0]["parameters"]["use_selector"] is enabled
    finally:
        restored.close()


def test_legacy_jobs_are_off_and_research_tasks_keep_their_own_contract(manager, tmp_path):
    manager.store.save_job({"id": "legacy", "kind": "backtest", "status": "succeeded",
                            "parameters": parameters(), "logs": []})
    assert manager.history()["items"][0]["parameters"]["use_selector"] is False
    restored = BacktestJobs(manager.data_dir, manager.reports_dir)
    try:
        assert restored.list()[0]["parameters"]["use_selector"] is False
    finally:
        restored.close()
    manager.register_task_type("robust", lambda run_id, params, output, config: ["research", "--own-flag"])
    manager._jobs["research_job"] = {"kind": "robust", "_config_path": tmp_path / "research.yaml"}
    assert manager._command("research_job", {"use_selector": True}) == ["research", "--own-flag"]
    manager.store.save_job({"id": "research_history", "kind": "robust", "status": "succeeded",
                            "parameters": {"windows": 4}, "logs": []})
    research = next(row for row in manager.history()["items"] if row["id"] == "research_history")
    assert research["parameters"] == {"windows": 4}


@pytest.mark.parametrize("raw,expected", [({}, False), ({"use_selector": True}, True),
                                         ({"use_selector": False}, False), ({"use_selector": "true"}, False)])
def test_report_projection_defaults_to_off_without_coercing_strings(tmp_path, raw, expected):
    folder = tmp_path / "web_saved"
    folder.mkdir()
    (folder / "equity.csv").write_text("timestamp,equity\n2025-01-01,10000\n2025-07-01,10100\n", encoding="utf-8")
    (folder / "dashboard_job.json").write_text(json.dumps({"status": "succeeded", "kind": "backtest",
        "parameters": {**parameters(), **raw, "model_path": "excluded"}}), encoding="utf-8")
    report = load_backtest(tmp_path, folder.name)
    assert report["parameters"]["use_selector"] is expected
    assert "model_path" not in report["parameters"]


class QueuedThread:
    """Keep admission queued until the test explicitly requests its command."""
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout=None):
        pass


def test_queued_task_uses_its_submitted_package_after_current_alias_changes(manager, monkeypatch):
    import backtest.coin_selector as selector
    monkeypatch.setattr("dashboard.backtest_jobs.Thread", QueuedThread)
    submitted = manager.submit(parameters(use_selector=True))
    assert submitted["status"] == "queued"
    manifest = manager.reports_dir / ".dashboard" / "selectors" / submitted["id"] / "manifest.json"
    _, original = selector.read_bundle(manifest)
    assert submitted["selector_bundle_sha256"] == sha256_file(manifest)
    assert submitted["selector_model_id"] == "policy-initial"
    assert manager._jobs[submitted["id"]]["_selector_bundle_path"] == manifest
    assert str(manifest) not in json.dumps(submitted)
    assert "_selector_bundle_path" not in manager.store.get(submitted["id"])
    make_bundle(selector.DEFAULT_BUNDLE.parent, "replacement")
    assert selector.read_bundle()[1]["candidate"]["model_id"] == "policy-replacement"
    command = manager._command(submitted["id"], submitted["parameters"])
    frozen_path = Path(command[command.index("--selector-bundle") + 1])
    assert frozen_path == manifest
    assert selector.read_bundle(frozen_path)[1] == original
    assert json.loads((manifest.parent / "policy.json").read_text(encoding="utf-8"))["weights"] == ["initial"]


@pytest.mark.parametrize("payload", [parameters(), parameters(use_selector=False)])
def test_off_submission_does_not_read_or_snapshot_a_model(manager, monkeypatch, payload):
    def forbidden(*args, **kwargs):
        pytest.fail("Off backtests must not read any selector package")
    monkeypatch.setattr("backtest.coin_selector.read_bundle", forbidden)
    monkeypatch.setattr("backtest.coin_selector.snapshot_bundle", forbidden)
    monkeypatch.setattr("dashboard.backtest_jobs.Thread", QueuedThread)
    submitted = manager.submit(payload)
    assert submitted["parameters"]["use_selector"] is False
    assert "selector_model_id" not in submitted
    assert not (manager.reports_dir / ".dashboard" / "selectors").exists()
    command = manager._command(submitted["id"], submitted["parameters"])
    assert command[command.index("--coin-selector") + 1] == "off"
    assert "--selector-bundle" not in command


@pytest.mark.parametrize("damage", ["missing", "weights_changed", "invalid_json_shape"])
def test_unusable_package_rejects_submission_before_task_or_slot_is_created(manager, monkeypatch, damage):
    import backtest.coin_selector as selector
    if damage == "missing":
        selector.DEFAULT_BUNDLE.unlink()
    elif damage == "weights_changed":
        (selector.DEFAULT_BUNDLE.parent / "policy.json").write_text("{}", encoding="utf-8")
    else:
        selector.DEFAULT_BUNDLE.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("dashboard.backtest_jobs.Thread", lambda **kwargs: pytest.fail("Must not launch a worker"))
    with pytest.raises(ValueError, match="Unable to freeze.*task was not created"):
        manager.submit(parameters(use_selector=True))
    assert manager._active is None
    assert manager.list() == []
    assert manager.history()["total"] == 0


def test_copy_corruption_rejects_before_admission(manager, monkeypatch):
    import backtest.coin_selector as selector
    original = selector.snapshot_bundle
    def corrupted_copy(identity, directory):
        manifest = original(identity, directory)
        (directory / "policy.json").write_text("{}", encoding="utf-8")
        return manifest
    monkeypatch.setattr(selector, "snapshot_bundle", corrupted_copy)
    with pytest.raises(ValueError, match="Unable to freeze"):
        manager.submit(parameters(use_selector=True))
    assert manager._active is None
    assert manager.list() == []


def test_enabled_command_cannot_fall_back_to_the_current_model_alias(manager, tmp_path):
    manager._jobs["missing_snapshot"] = {"kind": "backtest", "_config_path": tmp_path / "frozen.yaml",
                                          "config_sha256": "a" * 64}
    with pytest.raises(ValueError, match="no frozen selector package"):
        manager._command("missing_snapshot", manager._validate(parameters(use_selector=True)))


@pytest.fixture
def original_registration(dashboard_project):
    """Only fixed registration metadata is needed to test admission and argv."""
    root = dashboard_project
    symbols = [f"S{index:02d}/USDT" for index in range(60)]
    hashes = {symbol: f"hash-{index}" for index, symbol in enumerate(symbols)}
    data = {"symbols": symbols, "start": "2020-01-01", "end": "2026-09-18",
            "initial_capital": 100000, "engine_frame_hashes": hashes, "input_files": {}}
    smart = {"start": data["start"], "end": data["end"], "capital": 100000,
             "engine_frame_hashes": hashes, "input_files": {}, "arms": {"smart": {
                 "parameters": {"account": {"mode": "spot_margin"}, "execution": {"slippage_bps": 5},
                                "routing": {"TREND_UP": "TrendBreakout"}},
                 "engine_options": {"initial_capital": 100000, "capital_allocation": {"enabled": True}}}}}
    for relative, value in (("reports/multicoin_100k_20261004/registration.json", data),
                            ("reports/smart_capital_100k_20261004/registration.json", smart)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    base = root / "config" / "params.yaml"
    return root, data, smart, base.read_bytes()


def test_absent_local_reports_do_not_fall_back_to_workspace_registration(manager, dashboard_project):
    assert not (dashboard_project / "reports").exists()
    assert not (dashboard_project / "data").exists()
    with pytest.raises(ValueError, match="preset metadata is unavailable or invalid"):
        manager.submit({"preset": "original_100k", "use_selector": False})
    assert manager.list() == []
    assert manager._active is None


@pytest.mark.parametrize("payload", [
    {"preset": "arbitrary", "use_selector": False}, {"preset": "original_100k"},
    {"preset": "original_100k", "use_selector": "false"},
    {"preset": "original_100k", "use_selector": 0},
    {"preset": "original_100k", "use_selector": None},
    *[{"preset": "original_100k", "use_selector": False, field: "override"}
      for field in ("capital", "symbols", "start", "end", "seed", "strategy", "registration_path", "selector_bundle")],
])
def test_original_preset_rejects_unknown_presets_non_booleans_and_overrides(manager, payload):
    with pytest.raises(ValueError):
        manager.submit(payload)
    assert manager.list() == []
    assert manager._active is None


@pytest.mark.parametrize("enabled", [False, True])
def test_original_preset_fixed_parameters_do_not_use_ordinary_form_limits(manager, monkeypatch,
                                                                         original_registration, enabled):
    _, data, _, _ = original_registration
    monkeypatch.setattr("dashboard.backtest_jobs.list_markets", lambda *args: pytest.fail("Preset owns its registered inputs"))
    result = manager._validate({"preset": "original_100k", "use_selector": enabled})
    assert result == {"preset": "original_100k", "use_selector": enabled,
                      "source": "local", "symbols": data["symbols"], "start": "2020-01-01",
                      "end": "2026-09-18", "capital": 100000.0, "slippage_bps": 5,
                      "seed": 42, "strategy": {"family": "configured", "parameters": {}}}
    with pytest.raises(ValueError, match="between 1 and 4"):
        manager._validate(parameters(symbols=data["symbols"]))
    with pytest.raises(ValueError, match="between 30 and 1096"):
        manager._validate(parameters(start="2020-01-01", end="2026-09-18"))


@pytest.mark.parametrize("enabled", [False, True])
def test_original_preset_snapshots_smart_parameters_and_uses_fixed_runner(manager, monkeypatch,
                                                                         original_registration, enabled):
    import yaml
    root, _, smart, original_base = original_registration
    monkeypatch.setattr("dashboard.backtest_jobs.Thread", QueuedThread)
    if not enabled:
        monkeypatch.setattr("backtest.coin_selector.read_bundle",
                            lambda *args: pytest.fail("Off preset must not load a model"))
    submitted = manager.submit({"preset": "original_100k", "use_selector": enabled})
    job = manager._jobs[submitted["id"]]
    assert job["kind"] == "backtest" and job["result_file"] == "equity.csv"
    assert yaml.safe_load(job["_config_path"].read_text(encoding="utf-8")) == smart["arms"]["smart"]["parameters"]
    assert (root / "config" / "params.yaml").read_bytes() == original_base
    command = manager._command(job["id"], job["parameters"])
    assert command[1:3] == ["-u", str(root / "scripts" / "run_selector_backtest.py")]
    assert command[command.index("--coin-selector") + 1] == ("on" if enabled else "off")
    assert command[command.index("--output-dir") + 1] == str(manager.reports_dir / job["id"])
    assert not {"--symbols", "--start", "--end", "--capital", "--data-dir", "--config"}.intersection(command)
    if enabled:
        assert command[command.index("--selector-bundle") + 1] == str(job["_selector_bundle_path"])
        assert submitted["selector_model_id"] == "policy-initial"
    else:
        assert "--selector-bundle" not in command
    assert str(root / "reports") not in json.dumps(submitted)


def test_original_preset_metadata_and_results_retain_all_sixty_symbols(manager, monkeypatch,
                                                                      original_registration):
    _, data, _, _ = original_registration
    monkeypatch.setattr("dashboard.backtest_jobs.subprocess.Popen",
                        lambda command, **kwargs: FinishedProcess(command))
    submitted = manager.submit({"preset": "original_100k", "use_selector": False})
    manager._worker_thread.join(timeout=5)
    assert not manager._worker_thread.is_alive()
    job = manager.list()[0]
    assert job["status"] == "succeeded"
    assert manager.history()["items"][0]["parameters"] == submitted["parameters"]
    metadata = json.loads((manager.reports_dir / job["id"] / "dashboard_job.json").read_text(encoding="utf-8"))
    assert metadata["parameters"] == submitted["parameters"]
    report = load_backtest(manager.reports_dir, job["id"])
    assert report["parameters"] == submitted["parameters"]
    assert list_backtests(manager.reports_dir)[0]["parameters"]["symbols"] == data["symbols"]


def test_invalid_original_registration_refuses_to_create_a_task(manager, original_registration):
    root, _, smart, _ = original_registration
    smart["arms"]["smart"]["engine_options"]["capital_allocation"]["enabled"] = False
    (root / "reports/smart_capital_100k_20261004/registration.json").write_text(json.dumps(smart), encoding="utf-8")
    with pytest.raises(ValueError, match="preset metadata is unavailable or invalid"):
        manager.submit({"preset": "original_100k", "use_selector": False})
    assert manager.list() == []
    assert manager._active is None
