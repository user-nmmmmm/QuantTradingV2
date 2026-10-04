"""Prospective registration precedes requests; finite sessions retain evidence."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import pytest

from core.data_versions import DataVersionStore
from core.quote_observations import QuoteObservationStore, read_quote_observations
from scripts import start_paper_observation as module


@pytest.fixture
def source_root(tmp_path):
    source = tmp_path / "source"
    for name in module.OBSERVATION_SOURCES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("frozen source: " + name, encoding="utf-8")
    return source


def _args(argv):
    parser = argparse.ArgumentParser()
    for name in ("exchange", "market-type", "environment", "duration-seconds", "output",
                 "resume-store", "interval-seconds", "timeout-seconds", "stale-after-seconds"):
        parser.add_argument("--" + name)
    parser.add_argument("--symbols", nargs="+")
    return parser.parse_args(argv)


def fake_collector(*, inspect=None, emit=True, unknown_time=False, failure=False,
                   lifecycle="stopped", mutate_source=None, backfill=False, drop_symbol=False):
    def collect(argv):
        args = _args(argv)
        folder, database = Path(args.output), Path(args.resume_store)
        root, session = database.parent, folder.parent
        # This callback represents the first possible network call.
        ref = module._read(session / "registration_ref.json")
        sealed = DataVersionStore(root / "snapshots")
        sealed.verify_snapshot(ref["snapshot_id"])
        registered = module._read(session / "registration" / "registration.json")
        assert registered["parameters"]["duration_seconds"] == float(args.duration_seconds)
        assert registered["parameters"]["public_read_only"]
        if inspect:
            inspect(root, session, registered)
        folder.mkdir(parents=True, exist_ok=False)
        store = QuoteObservationStore(database)
        previous = store.read_all()
        starting_sequence = previous[-1]["sequence"] if previous else 0
        collector_id = "fake-public-" + uuid4().hex
        manifest = {"starting_sequence": starting_sequence, "collector_run_id": collector_id,
            "orders_submitted": 0, "credentials_used": False, "public_read_only": True,
            "exchange_id": args.exchange, "symbols": args.symbols, "market_type": args.market_type,
            "source": args.environment, "quote_store": str(database.resolve()), "started_at": module._now(),
            "duration_seconds": float(args.duration_seconds), "interval_seconds": float(args.interval_seconds),
            "timeout_seconds": float(args.timeout_seconds), "stale_after_seconds": float(args.stale_after_seconds)}
        module._write(folder / "manifest.json", manifest)
        samples = []
        if emit:
            symbols = args.symbols[:-1] if drop_symbol else args.symbols
            for symbol in symbols:
                at = module._now()
                quote = {"quote_id": uuid4().hex, "exchange_id": args.exchange,
                    "environment": args.environment, "market_type": args.market_type,
                    "symbol": symbol, "quote_currency": symbol.split("/")[1],
                    "bid": 99., "ask": 101., "bid_quantity": 1., "ask_quantity": 1.,
                    "occurred_at": None if unknown_time else at,
                    "occurred_at_status": "unknown" if unknown_time else "exchange_timestamp",
                    "request_started_at": "2000-01-01T00:00:00Z" if backfill else at,
                    "observed_at": at, "available_at": at,
                    "source_id": args.exchange + ":public_rest_order_book",
                    "collector_run_id": collector_id, "independent": True,
                    "observation_kind": "public_order_book"}
                samples.append(store.append(quote))
        if failure:
            raise RuntimeError("secret must never be copied to a failure report")
        if mutate_source:
            mutate_source()
        module._write(folder / "quote_observations.json", samples)
        module._write(folder / "sampler_health.json", {
            "status": "ok" if emit else "degraded", "lifecycle": lifecycle})
        summary = {"status": "observed_quotes" if samples else "unavailable", "fills": 0,
            "real_venue_calibration": False, "observations": len(samples)}
        module._write(folder / "summary.json", summary)
        module._write(folder / "calibration_without_fills.json", {
            "status": "insufficient", "real_venue_calibration": False})
        return summary
    return collect


def start(tmp_path, source_root, collector=None, **kwargs):
    return module.run_observation(output=tmp_path / "observations", duration_seconds=.001,
        source_root=source_root, collector=collector or fake_collector(), **kwargs)


def test_registration_is_sealed_before_requests_and_unknown_venue_time_stays_raw(tmp_path, source_root):
    seen = []
    def inspect(root, session, registered):
        assert not read_quote_observations(root / "execution_quotes.sqlite3")
        protocol = module._read(root / "protocol.json")
        assert module.aware_utc(protocol["registered_at"]) <= module.aware_utc(registered["registered_at"])
        assert DataVersionStore(root / "snapshots").verify_snapshot(
            module._read(root / "protocol_ref.json")["snapshot_id"])
        seen.append(registered["registered_at"])
    result = start(tmp_path, source_root, fake_collector(inspect=inspect, unknown_time=True))
    assert seen and result["status"] == "completed" and result["observations"] == 2
    assert result["unknown_exchange_timestamps"] == 2
    assert not result["real_venue_calibration"] and not result["independent_holdout"]
    session = Path(result["session"])
    quotes = module._read(session / "captured_quotes.json")
    assert all(row["occurred_at"] is None for row in quotes)
    store = DataVersionStore(tmp_path / "observations" / "snapshots")
    frozen = store.verify_snapshot(result["snapshot_id"])
    assert frozen["kind"] == "files" and not frozen["availability"]["point_in_time_complete"]
    assert all("sqlite" not in name for name in frozen["files"])
    events = [json.loads(line) for line in (session / "registration" / "attempts.jsonl").read_text().splitlines()]
    assert [e["status"] for e in events if e.get("phase") == "collection"] == ["started", "completed"]


def test_resume_has_new_registration_and_preserves_prior_sequence_and_artifacts(tmp_path, source_root):
    first = start(tmp_path, source_root)
    session = Path(first["session"])
    original = {p: p.read_bytes() for p in session.rglob("*") if p.is_file()}
    second = module.run_observation(resume=tmp_path / "observations", duration_seconds=.001,
        source_root=source_root, collector=fake_collector())
    assert second["status"] == "completed"
    assert (first["starting_sequence"], first["last_sequence"], second["starting_sequence"], second["last_sequence"]) == (0, 2, 2, 4)
    assert first["session_id"] != second["session_id"]
    assert all(path.read_bytes() == content for path, content in original.items())
    registration = module._read(Path(second["session"]) / "registration" / "registration.json")
    assert registration["parameters"]["previous_session_snapshots"] == [first["snapshot_id"]]
    assert len(read_quote_observations(tmp_path / "observations" / "execution_quotes.sqlite3")) == 4


@pytest.mark.parametrize("target", ["protocol", "artifact", "source", "configuration", "extra_quote"])
def test_resume_rejects_tampered_identity_or_unsealed_history_before_network(tmp_path, source_root, target):
    first = start(tmp_path, source_root)
    root = tmp_path / "observations"
    options = {}
    if target == "protocol":
        path = root / "protocol.json"
        path.write_text(path.read_text().replace('"binance"', '"okx"'), encoding="utf-8")
    elif target == "artifact":
        path = Path(first["session"]) / "captured_quotes.json"
        path.write_text("[]", encoding="utf-8")
    elif target == "source":
        (source_root / module.OBSERVATION_SOURCES[0]).write_text("changed", encoding="utf-8")
    elif target == "configuration":
        options["interval_seconds"] = 2.
    else:
        database = root / "execution_quotes.sqlite3"
        row = deepcopy(read_quote_observations(database)[0])
        row["quote_id"] = "unregistered-quote"
        QuoteObservationStore(database).append(row)
    called = []
    with pytest.raises(ValueError):
        module.run_observation(resume=root, duration_seconds=.001, source_root=source_root,
            collector=lambda argv: called.append(argv), **options)
    assert called == []


@pytest.mark.parametrize("collector,status", [
    (fake_collector(emit=False), "insufficient"),
    (fake_collector(drop_symbol=True), "insufficient"),
    (fake_collector(lifecycle="stop_pending"), "stop_pending"),
    (fake_collector(backfill=True), "failed"),
    (fake_collector(failure=True), "failed"),
])
def test_missing_quotes_failed_calls_and_backfill_never_claim_completed(tmp_path, source_root, collector, status):
    result = start(tmp_path, source_root, collector)
    assert result["status"] == status
    assert result["orders_submitted"] == 0 and not result["production_approved"]
    assert (Path(result["session"]) / "receipt.json").is_file()
    frozen = DataVersionStore(tmp_path / "observations" / "snapshots").verify_snapshot(result["snapshot_id"])
    assert frozen["metadata"]["status"] == status
    assert "secret" not in json.dumps(result)
    if status == "stop_pending":
        with pytest.raises(ValueError, match="termination"):
            module.run_observation(resume=tmp_path / "observations", duration_seconds=.001,
                source_root=source_root, collector=fake_collector())


def test_source_change_during_collection_is_sealed_as_failure(tmp_path, source_root):
    collector = fake_collector(mutate_source=lambda: (source_root / module.OBSERVATION_SOURCES[0]).write_text("new version"))
    result = start(tmp_path, source_root, collector)
    assert result["status"] == "failed" and result["observations"] == 2
    assert result["identity"]["identity_changed"] and not result["identity"]["source_unchanged"]


def test_early_clean_stop_does_not_claim_full_registered_duration(tmp_path, source_root):
    result = module.run_observation(output=tmp_path / "observations", duration_seconds=30.,
        source_root=source_root, collector=fake_collector())
    assert result["status"] == "incomplete_duration" and result["observations"] == 2
    assert result["elapsed_seconds"] < result["duration_seconds"]


def test_failed_collector_preserves_persisted_quotes_for_verified_next_session(tmp_path, source_root):
    first = start(tmp_path, source_root, fake_collector(failure=True))
    assert first["status"] == "failed" and first["observations"] == 2
    second = module.run_observation(resume=tmp_path / "observations", duration_seconds=.001,
        source_root=source_root, collector=fake_collector())
    assert second["status"] == "completed" and second["starting_sequence"] == 2


def test_cli_requires_explicit_duration_and_existing_output_is_not_overwritten(tmp_path, source_root):
    with pytest.raises(SystemExit):
        module.main(["--output", str(tmp_path / "not-created")])
    assert not (tmp_path / "not-created").exists()
    start(tmp_path, source_root)
    with pytest.raises(FileExistsError):
        start(tmp_path, source_root)


def test_only_one_session_writer_can_enter_and_old_lock_is_preserved(tmp_path, source_root):
    start(tmp_path, source_root)
    lock = tmp_path / "observations" / "active_session.lock"
    lock.write_text("existing observation owner", encoding="utf-8")
    with pytest.raises(FileExistsError):
        module.run_observation(resume=lock.parent, duration_seconds=.001,
            source_root=source_root, collector=fake_collector())
    assert lock.read_text() == "existing observation owner"


def test_real_collection_entrypoint_works_with_an_injected_public_fetcher(tmp_path, source_root, monkeypatch):
    from scripts import collect_execution_quotes
    from core.quote_observations import BackgroundQuoteSampler, utc_now
    calls = []
    def fetch(symbol):
        calls.append(symbol)
        return {"bids": [[99., 2.]], "asks": [[101., 3.]],
                "timestamp": int(utc_now().timestamp() * 1000)}
    monkeypatch.setattr(collect_execution_quotes, "BackgroundQuoteSampler",
                        lambda **kwargs: BackgroundQuoteSampler(fetcher=fetch, **kwargs))
    result = module.run_observation(output=tmp_path / "observations", duration_seconds=.04,
        interval_seconds=.01, source_root=source_root, collector=collect_execution_quotes.main)
    assert result["status"] == "completed" and set(calls) == {"BTC/USDT", "ETH/USDT"}
    assert result["observations"] >= 2 and result["elapsed_seconds"] >= .04
    assert result["orders_submitted"] == 0 and not result["real_venue_calibration"]
