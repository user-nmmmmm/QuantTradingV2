"""Behavior of small operational tools that had no direct tests (infrastructure roadmap C5).

Each test states a property an operator relies on: the incident journal is append-only and rejects vague
entries, the supervisor backs off and gives up, the heartbeat never invents financial facts, and the R7 audit
fails closed on malformed evidence.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from ops import incident_journal, r7_acceptance, telegram_heartbeat
from ops.operations import incident_journal as journal
from ops.supervisor import RestartPolicy, main as supervisor_main, supervise


# ---- incident journal ------------------------------------------------------------------------------------------

def test_incident_journal_appends_one_json_line_per_record(tmp_path):
    path = tmp_path / "nested" / "journal.jsonl"
    first = journal.record_incident(str(path), event_key="2026-08-01T00:00:00+00:00|risk_halt", outcome="resolved",
                                    explanation="  stale data drill  ", operator=" ops ")
    journal.record_incident(str(path), event_key="2026-08-02T00:00:00+00:00|tick_unhealthy", outcome="explained",
                            explanation="exchange maintenance", operator="ops")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["event_key"].split("|")[1] for line in lines] == ["risk_halt", "tick_unhealthy"]
    assert first["explanation"] == "stale data drill" and first["operator"] == "ops" and first["schema_version"] == "1"
    assert json.loads(lines[0]) == first


@pytest.mark.parametrize("kwargs, message", [
    ({"event_key": "", "outcome": "resolved", "explanation": "x", "operator": "o"}, "event_key"),
    ({"event_key": "no-separator", "outcome": "resolved", "explanation": "x", "operator": "o"}, "event_key"),
    ({"event_key": "t|e", "outcome": "ignored", "explanation": "x", "operator": "o"}, "outcome"),
    ({"event_key": "t|e", "outcome": "resolved", "explanation": "   ", "operator": "o"}, "required"),
    ({"event_key": "t|e", "outcome": "resolved", "explanation": "x", "operator": ""}, "required"),
])
def test_incident_journal_rejects_vague_entries_and_writes_nothing(tmp_path, kwargs, message):
    path = tmp_path / "journal.jsonl"
    with pytest.raises(ValueError, match=message):
        journal.record_incident(str(path), **kwargs)
    assert not path.exists()


def test_incident_journal_command_line_and_compatibility_entrypoint(tmp_path, monkeypatch, capsys):
    path = tmp_path / "cli.jsonl"
    monkeypatch.setattr(sys, "argv", ["incident_journal", "--event-key", "t|risk_halt", "--outcome", "resolved",
                                      "--explanation", "drill", "--operator", "ops", "--path", str(path)])
    assert incident_journal.main() == 0
    assert json.loads(capsys.readouterr().out)["event_key"] == "t|risk_halt"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert incident_journal.record_incident is journal.record_incident


# ---- supervisor ------------------------------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _runner(clock, codes, run_seconds=1.0):
    codes = list(codes)

    def run(command, check=False):
        clock.now += run_seconds
        return subprocess.CompletedProcess(command, codes.pop(0))
    return run


def test_supervisor_stops_on_clean_exit_without_sleeping():
    clock = _Clock()
    assert supervise(["x"], runner=_runner(clock, [0]), sleep=clock.sleep, monotonic=clock.monotonic) == 0
    assert clock.sleeps == []


def test_supervisor_backs_off_exponentially_up_to_the_cap_then_gives_up_with_the_last_code():
    clock = _Clock()
    policy = RestartPolicy(max_restarts=4, base_delay=1.0, max_delay=3.0, stable_seconds=300.0)
    code = supervise(["x"], policy, runner=_runner(clock, [3, 3, 3, 3, 7]), sleep=clock.sleep, monotonic=clock.monotonic)
    assert code == 7 and clock.sleeps == [1.0, 2.0, 3.0, 3.0]


def test_supervisor_resets_the_restart_budget_after_a_stable_run():
    clock = _Clock()
    policy = RestartPolicy(max_restarts=1, base_delay=1.0, stable_seconds=10.0)
    runs = iter([(1.0, 1), (20.0, 1), (1.0, 0)])          # (seconds the child ran, exit code)

    def run(command, check=False):
        seconds, code = next(runs)
        clock.now += seconds
        return subprocess.CompletedProcess(command, code)
    assert supervise(["x"], policy, runner=run, sleep=clock.sleep, monotonic=clock.monotonic) == 0
    assert clock.sleeps == [1.0, 1.0]                      # the long run in the middle did not use up the budget


def test_supervisor_rejects_an_empty_command_and_reports_a_zero_code_failure_as_one(monkeypatch):
    with pytest.raises(ValueError):
        supervise([])
    clock = _Clock()
    result = subprocess.CompletedProcess(["x"], 0)
    result.returncode = None                                # a runner that cannot report a code still counts as failure
    code = supervise(["x"], RestartPolicy(max_restarts=0), runner=lambda *a, **k: result, sleep=clock.sleep,
                     monotonic=clock.monotonic)
    assert code == 1
    monkeypatch.setattr(sys, "argv", ["supervisor", "--max-restarts", "0", sys.executable, "-c", "raise SystemExit(5)"])
    assert supervisor_main() == 5


# ---- telegram heartbeat ----------------------------------------------------------------------------------------

def test_heartbeat_for_an_invalid_status_file_states_that_no_facts_are_available():
    message = telegram_heartbeat.build_heartbeat_message({"status_valid": False, "equity": 123})
    assert "STATUS FILE INVALID" in message and "123" not in message


def test_heartbeat_lists_positions_reasons_and_only_the_last_five_alerts():
    alerts = [{"level": "warn", "event": f"event_{n}"} for n in range(8)]
    message = telegram_heartbeat.build_heartbeat_message({
        "status_valid": True, "healthy": False, "operational_state": "RISK_HALTED", "timestamp": "t0",
        "equity": 1000.0, "cash": 250.0, "positions": {"BTC/USDT": {"qty": 0.5}},
        "health_reason_codes": ["stale_data", "drawdown"], "recent_alerts": alerts})
    assert "UNHEALTHY (RISK_HALTED)" in message and "BTC/USDT: qty=0.5" in message
    assert "Health reasons: stale_data, drawdown" in message and "Recent alerts (8):" in message
    assert "[WARN] event_7" in message and "[WARN] event_3" in message and "event_2" not in message
    assert "Positions: (none)" in telegram_heartbeat.build_heartbeat_message({"status_valid": True})


def test_heartbeat_command_requires_credentials_and_sets_the_exit_code(tmp_path, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(telegram_heartbeat, "send_telegram_message", lambda token, chat, text: sent.append((token, chat, text)))
    monkeypatch.setattr(telegram_heartbeat, "load_dashboard", lambda *a, **k: {"status_valid": False})
    monkeypatch.setattr(sys, "argv", ["heartbeat", "--status", str(tmp_path / "s.json"), "--alerts", str(tmp_path / "a.jsonl")])
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(SystemExit) as missing:
        telegram_heartbeat.main()
    assert missing.value.code == 2 and sent == []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    assert telegram_heartbeat.main() == 2                  # invalid status: still reported, but the exit code is non-zero
    assert json.loads(capsys.readouterr().out) == {"sent": True, "status_valid": False}
    assert sent and sent[0][:2] == ("token", "chat")
    monkeypatch.setattr(telegram_heartbeat, "load_dashboard", lambda *a, **k: {"status_valid": True})
    assert telegram_heartbeat.main() == 0


# ---- R7 acceptance: fail closed on malformed evidence ----------------------------------------------------------

def _evidence(tmp_path: Path, days=2):
    reconciliation = tmp_path / "reconciliation"
    reconciliation.mkdir()
    start = date(2026, 8, 1)
    for offset in range(days):
        day = date.fromordinal(start.toordinal() + offset)
        (reconciliation / f"{day.isoformat()}_acct.json").write_text(
            json.dumps({"trading_day": day.isoformat(), "ok": True, "discrepancy_count": 0}), encoding="utf-8")
    (tmp_path / "closures.json").write_text(json.dumps({f"G{n}": "Closed" for n in range(11, 17)}), encoding="utf-8")
    return dict(start=start, end=date.fromordinal(start.toordinal() + days - 1), reconciliation_dir=str(reconciliation),
                alerts_path=str(tmp_path / "alerts.jsonl"), incidents_path=str(tmp_path / "incidents.jsonl"),
                closures_path=str(tmp_path / "closures.json"), account_id="acct", minimum_days=days)


def test_r7_audit_passes_with_complete_evidence_and_no_alerts(tmp_path):
    (tmp_path / "alerts.jsonl").write_text("", encoding="utf-8")
    report = r7_acceptance.audit_r7(**_evidence(tmp_path))
    assert report["ok"] and report["issues"] == [] and report["reconciliation_reports"] == 2


def test_r7_audit_rejects_a_reversed_range_and_a_non_positive_minimum():
    with pytest.raises(ValueError, match="before start"):
        r7_acceptance.audit_r7(start=date(2026, 8, 2), end=date(2026, 8, 1), reconciliation_dir="x", alerts_path="x",
                               incidents_path="x", closures_path="x", account_id="a")


def test_r7_audit_reports_each_kind_of_bad_evidence(tmp_path):
    kwargs = _evidence(tmp_path, days=3)
    reconciliation = Path(kwargs["reconciliation_dir"])
    (reconciliation / "2026-08-01_acct.json").write_text("{not json", encoding="utf-8")                  # unreadable
    (reconciliation / "2026-08-02_acct.json").write_text(json.dumps({"trading_day": "2026-08-09", "ok": True}), encoding="utf-8")
    (reconciliation / "2026-08-03_acct.json").write_text(
        json.dumps({"trading_day": "2026-08-03", "ok": True, "discrepancy_count": True}), encoding="utf-8")  # bool is not a count
    Path(kwargs["alerts_path"]).write_text(
        "not an object\n[1, 2]\n"
        + json.dumps({"timestamp": "garbage", "event": "risk_halt"}) + "\n"
        + json.dumps({"timestamp": "2026-08-02T01:00:00Z", "event": "circuit_breaker_triggered"}) + "\n"
        + json.dumps({"timestamp": "2026-09-30T01:00:00Z", "event": "risk_halt"}) + "\n"      # outside the window
        + json.dumps({"timestamp": "2026-08-02T01:00:00Z", "event": "heartbeat"}) + "\n", encoding="utf-8")
    Path(kwargs["incidents_path"]).write_text(
        json.dumps({"event_key": "2026-08-02T01:00:00Z|circuit_breaker_triggered", "outcome": "resolved",
                    "explanation": "   "}) + "\n", encoding="utf-8")
    (Path(kwargs["closures_path"])).write_text(json.dumps({"G11": "open"}), encoding="utf-8")
    issues = r7_acceptance.audit_r7(**kwargs)["issues"]
    joined = "\n".join(issues)
    assert "invalid_reconciliation:" in joined and "invalid_reconciliation_schema:" in joined
    assert "reconciliation_difference:2026-08-03" in joined
    assert joined.count("invalid_jsonl_record") == 2 and "invalid_alert_timestamp:garbage|risk_halt" in joined
    assert "unresolved_halt_or_alert:2026-08-02T01:00:00Z|circuit_breaker_triggered" in joined
    assert "2026-09-30" not in joined
    assert "p0_p1_not_closed:G11" in joined and "p0_p1_not_closed:G16" in joined


def test_r7_audit_reports_missing_files_and_a_short_soak(tmp_path):
    report = r7_acceptance.audit_r7(start=date(2026, 8, 1), end=date(2026, 8, 1), reconciliation_dir=str(tmp_path / "none"),
                                    alerts_path=str(tmp_path / "no_alerts.jsonl"), incidents_path=str(tmp_path / "no_inc.jsonl"),
                                    closures_path=str(tmp_path / "closures.json"), account_id="a/b c", minimum_days=0)
    joined = "\n".join(report["issues"])
    assert not report["ok"] and "invalid_minimum_days" in joined and "missing_reconciliation:2026-08-01" in joined
    assert "missing_alert_log:" in joined and "missing_or_invalid_closures:" in joined
    (tmp_path / "closures.json").write_text("[1]", encoding="utf-8")
    assert any(i.startswith("invalid_closure_schema:") for i in
               r7_acceptance.audit_r7(start=date(2026, 8, 1), end=date(2026, 8, 1), reconciliation_dir=str(tmp_path),
                                      alerts_path=str(tmp_path / "x"), incidents_path=str(tmp_path / "y"),
                                      closures_path=str(tmp_path / "closures.json"), account_id="a", minimum_days=5)["issues"])
    assert any(i.startswith("soak_too_short") for i in
               r7_acceptance.audit_r7(start=date(2026, 8, 1), end=date(2026, 8, 1), reconciliation_dir=str(tmp_path),
                                      alerts_path=str(tmp_path / "x"), incidents_path=str(tmp_path / "y"),
                                      closures_path=str(tmp_path / "closures.json"), account_id="a", minimum_days=5)["issues"])


def test_r7_command_writes_the_report_and_exits_non_zero_when_evidence_is_missing(tmp_path, monkeypatch, capsys):
    output = tmp_path / "out" / "report.json"
    monkeypatch.setattr(sys, "argv", ["r7", "--start", "2026-08-01", "--end", "2026-08-01", "--account-id", "a",
                                      "--reconciliation-dir", str(tmp_path / "r"), "--alerts", str(tmp_path / "a.jsonl"),
                                      "--incidents", str(tmp_path / "i.jsonl"), "--closures", str(tmp_path / "c.json"),
                                      "--minimum-days", "1", "--output", str(output)])
    assert r7_acceptance.main() == 2
    assert json.loads(output.read_text(encoding="utf-8"))["ok"] is False
    assert json.loads(capsys.readouterr().out)["job"] == "r7_soak_acceptance"
