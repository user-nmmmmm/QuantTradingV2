"""B6 (ADR 0029): the account-sync state is owned by ``AccountSync``; ``LiveBroker`` keeps the names.

The golden rows were recorded on the code BEFORE the state moved: after every step of the scripted run the eight state
attributes must be identical (including "not bound yet" for the ones that callers probe with ``getattr``), and every
old attribute on ``LiveBroker`` must still read and write the same value.
"""
from __future__ import annotations

from tests import test_account_source_integration as _source

NOW = _source.NOW
configure = _source.configure
account = _source.account  # the fixture, re-exported so pytest finds it in this module

_ABSENT = "<absent>"


def _get(broker, name):
    return getattr(broker, name, _ABSENT)


def _snap(broker, step):
    report = _get(broker, "account_reconciliation_report")
    snapshot = _get(broker, "account_snapshot")
    facts = _get(broker, "account_balance_facts")
    source = _get(broker, "_account_source")
    return {
        "step": step,
        "source": _ABSENT if source is _ABSENT else (source is not None),
        "environment": _get(broker, "_account_source_environment"),
        "maximum_age": _get(broker, "_account_source_maximum_age"),
        "report": report if report is _ABSENT else {"ok": report["ok"], "issues": report["issues"],
                                                    "allows_new_risk": report["allows_new_risk"]},
        "unowned": dict(broker.unowned_positions),
        "last_sync": _get(broker, "last_account_sync_at"),
        "snapshot": snapshot if snapshot is _ABSENT else sorted(snapshot),
        "facts": facts if facts is _ABSENT else sorted(facts),
    }


def _run(account, tmp_path):
    broker, venue, data = account
    rows = [_snap(broker, "fresh")]
    configure(broker, data, tmp_path)
    rows.append(_snap(broker, "configured"))
    broker.sync()
    rows.append(_snap(broker, "synced"))
    venue.fetch_balance.return_value["free"]["USDT"] += 1
    venue.fetch_balance.return_value["total"]["USDT"] += 1
    broker.sync()
    rows.append(_snap(broker, "balance drifted"))
    return rows


SNAPSHOT_KEYS = ['account', 'base_currency', 'capital_bridge_status', 'cash', 'exchange', 'market_type', 'observed_at', 'positions', 'raw_balance', 'schema_version', 'source']


GOLDEN = [
    {"step": "fresh", "source": _ABSENT, "environment": _ABSENT, "maximum_age": _ABSENT, "report": _ABSENT,
     "unowned": {}, "last_sync": None, "snapshot": _ABSENT, "facts": _ABSENT},
    {"step": "configured", "source": True, "environment": "sandbox", "maximum_age": 90, "report": {"ok": False, "issues": ["account_source_not_checked"], "allows_new_risk": False},
     "unowned": {}, "last_sync": None, "snapshot": _ABSENT, "facts": _ABSENT},
    {"step": "synced", "source": True, "environment": "sandbox", "maximum_age": 90, "report": {"ok": True, "issues": [], "allows_new_risk": False},
     "unowned": {}, "last_sync": NOW, "snapshot": SNAPSHOT_KEYS, "facts": _ABSENT},
    {"step": "balance drifted", "source": True, "environment": "sandbox", "maximum_age": 90, "report": {"ok": False, "issues": ["observed_account_cash_differs_from_export"], "allows_new_risk": False},
     "unowned": {}, "last_sync": NOW, "snapshot": SNAPSHOT_KEYS, "facts": _ABSENT},
]


def test_account_sync_golden_run(account, tmp_path):
    assert _run(account, tmp_path) == GOLDEN


def test_the_old_names_read_and_write_through_the_broker(account):
    broker, _, _ = account
    assert broker.unowned_positions == {} and broker.last_account_sync_at is None
    assert getattr(broker, "_account_source", None) is None
    assert not broker.account_source_configured
    for name, value in (("_account_source", object()), ("_account_source_environment", "x"), ("_account_source_maximum_age", 7.0),
                        ("account_reconciliation_report", {"ok": False}), ("account_snapshot", {"a": 1}),
                        ("account_balance_facts", {"USDT": 1}), ("unowned_positions", {"S": {}}), ("last_account_sync_at", NOW)):
        setattr(broker, name, value)
        assert getattr(broker, name) is value
    assert broker.account_source_configured


def test_the_collaborator_is_where_the_state_lives(account, tmp_path):
    broker, _, data = account
    configure(broker, data, tmp_path)
    broker.sync()
    sync = broker._account_sync
    names = ("_account_source", "_account_source_environment", "_account_source_maximum_age", "account_reconciliation_report",
             "account_snapshot", "unowned_positions", "last_account_sync_at")
    for name in names:
        assert getattr(broker, name) is getattr(sync, name)
    assert not set(names + ("account_balance_facts",)) & set(vars(broker))
