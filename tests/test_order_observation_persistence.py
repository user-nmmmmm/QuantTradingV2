import json
import sqlite3
import time

import pytest

from core.order_latency import (OrderLatencyRecorder, order_observation_sidecar_path,
                                read_order_observations)


def record(recorder, identity="client1"):
    return recorder.call("submit", lambda: {"id": "venue1", "status": "open", "filled": 0.,
        "secret": "not-for-storage", "info": {"apiKey": "never-record"}}, context={
        "client_order_id": identity, "account_id": "account1", "symbol": "BTC/USDT",
        "secret": "secret-request-value"})


def test_restart_recovers_recent_window_total_and_stable_ids(tmp_path):
    path = tmp_path/"observations.sqlite3"
    first = OrderLatencyRecorder(max_records=2, persistence_path=path)
    for i in range(5):
        record(first, str(i))
    before = read_order_observations(path)
    restored = OrderLatencyRecorder(max_records=2, persistence_path=path)
    summary = restored.summary()
    assert summary["total_records"] == 5
    assert summary["window_records"] == 2
    assert summary["window_evicted_records"] == 3
    assert summary["unrecoverable_records"] == summary["dropped_records"] == 0
    assert [r["observation_id"] for r in summary["records"]] == [r["observation_id"] for r in before[-2:]]
    record(restored, "new")
    assert len(restored.export_records()) == 6
    assert [r["sequence"] for r in restored.export_records()] == [1, 2, 3, 4, 5, 6]
    assert len({r["observation_id"] for r in restored.export_records()}) == 6


def test_append_only_all_records_and_whitelist(tmp_path):
    path = tmp_path/"observations.sqlite3"
    recorder = OrderLatencyRecorder(max_records=1, persistence_path=path)
    for _ in range(4):
        record(recorder)
    rows = read_order_observations(path)
    assert len(rows) == 4
    assert len(read_order_observations(path, after_sequence=2, limit=1)) == 1
    serialized = json.dumps(rows)
    assert "secret" not in serialized and "apiKey" not in serialized and "never-record" not in serialized
    assert all(row["client_order_id"] == "client1" for row in rows)
    assert recorder.summary()["persistence"]["persisted_records"] == 4


def test_persistence_failure_does_not_mask_ack_or_original_exception(tmp_path, monkeypatch):
    recorder = OrderLatencyRecorder(persistence_path=tmp_path/"observations.sqlite3")
    def fail(_row):
        raise sqlite3.OperationalError("sensitive detail must not be exported")
    monkeypatch.setattr(recorder, "_persist", fail)
    result = {"status": "open", "id": "accepted"}
    calls = []
    def accepted():
        calls.append(1)
        return result
    assert recorder.call("submit", accepted) is result
    original = RuntimeError("original exchange error")
    def rejected():
        calls.append(2)
        raise original
    with pytest.raises(RuntimeError) as caught:
        recorder.call("submit", rejected)
    assert caught.value is original
    assert calls == [1, 2]
    summary = recorder.summary()
    assert summary["persistence"]["write_failures"] == 2
    assert summary["persistence"]["status"] == "degraded"
    assert "sensitive detail" not in json.dumps(summary)
    assert len(recorder.export_records()) == 2


def test_sqlite_lock_is_bounded_and_call_time_excludes_persistence(tmp_path):
    path = tmp_path/"observations.sqlite3"
    ticks = iter([1., 1.02])
    recorder = OrderLatencyRecorder(persistence_path=path, lock_timeout_seconds=.02, clock=lambda: next(ticks))
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        result = record(recorder)
        elapsed = time.monotonic()-started
    finally:
        connection.rollback()
        connection.close()
    assert result["status"] == "open"
    assert elapsed < 1.
    summary = recorder.summary()
    assert summary["records"][0]["duration_seconds"] == pytest.approx(.02)
    assert summary["persistence"]["write_failures"] == 1
    assert not read_order_observations(path)


def test_bad_sidecar_and_bad_payload_cannot_change_order_result(tmp_path):
    path = tmp_path/"broken.sqlite3"
    path.write_text("not sqlite", encoding="utf-8")
    recorder = OrderLatencyRecorder(persistence_path=path)
    result = {"id": "accepted", "status": "open", "timestamp": float("nan"), "filled": object()}
    assert recorder.call("submit", lambda: result) is result
    summary = recorder.summary()
    assert summary["persistence"]["restore_failures"] == 1
    assert summary["persistence"]["write_failures"] == 1
    assert "filled" not in summary["records"][0]


def test_memory_mode_and_true_unrecoverable_loss_are_distinguished():
    assert order_observation_sidecar_path(":memory:") is None
    assert order_observation_sidecar_path("orders.db").endswith("orders.db.order_observations.sqlite3")
    recorder = OrderLatencyRecorder(max_records=1)
    record(recorder)
    record(recorder)
    summary = recorder.summary()
    assert summary["window_evicted_records"] == 1
    assert summary["unrecoverable_records"] == 1
    assert not summary["persistence"]["enabled"]


def test_recorder_internal_error_cannot_mask_exchange_result(tmp_path, monkeypatch):
    recorder = OrderLatencyRecorder(persistence_path=tmp_path/"observations.sqlite3")
    def broken(_row):
        raise ValueError("broken telemetry")
    monkeypatch.setattr(recorder, "_record", broken)
    result = {"id": "already-accepted"}
    assert recorder.call("submit", lambda: result) is result
    assert recorder.summary()["persistence"]["telemetry_failures"] == 1


def test_receive_clock_failure_keeps_ack_observation(tmp_path):
    ticks = iter([1.])
    path = tmp_path/"observations.sqlite3"
    recorder = OrderLatencyRecorder(persistence_path=path, clock=lambda: next(ticks))
    assert record(recorder)["status"] == "open"
    rows = read_order_observations(path)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "ack"
    assert "duration_seconds" not in rows[0]
    assert recorder.summary()["persistence"]["telemetry_failures"] == 1
