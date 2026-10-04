import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import aiohttp
import pytest

from core.binance_depth_observer import (DepthGap, DepthJournal, SequencedDepthBook,
    displayed_depth_sweep, observe_depth, verify_depth_journal)
from core.quote_observations import QuoteObservationStore


AT = "2020-01-01T00:00:01+00:00"
MS = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def diff(first=100, last=101, **changes):
    return {"e": "depthUpdate", "s": "BTCUSDT", "E": MS,
        "U": first, "u": last, "b": [["99", "2"]], "a": [["101", "3"]], **changes}


def snapshot(last=100):
    return {"lastUpdateId": last, "bids": [["99", "1"], ["98", "2"]], "asks": [["101", "1"], ["102", "2"]]}


def ready():
    book = SequencedDepthBook("BTC/USDT")
    book.install_snapshot(snapshot(), observed_at=AT)
    return book


def test_snapshot_alone_never_has_fabricated_exchange_time():
    book = ready()
    assert not book.synced and book.last_event_at is None
    row = book.apply(diff(), observed_at=AT, available_at=AT)
    assert row["last_update_id"] == 101 and row["sequence_verified"]
    assert row["occurred_at"] == "2020-01-01T00:00:00+00:00"
    assert row["bids"][0] == ["99", "2"]
    assert book.apply(diff(), observed_at=AT, available_at=AT) is None


def test_gap_resets_and_requires_another_snapshot():
    book = ready()
    book.apply(diff(), observed_at=AT, available_at=AT)
    with pytest.raises(DepthGap):
        book.apply(diff(103, 104), observed_at=AT, available_at=AT)
    assert not book.synced and book.last_id is None and book.bids == {}
    with pytest.raises(DepthGap):
        book.apply(diff(105, 105), observed_at=AT, available_at=AT)
    book.install_snapshot(snapshot(105), observed_at=AT)
    assert book.apply(diff(106, 106), observed_at=AT, available_at=AT)["last_update_id"] == 106


def test_zero_deletes_absolute_quantity_and_unknown_outer_depth_is_not_used():
    book = ready()
    row = book.apply(diff(b=[["99", "0"], ["97", "100"]]), observed_at=AT, available_at=AT)
    assert row["bids"] == [["98", "2"]]
    with pytest.raises(DepthGap):
        book.apply(diff(102, 102, b=[["98", "0"]]), observed_at=AT, available_at=AT)


@pytest.mark.parametrize("changes", [{"b": [["103", "1"]]}, {"U": 105}])
def test_future_clocks_crossed_books_invalid_sequences_fail_closed(changes):
    book = ready()
    with pytest.raises(ValueError):
        book.apply(diff(**changes), observed_at=AT, available_at=AT)


def test_exchange_ahead_of_local_clock_remains_raw_conflict_not_corrected():
    row = ready().apply(diff(E=MS + 32000), observed_at=AT, available_at=AT)
    assert row["occurred_at"] == "2020-01-01T00:00:32+00:00"
    assert row["observed_at"] == AT
    assert row["exchange_event_minus_local_receipt_seconds"] == 31.
    assert not row["clock_consistent"]


def test_ccxt_snapshot_decimal_string_identity_supported():
    book = SequencedDepthBook("BTC/USDT")
    book.install_snapshot({**snapshot(), "lastUpdateId": "100"}, observed_at=AT)
    assert book.apply(diff(), observed_at=AT, available_at=AT)["last_update_id"] == 101


def test_buffered_diff_cannot_be_available_before_rest_snapshot():
    book = ready()
    with pytest.raises(DepthGap):
        book.apply(diff(), observed_at="2020-01-01T00:00:00.500+00:00", available_at="2020-01-01T00:00:00.600+00:00")


def test_journal_tamper_and_capacity_limits(tmp_path):
    journal = DepthJournal(tmp_path / "evidence.jsonl")
    row = ready().apply(diff(), observed_at=AT, available_at=AT)
    journal.append(row)
    journal.close()
    assert verify_depth_journal(journal.path)[0]["last_update_id"] == 101
    small = displayed_depth_sweep(row, quote_notional=100., side="buy")
    large = displayed_depth_sweep(row, quote_notional=10000., side="buy")
    assert small["status"] == "displayed_depth_covers" and not small["realized_fill"]
    assert large["status"] == "beyond_observed_depth" and large["visible_book_vwap"] is None
    journal.path.write_text(journal.path.read_text().replace('"last_update_id": 101', '"last_update_id": 102'))
    with pytest.raises(ValueError):
        verify_depth_journal(journal.path)


class FakeSocket:
    def __init__(self, events):
        self.events = iter(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def receive(self):
        await asyncio.sleep(.005)
        event = next(self.events, None)
        if event is None:
            await asyncio.sleep(10.)
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(event))


class FakeSession:
    def __init__(self, streams):
        self.streams = iter(streams)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def ws_connect(self, url, **kwargs):
        assert url.startswith("wss://data-stream.binance.vision/ws/")
        assert kwargs["autoping"]
        return FakeSocket(next(self.streams))


def test_finite_fake_collection_records_snapshot_before_derived_bbo(tmp_path):
    journal = DepthJournal(tmp_path / "depth.jsonl")
    store = QuoteObservationStore(tmp_path / "quotes.sqlite3")
    session = FakeSession([[diff(), diff(102, 102), diff(103, 103)]])
    out = asyncio.run(observe_depth(symbols=["BTC/USDT"], duration_seconds=.15,
        journal=journal, quote_store=store, run_id="run", timeout_seconds=.1,
        snapshot_fetcher=lambda _: snapshot(), session_factory=lambda **_: session))
    journal.close()
    records = verify_depth_journal(journal.path)
    assert out["observations"] >= 2 and out["orders_submitted"] == 0
    kinds = [r["kind"] for r in records]
    assert kinds.index("raw_depth_diff") < kinds.index("raw_depth_snapshot") < kinds.index("synchronized_book")
    assert next(r for r in records if r["kind"] == "raw_depth_snapshot")["occurred_at"] is None
    assert all(q["occurred_at_status"] == "exchange_timestamp" for q in store.read_all())


def test_missing_snapshot_never_emits_quote(tmp_path):
    journal = DepthJournal(tmp_path / "depth.jsonl")
    store = QuoteObservationStore(tmp_path / "quotes.sqlite3")
    def fail(_):
        raise TimeoutError("test")
    out = asyncio.run(observe_depth(symbols=["BTC/USDT"], duration_seconds=.1,
        journal=journal, quote_store=store, run_id="run", timeout_seconds=.1, maximum_reconnects=0,
        snapshot_fetcher=fail, session_factory=lambda **_: FakeSession([[diff()]])))
    journal.close()
    assert out["observations"] == 0 and out["status"] == "incomplete"
    assert store.read_all() == []


def test_synchronized_future_clock_depth_is_saved_but_cannot_enter_quote_store(tmp_path):
    future = int(datetime.now(timezone.utc).timestamp() * 1000) + 60000
    journal = DepthJournal(tmp_path / "depth.jsonl")
    store = QuoteObservationStore(tmp_path / "quotes.sqlite3")
    out = asyncio.run(observe_depth(symbols=["BTC/USDT"], duration_seconds=.05,
        journal=journal, quote_store=store, run_id="run", timeout_seconds=.1, maximum_reconnects=0,
        snapshot_fetcher=lambda _: snapshot(),
        session_factory=lambda **_: FakeSession([[diff(E=future), diff(102, 102, E=future)]])))
    journal.close()
    assert out["observations"] > 0 and out["status"] == "clock_conflict"
    assert store.read_all() == []
    assert all(not row["clock_consistent"] for row in verify_depth_journal(journal.path) if row["kind"] == "synchronized_book")


def test_cli_registers_before_transport_and_empty_run_not_success(tmp_path, monkeypatch):
    from scripts import collect_execution_depth
    output = tmp_path / "new"
    async def collect(**kwargs):
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["orders_submitted"] == 0 and manifest["source_sha256"]
        return {"status": "incomplete", "lifecycle": "stopped"}
    monkeypatch.setattr(collect_execution_depth, "observe_depth", collect)
    out = collect_execution_depth.main(["--output", str(output), "--duration-seconds", "1"])
    assert out["status"] == "incomplete" and out["realized_fills"] == 0
    assert not out["real_venue_calibration"]
