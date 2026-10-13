"""Fail-closed behavior of the Phase 6 evaluator command and session-risk recovery in the governor (C5)."""
from __future__ import annotations

import json
import sys

import pytest

from core.risk.portfolio_governor import PortfolioRiskGovernor
from ops import admission_gates


# ---- Phase 6 evaluator command: anything unreadable is a failed, recorded evaluation --------------------------------

def _run(monkeypatch, capsys, tmp_path, bundle_text=None, *, missing=False):
    source = tmp_path / "bundle.json"
    if not missing:
        source.write_text(bundle_text, encoding="utf-8")
    output = tmp_path / "out" / "report.json"
    monkeypatch.setattr(sys, "argv", ["gates", "--input", str(source), "--output", str(output)])
    code = admission_gates.main()
    printed = json.loads(capsys.readouterr().out)
    return code, printed, json.loads(output.read_text(encoding="utf-8"))


@pytest.mark.parametrize("text, missing, label", [
    ("{not json", False, "JSONDecodeError"),
    ("[1, 2, 3]", False, "TypeError"),
    (None, True, "FileNotFoundError"),
])
def test_unreadable_evidence_produces_a_failed_report_with_the_reason(monkeypatch, capsys, tmp_path, text, missing, label):
    code, printed, saved = _run(monkeypatch, capsys, tmp_path, text, missing=missing)
    assert code == 2 and printed == saved
    assert saved["passed"] is False and saved["admission_passed"] is False
    assert saved["issues"][0].startswith(f"invalid_evidence_bundle:{label}")


def test_an_empty_bundle_does_not_pass_and_the_report_is_written_atomically(monkeypatch, capsys, tmp_path):
    code, printed, saved = _run(monkeypatch, capsys, tmp_path, "{}")
    assert code == 2 and saved["passed"] is False and saved["admission_passed"] is False
    assert list(saved["tasks"]) == [f"T-6.{n}" for n in range(1, 9)]
    assert not list((tmp_path / "out").glob("*.tmp"))
    assert printed["phase"] == "Phase 6"


# ---- PortfolioRiskGovernor.begin_session -----------------------------------------------------------------------

class _StateStore:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


class _OrderStore:
    def __init__(self, rows):
        self.rows = rows

    def list_all(self):
        return self.rows


def _order(side="buy", *, bar_time="2026-10-01T00:01:00", timeframe="1m", ref=100.0, stop=98.0, qty=3.0):
    return {"side": side, "requested_qty": qty,
            "intent": {"bar_time": bar_time, "timeframe": timeframe, "reference_price": ref, "initial_stop": stop}}


def test_begin_session_resets_risk_only_when_the_session_changes():
    governor = PortfolioRiskGovernor()
    governor.begin_session("s1")
    governor._session_risk = 5.0
    governor.begin_session("s1")
    assert governor.session_risk_used == 5.0
    governor.begin_session("s2")
    assert governor.session_risk_used == 0.0


def test_session_risk_is_restored_from_the_state_store():
    governor = PortfolioRiskGovernor()
    governor.bind_state_store(_StateStore({"session_risk:s1": "4.5"}))
    governor.begin_session("s1")
    assert governor.session_risk_used == 4.5
    governor.begin_session("s2")                                         # nothing stored for another session
    assert governor.session_risk_used == 0.0


def test_open_entry_orders_of_the_same_bar_count_as_already_used_risk():
    orders = _OrderStore([
        _order(),                                                         # risk (100-98)*3 = 6, belongs to this session
        _order("short", ref=50.0, stop=51.0, qty=2.0),                    # risk 2, same session
        _order("sell"),                                                   # exits do not consume entry risk
        _order(bar_time="2026-10-01T00:05:00"),                           # a different bar
        _order(bar_time="2026-10-01T00:01:00+00:00"),                     # aware timestamp is compared in UTC: also this bar, risk 6
        {"side": "buy", "requested_qty": 1.0, "intent": {"bar_time": "2026-10-01T00:01:00"}},   # malformed: skipped
        {"side": "buy", "requested_qty": 1.0},                            # no intent at all: skipped
    ])
    governor = PortfolioRiskGovernor()
    governor.bind_state_store(_StateStore({}), orders)
    governor.begin_session("2026-10-01T00:00:00")
    assert governor.session_risk_used == pytest.approx(14.0)


def test_the_larger_of_stored_and_reserved_risk_wins_and_no_session_means_no_reservation_scan():
    orders = _OrderStore([_order()])
    governor = PortfolioRiskGovernor()
    governor.bind_state_store(_StateStore({"session_risk:2026-10-01T00:00:00": 10.0}), orders)
    governor.begin_session("2026-10-01T00:00:00")
    assert governor.session_risk_used == 10.0                             # stored 10 > reserved 6
    fresh = PortfolioRiskGovernor()
    fresh.bind_state_store(_StateStore({}), _OrderStore([_order()]))
    fresh.begin_session(None)                                             # None is the initial session: nothing to start or scan
    assert fresh.session_risk_used == 0.0
