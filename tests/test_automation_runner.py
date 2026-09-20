"""Automation supervision, factual day counts and owned retention boundaries."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest

from scripts import run_automation as automation


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    config = automation.read_json(automation.ROOT / "config" / "automation.json")
    root = tmp_path / "repository"
    root.mkdir()
    (root / "config").mkdir()
    config_path = root / "config" / "automation.json"
    automation.save_json(config_path, config)
    monkeypatch.setattr(automation, "ROOT", root)
    files = {"core.py": "a" * 64}
    monkeypatch.setattr(automation, "source_identity", lambda: {"sha256": automation.value_sha256(files), "files": files})
    target = automation.managed_root(config)
    automation.initialize_root(target)
    return root, target, config, config_path


def receipt(target, identifier, finished, *, task="nightly-data", synthetic=False, status="passed", pinned=False, sealed=True):
    directory = target / "runs" / identifier
    directory.mkdir()
    automation.save_json(directory / ".automation-owner.json",
                         {"schema_version": automation.OWNER, "repository": str(automation.ROOT.resolve()), "run_id": identifier})
    row = {"schema_version": automation.OWNER, "run_id": identifier, "task": task,
           "started_at": (finished - timedelta(minutes=10)).isoformat(), "finished_at": finished.isoformat(),
           "status": status, "synthetic": synthetic, "production_evidence": status == "passed" and not synthetic,
           "pinned": pinned}
    if sealed:
        config_path = automation.ROOT / "config" / "automation.json"
        config = automation.read_json(config_path)
        (directory / "config_input.json").write_bytes(config_path.read_bytes())
        automation.save_json(directory / "config.json", config)
        source = automation.source_identity()
        automation.save_json(directory / "source_identity.json", source)
        row["source_sha256"], row["config_sha256"] = source["sha256"], automation.sha256(config_path)
        captured = finished - timedelta(minutes=5)
        boundary = captured.replace(hour=0, minute=0, second=0, microsecond=0)
        count = (boundary.date() - datetime.fromisoformat(config["start"]).date()).days
        caches = {"1d": {"schema_version": "binance-cache/v2", "provider": "binance", "market_type": "spot",
                         "timeframe": "1d", "generated_at": captured.isoformat(), "failures": [], "symbols": {}}}
        for symbol in config["symbols"]:
            caches["1d"]["symbols"][symbol] = {"provider": "binance", "market_type": "spot", "timeframe": "1d",
                "symbol": symbol.replace("/", "-"), "rows": count, "sha256": "1" * 64,
                "first": config["start"], "last": (boundary - timedelta(days=1)).isoformat(),
                "requested_start": config["start"], "requested_end": captured.date().isoformat(),
                "coverage": {"status": "complete", "end_policy": "closed_bars_only", "expected_rows": count,
                             "effective_end_exclusive": boundary.isoformat()}}
        automation.save_json(directory / "cache_identities.json", caches)
        row["steps"] = [{"name": "fetch_1d", "status": "passed", "exit_code": 0}]
        automation.seal_run(directory, row)
    automation.save_json(directory / "run_log.json", row)
    return row


def test_exclusive_lock_never_steals_or_removes_another_owners_lock(workspace):
    _, target, _, _ = workspace
    with automation.exclusive_lock(target):
        with pytest.raises(RuntimeError, match="another automation"):
            with automation.exclusive_lock(target):
                pytest.fail("parallel task acquired a held lock")
        assert (target / ".automation.lock").is_file()
    assert not (target / ".automation.lock").exists()
    with automation.exclusive_lock(target):
        automation.save_json(target / ".automation.lock", {"token": "different_owner"})
    assert automation.read_json(target / ".automation.lock")["token"] == "different_owner"


def test_failed_task_records_failure_local_alert_and_atomic_indexes(workspace, monkeypatch):
    _, target, config, path = workspace
    def fail(*args, **kwargs):
        raise RuntimeError("independent cache hash mismatch")
    monkeypatch.setattr(automation, "execute_task", fail)
    row = automation.run_task("nightly-data", config, path, target)
    assert row["status"] == "failed" and row["production_evidence"] is False
    assert "cache hash mismatch" in row["reason"]
    assert (target / "runs" / row["run_id"] / "ALERT.md").is_file()
    assert "no external delivery" in (target / "ALERT.md").read_text()
    assert automation.read_json(target / "index.json")["runs"][0]["status"] == "failed"
    assert "failed" in (target / "index.csv").read_text()
    assert not list(target.glob("*.tmp"))


def test_source_drift_blocks_success_and_synthetic_never_becomes_production_evidence(workspace, monkeypatch):
    _, target, config, path = workspace
    monkeypatch.setattr(automation, "execute_task", lambda *a, **k: {"status": "pass"})
    identities = iter([{"sha256": "old", "files": {}}, {"sha256": "new", "files": {}}])
    monkeypatch.setattr(automation, "source_identity", lambda: next(identities))
    row = automation.run_task("weekly-smoke", config, path, target)
    assert row["status"] == "failed" and "changed during" in row["reason"]
    monkeypatch.setattr(automation, "source_identity", lambda: {"sha256": "stable", "files": {}})
    row = automation.run_task("weekly-smoke", config, path, target)
    assert row["status"] == "passed" and row["synthetic"] is True and row["production_evidence"] is False
    assert automation.sha256(target / "runs" / row["run_id"] / "config_input.json") == row["config_sha256"]


def test_process_failure_and_timeout_are_visible_in_step_receipts(workspace):
    root, target, _, _ = workspace
    directory = target / "runs" / "step_test"
    directory.mkdir()
    record = {"steps": []}
    runner = automation.Runner(directory, record, 10)
    with pytest.raises(RuntimeError, match="exited 3"):
        runner.step("failure", [sys.executable, "-c", "print('diagnostic'); raise SystemExit(3)"])
    assert record["steps"][0]["exit_code"] == 3
    assert "diagnostic" in (directory / record["steps"][0]["log"]).read_text()
    runner = automation.Runner(directory, record, .05)
    with pytest.raises(TimeoutError):
        runner.step("timeout", [sys.executable, "-c", "import time; time.sleep(5)"])
    assert record["steps"][-1]["status"] == "timeout"
    assert automation.read_json(directory / "run_log.json")["steps"][-1]["finished_at"]


def test_14_real_utc_days_required_and_synthetic_duplicate_future_days_do_not_count(workspace):
    _, target, _, _ = workspace
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    for offset in range(14):
        receipt(target, f"real_{offset:02}", now - timedelta(days=offset))
    receipt(target, "future", now + timedelta(days=1))
    receipt(target, "same_day_duplicate", now - timedelta(minutes=1))
    receipt(target, "synthetic", now - timedelta(days=14), synthetic=True)
    receipt(target, "weekly_does_not_count", now - timedelta(days=14), task="weekly-matrix")
    result = automation.operational_status(target, now=now)
    assert result["consecutive_real_daily_refresh_days"] == 14
    assert result["daily_data_observation"] == "pass"
    # A latest failed refresh is visible even after a same-day earlier success.
    receipt(target, "later_failure", now + timedelta(minutes=1), status="failed")
    result = automation.operational_status(target, now=now + timedelta(minutes=2))
    assert result["consecutive_real_daily_refresh_days"] == 0
    assert result["live_admission"] is False


def test_missing_calendar_day_breaks_streak_and_simulated_receipts_cannot_fill_it(workspace):
    _, target, _, _ = workspace
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    for offset in [1, 2, 4, 5]:
        receipt(target, f"day{offset}", now - timedelta(days=offset))
    receipt(target, "fixture3", now - timedelta(days=3), synthetic=True)
    result = automation.operational_status(target, now=now)
    assert result["consecutive_real_daily_refresh_days"] == 2
    assert result["daily_data_observation"] == "pending_evidence"


@pytest.mark.parametrize("damage", ["unsealed", "edited_receipt", "edited_cache", "bad_coverage", "wrong_capture_day"])
def test_operational_days_require_sealed_actual_capture_evidence(workspace, damage):
    _, target, _, _ = workspace
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    row = receipt(target, "candidate", now, sealed=damage != "unsealed")
    directory = target / "runs" / "candidate"
    if damage == "edited_receipt":
        row["source_sha256"] = "forged"
        automation.save_json(directory / "run_log.json", row)
    elif damage in {"edited_cache", "bad_coverage", "wrong_capture_day"}:
        caches = automation.read_json(directory / "cache_identities.json")
        if damage == "wrong_capture_day":
            caches["1d"]["generated_at"] = (now - timedelta(days=1)).isoformat()
        else:
            caches["1d"]["symbols"]["BTC/USDT"]["coverage"]["status"] = "partial"
        automation.save_json(directory / "cache_identities.json", caches)
        if damage != "edited_cache":
            row.pop("evidence_seal_sha256")
            automation.seal_run(directory, row)
            automation.save_json(directory / "run_log.json", row)
    result = automation.operational_status(target, now=now)
    assert result["consecutive_real_daily_refresh_days"] == 0
    assert "unverified" in result["streak_stopped_reason"]


@pytest.mark.parametrize("identity_change", ["source", "config"])
def test_source_or_config_identity_change_breaks_real_day_streak(workspace, monkeypatch, identity_change):
    _, target, config, path = workspace
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    receipt(target, "old", now - timedelta(days=1))
    if identity_change == "source":
        files = {"core.py": "b" * 64}
        monkeypatch.setattr(automation, "source_identity", lambda: {"files": files, "sha256": automation.value_sha256(files)})
    else:
        config["seed"] += 1
        automation.save_json(path, config)
    receipt(target, "current", now)
    result = automation.operational_status(target, now=now)
    assert result["consecutive_real_daily_refresh_days"] == 1
    assert result["streak_stopped_reason"] == "source_or_configuration_changed"


def test_prune_defaults_to_plan_and_preserves_pinned_referenced_and_unowned(workspace):
    root, target, config, _ = workspace
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    old = now - timedelta(days=100)
    for identifier in ("delete_owned", "pinned_receipt", "pinned_file", "pinned_config", "referenced_frozen", "not_owned"):
        receipt(target, identifier, old, pinned=identifier == "pinned_receipt")
    (target / "runs" / "not_owned" / ".automation-owner.json").unlink()
    (target / "runs" / "pinned_file" / "PINNED").write_text("retain")
    config["pinned_runs"] = ["pinned_config"]
    (root / "docs").mkdir()
    (root / "docs" / "acceptance.md").write_text("sealed evidence: outputs/automation/runs/referenced_frozen/run_log.json")
    result = automation.prune(target, config, now=now)
    assert result["dry_run"] is True and result["candidates"] == ["delete_owned"]
    assert (target / "runs" / "delete_owned").is_dir()
    result = automation.prune(target, config, apply=True, now=now)
    assert result["dry_run"] is False
    assert not (target / "runs" / "delete_owned").exists()
    for identifier in ("pinned_receipt", "pinned_file", "pinned_config", "referenced_frozen", "not_owned"):
        assert (target / "runs" / identifier).is_dir()


def test_prune_preserves_links_and_running_receipts(workspace, monkeypatch):
    _, target, config, _ = workspace
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    for identifier in ("contains_link", "running"):
        receipt(target, identifier, now - timedelta(days=100), status="running" if identifier == "running" else "passed")
    symbolic = target / "runs" / "contains_link" / "external-link"
    symbolic.write_text("modeled junction")
    original = automation.is_link
    monkeypatch.setattr(automation, "is_link", lambda path: path == symbolic or original(path))
    result = automation.prune(target, config, apply=True, now=now)
    assert result["candidates"] == []
    assert {row["reason"] for row in result["kept"]} == {"contains_link_or_junction", "not_completed"}


def test_prune_preserves_a_nightly_run_referenced_by_another_owned_matrix(workspace):
    _, target, config, _ = workspace
    now = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    receipt(target, "old_nightly", now - timedelta(days=100))
    receipt(target, "new_matrix", now - timedelta(days=1), task="weekly-matrix")
    automation.save_json(target / "runs" / "new_matrix" / "inputs.json", {"nightly_receipt": "old_nightly/run_log.json"})
    result = automation.prune(target, config, apply=True, now=now)
    assert result["candidates"] == []
    assert (target / "runs" / "old_nightly").is_dir()


@pytest.mark.parametrize("output", [".", "outputs", "docs", "../outside", "outputs/../reports"])
def test_managed_output_root_cannot_target_repository_or_protected_evidence(workspace, output):
    _, _, config, _ = workspace
    with pytest.raises(ValueError, match="subdirectory"):
        automation.managed_root({**config, "output_root": output})


def test_existing_unowned_nonempty_directory_cannot_be_claimed(workspace):
    root, _, _, _ = workspace
    path = root / "outputs" / "valuable_existing"
    path.mkdir()
    (path / "evidence.txt").write_text("retain")
    with pytest.raises(ValueError, match="nonempty"):
        automation.initialize_root(path)


def test_cli_status_and_invalid_flag_boundaries(workspace, capsys):
    _, _, _, path = workspace
    assert automation.main(["status", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["consecutive_real_daily_refresh_days"] == 0
    with pytest.raises(SystemExit) as exc:
        automation.main(["weekly-smoke", "--apply"])
    assert exc.value.code == 2


def test_smoke_uses_fixed_synthetic_main_full_artifacts_and_replay(workspace, monkeypatch):
    _, target, config, _ = workspace
    directory = target / "runs" / "plan"
    directory.mkdir()
    runner = automation.Runner(directory, {"steps": []}, 10)
    calls = []
    monkeypatch.setattr(runner, "step", lambda name, args: calls.append((name, args)))
    monkeypatch.setattr("scripts.evaluate_matrix.evaluate_report", lambda *a, **k: {"status": "pass"})
    assert automation.execute_task("weekly-smoke", runner, config)["status"] == "pass"
    args = calls[0][1]
    assert args[args.index("--source") + 1] == "synthetic"
    assert args[args.index("--report-profile") + 1] == "full"
    assert args[args.index("--start") + 1] == "2020-01-01"
    assert calls[1][1][-2] == "--replay-manifest"
    assert not any("fetch" in str(arg) for _, args in calls for arg in args)


def test_weekly_matrix_replays_every_owned_full_manifest(workspace, monkeypatch):
    _, target, config, _ = workspace
    directory = target / "runs" / "matrix_plan"
    directory.mkdir()
    runner = automation.Runner(directory, {"steps": []}, 10)
    calls = []
    monkeypatch.setattr(runner, "step", lambda name, args: calls.append((name, args)))
    monkeypatch.setattr(automation, "nightly", lambda *args: {"status": "pass"})
    monkeypatch.setattr("scripts.evaluate_matrix.evaluate_matrix", lambda *a, **k: {
        "status": "pass", "cells": [{"status": "pass", "report_dir": str(directory / "matrix" / f"cell{i}")} for i in range(2)]})
    assert automation.execute_task("weekly-matrix", runner, config)["status"] == "pass"
    assert [name for name, _ in calls] == ["matrix", "replay_0000", "replay_0001"]
    args = calls[0][1]
    assert args[args.index("--report-profile") + 1] == "full"
    assert args[args.index("--output-dir") + 1] == str(directory / "matrix")


def test_skip_fetch_rejects_cache_mutated_after_verified_nightly(workspace, monkeypatch):
    _, target, config, _ = workspace
    now = automation.utcnow()
    captured = receipt(target, "nightly", now - timedelta(minutes=1))
    caches = automation.read_json(target / "runs" / "nightly" / "cache_identities.json")
    caches["1d"]["symbols"]["BTC/USDT"]["sha256"] = "2" * 64
    monkeypatch.setattr("scripts.run_backtest_matrix.verify_cache", lambda *args, **kwargs: caches["1d"])
    directory = target / "runs" / "matrix_attempt"
    directory.mkdir()
    runner = automation.Runner(directory, {"steps": [], "source_sha256": captured["source_sha256"],
                                           "config_sha256": captured["config_sha256"]}, 10)
    with pytest.raises(ValueError, match="differs from the sealed"):
        automation.execute_task("weekly-matrix", runner, config, skip_fetch=True)


def test_monthly_universe_never_changes_formal_csv_and_missing_source_is_insufficient(workspace):
    root, target, config, path = workspace
    formal = root / config["universe"]["file"]
    original = "symbol,listed_at,delisted_at,source\nBTC-USDT,2020-01-01,,original\n"
    formal.write_text(original)
    row = automation.run_task("monthly-universe", config, path, target)
    assert row["status"] == "insufficient_data" and row["production_evidence"] is False
    assert formal.read_text() == original
    report = automation.read_json(target / "runs" / row["run_id"] / "universe_diff.json")
    assert report["status"] == "insufficient_data"


@pytest.mark.parametrize("mismatch", [False, True])
def test_monthly_universe_compares_independent_dates_with_explicit_diff(workspace, mismatch):
    root, target, config, _ = workspace
    formal = root / config["universe"]["file"]
    formal.write_text("symbol,listed_at,delisted_at,source\nBTC-USDT,2020-01-01,,original\n")
    config["universe"]["independent_evidence"] = "config/independent_universe.json"
    automation.save_json(root / config["universe"]["independent_evidence"], {
        "schema_version": "universe-observation/v1", "independent": True, "source_id": "official-lifecycle-export",
        "observed_at": automation.utcnow().isoformat(), "records": [{"symbol": "BTC/USDT",
        "listed_at": "2020-01-02" if mismatch else "2020-01-01", "delisted_at": None, "source_ref": "export-row-1"}]})
    directory = target / "runs" / "universe"
    directory.mkdir()
    result = automation.audit_universe(directory, config)
    assert result["status"] == ("fail" if mismatch else "pass")
    assert len(result["differences"]) == (1 if mismatch else 0)
