from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pandas as pd
import pytest

from analysis.strategy_review import freeze_prospective
from scripts.prepare_strategy_forward import prepare
from scripts.collect_strategy_forward import collect


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config/params.yaml").write_text("strategy_governance: paused_revalidation\n")
    (root / "main.py").write_text("# frozen strategy\n")
    old = tmp_path / "old"
    old.mkdir()
    registry = old / "review_protocol.json"
    registry.write_text(json.dumps({"public_data": {"symbols": ["BTC/USDT", "ETH/USDT"]}}))
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    freeze_prospective(old / "prospective_protocol.json", code_hash="old-code",
        config_hash=digest(root / "config/params.yaml"), registry_hash=digest(registry), frozen_at="2026-09-19T00:00:00Z")
    monkeypatch.setattr("scripts.register_strategy_successor._utc_now", lambda: datetime(2026, 10, 4, tzinfo=timezone.utc))
    batch = tmp_path / "new"
    previous = (old / "prospective_protocol.json").read_bytes()
    result = prepare(historical_batch=old, output=batch, source_root=root)
    assert (old / "prospective_protocol.json").read_bytes() == previous
    assert not result["new_observation_inherits_elapsed_days"]
    return root, batch, result


class PublicCandles:
    def __init__(self):
        self.calls = []
        self.mutation = None

    def fetch(self, params, timeout):
        self.calls.append(deepcopy(params))
        rows = [[t, "100", "102", "99", "101", "1000", t+86_400_000-1, "100000", 100, "1", "1"]
                for t in range(params["startTime"], params["endTime"]+1, 86_400_000)]
        if self.mutation == "missing":
            return rows[:-1]
        if self.mutation == "short":
            rows[0][6] -= 1000
        if self.mutation == "duplicate":
            rows.append(rows[-1])
        if self.mutation == "outside":
            rows[0][0] -= 86_400_000
        if self.mutation == "nan":
            rows[0][1] = "NaN"
        return rows


def clock(monkeypatch, value):
    monkeypatch.setattr("scripts.collect_strategy_forward._now", lambda: value)


def test_registration_starts_fresh_clock_and_does_not_call_network_early(candidate, monkeypatch):
    root, batch, receipt = candidate
    assert receipt["observation_boundary"] == "2026-10-05T00:00:00+00:00"
    assert receipt["test_start"] == "2026-11-04T00:00:00+00:00"
    clock(monkeypatch, "2026-10-04T12:00:00+00:00")
    client = PublicCandles()
    result = collect(batch, source_root=root, client=client)
    assert result["status"] == "awaiting_closed_forward_bars"
    assert not client.calls and result["requests"] == 0
    assert not result["strategy_evaluated"]
    assert (batch / "forward_capture" / result["output"].split("\\")[-1] / "receipt.json").is_file()


def test_closed_forward_candles_have_receipt_time_and_resume_does_not_refetch(candidate, monkeypatch):
    root, batch, _ = candidate
    clock(monkeypatch, "2026-10-06T00:01:00+00:00")
    client = PublicCandles()
    first = collect(batch, source_root=root, client=client)
    assert first["status"] == "complete" and first["requests"] == 2
    assert all(c["received"] == 1 for c in first["coverage"].values())
    from core.data_versions import DataVersionStore
    store = DataVersionStore(batch / "intake_snapshots")
    records = store.read_snapshot(first["record_snapshot"])["records"]
    assert all(r["available_at"] > r["event_time"] for r in records)
    assert all("2026-10-06T00:01:00" in r["available_at"] for r in records)
    second = collect(batch, source_root=root, client=client)
    assert second["status"] == "awaiting_closed_forward_bars" and len(client.calls) == 2
    assert second["prior_snapshots"] == [first["snapshot_id"]]


@pytest.mark.parametrize("kind", ["missing", "short", "duplicate", "outside", "nan"])
def test_missing_or_invalid_public_data_cannot_be_complete(candidate, monkeypatch, kind):
    root, batch, _ = candidate
    clock(monkeypatch, "2026-10-06T00:01:00+00:00")
    client = PublicCandles()
    client.mutation = kind
    result = collect(batch, source_root=root, client=client)
    assert result["status"] in {"incomplete_coverage", "failed"}
    assert not result["strategy_evaluated"]


@pytest.mark.parametrize("target", ["source", "protocol", "contract", "previous_session", "opened", "lock"])
def test_resume_refuses_mutated_or_opened_evidence_before_network(candidate, monkeypatch, target):
    root, batch, _ = candidate
    clock(monkeypatch, "2026-10-06T00:01:00+00:00")
    client = PublicCandles()
    first = collect(batch, source_root=root, client=client)
    count = len(client.calls)
    if target == "source":
        (root / "main.py").write_text("# changed\n")
    elif target == "protocol":
        (batch / "prospective_protocol.json").write_text("{}")
    elif target == "contract":
        (batch / "intake_contract.json").write_text("{}")
    elif target == "previous_session":
        from pathlib import Path
        (Path(first["output"]) / "summary.json").write_text("{}")
    elif target == "opened":
        (batch / "prospective_protocol.json.opened").write_text("claimed")
    else:
        (batch / "forward_capture.lock").write_text("another writer")
    with pytest.raises((ValueError, PermissionError, FileExistsError)):
        collect(batch, source_root=root, client=client)
    assert len(client.calls) == count


def test_test_period_capture_does_not_open_or_disclose_strategy_results(candidate, monkeypatch):
    root, batch, _ = candidate
    clock(monkeypatch, "2026-11-05T00:01:00+00:00")
    result = collect(batch, source_root=root, client=PublicCandles())
    assert result["phase"] == "test_capture" and result["status"] == "complete"
    assert not result["strategy_evaluated"] and not result["performance_disclosed"]
    assert not (batch / "prospective_protocol.json.opened").exists()
    assert pd.Timestamp(result["stop_exclusive"]) == pd.Timestamp("2026-11-05T00:00:00Z")


def test_missing_last_session_cannot_silently_restart_history(candidate, monkeypatch):
    from pathlib import Path
    root, batch, _ = candidate
    clock(monkeypatch, "2026-10-06T00:01:00+00:00")
    client = PublicCandles()
    collect(batch, source_root=root, client=client)
    calls = len(client.calls)
    original = Path.iterdir
    # Simulate an incomplete restore, without deleting any evidence.
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(()) if self == batch / "forward_capture" else original(self))
    with pytest.raises(ValueError, match="missing from disk"):
        collect(batch, source_root=root, client=client)
    assert len(client.calls) == calls
