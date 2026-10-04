"""Finite, anonymous Binance spot depth observation; no trading-runtime imports.

Uses the existing websocket_market transport pattern (aiohttp, automatic pong,
bounded receives and reconnects), but a separate sequence-checked depth state
machine. A kline repair cannot establish order-book sequence continuity.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.request import getproxies
from uuid import uuid4

from core.quote_observations import QuoteObservationStore, aware_utc, utc_now, anonymous_public_auth


class DepthGap(ValueError):
    """The entire book must be discarded and a new snapshot obtained."""


def _integer(value):
    # CCXT intentionally represents large JSON integers as decimal strings.
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("nonnegative integer update identity required")
    return value


def _levels(values, *, maximum=20000):
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError("bounded depth levels required")
    result = {}
    for level in values:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ValueError("price and absolute quantity required")
        try:
            price, qty = Decimal(str(level[0])), Decimal(str(level[1]))
        except InvalidOperation as exc:
            raise ValueError("invalid level") from exc
        if not price.is_finite() or not qty.is_finite() or price <= 0 or qty < 0 or price in result:
            raise ValueError("invalid or duplicate depth level")
        result[price] = qty
    return result


def _pairs(values):
    return [[str(price), str(qty)] for price, qty in values]


def normalize_diff(document, symbol):
    """Discard non-market fields; no arbitrary SDK payload is persisted."""
    if document.get("e") != "depthUpdate" or document.get("s") != symbol.replace("/", ""):
        raise ValueError("unexpected market depth stream")
    first, last, event_ms = (_integer(document[k]) for k in ("U", "u", "E"))
    if first > last or event_ms <= 0:
        raise ValueError("invalid event sequence or exchange clock")
    return {"e": "depthUpdate", "s": document["s"], "E": event_ms, "U": first, "u": last,
            "b": _pairs(_levels(document["b"]).items()), "a": _pairs(_levels(document["a"]).items())}


class SequencedDepthBook:
    def __init__(self, symbol, *, export_levels=20, maximum_levels=20000):
        if not re.fullmatch(r"[A-Z0-9]+/[A-Z0-9]+", symbol):
            raise ValueError("explicit uppercase spot pair required")
        if not 1 <= export_levels <= 5000 or maximum_levels < export_levels:
            raise ValueError("invalid depth bounds")
        self.symbol, self.export_levels, self.maximum_levels = symbol, export_levels, maximum_levels
        self.reset()

    def reset(self):
        self.last_id = None
        self.snapshot_id = None
        self.snapshot_observed = None
        self.last_event_at = None
        self.bids, self.asks = {}, {}
        self.synced = False

    def install_snapshot(self, document, *, observed_at):
        self.reset()
        bids, asks = _levels(document["bids"]), _levels(document["asks"])
        bids, asks = {p: q for p, q in bids.items() if q}, {p: q for p, q in asks.items() if q}
        if not bids or not asks or max(bids) >= min(asks):
            raise ValueError("empty or crossed snapshot")
        if max(len(bids), len(asks)) > self.maximum_levels:
            raise ValueError("snapshot exceeds bound")
        self.last_id = self.snapshot_id = _integer(document["lastUpdateId"])
        self.snapshot_observed = aware_utc(observed_at)
        self.bids, self.asks = bids, asks
        # Outside these initial boundaries, unchanged unseen levels are unknown.
        self.bid_boundary, self.ask_boundary = min(bids), max(asks)

    def apply(self, document, *, observed_at, available_at=None):
        try:
            event = normalize_diff(document, self.symbol)
        except Exception:
            self.reset()
            raise
        if self.last_id is None:
            raise DepthGap("snapshot required")
        if event["u"] <= self.last_id:
            return None
        observed = aware_utc(observed_at)
        occurred = datetime.fromtimestamp(event["E"] / 1000., timezone.utc)
        available = aware_utc(available_at or utc_now())
        if (event["U"] > self.last_id + 1
                or (self.last_event_at is not None and occurred < self.last_event_at)
                or available < max(observed, self.snapshot_observed)):
            self.reset()
            raise DepthGap("sequence or clock discontinuity")
        for book, changes in ((self.bids, event["b"]), (self.asks, event["a"])):
            for price, qty in _levels(changes).items():
                if qty:
                    book[price] = qty
                else:
                    book.pop(price, None)
        bids = sorted(((p, q) for p, q in self.bids.items() if p >= self.bid_boundary), reverse=True)
        asks = sorted((p, q) for p, q in self.asks.items() if p <= self.ask_boundary)
        if (not bids or not asks or bids[0][0] >= asks[0][0]
                or max(len(self.bids), len(self.asks)) > self.maximum_levels):
            self.reset()
            raise DepthGap("book crossed, exhausted known depth or exceeded bounds")
        self.last_id, self.last_event_at, self.synced = event["u"], occurred, True
        return {"kind": "synchronized_book", "exchange_id": "binance", "environment": "live",
            "market_type": "spot", "symbol": self.symbol, "quote_currency": self.symbol.split("/")[1],
            "sequence_verified": True, "snapshot_last_update_id": self.snapshot_id,
            "first_update_id": event["U"], "last_update_id": event["u"],
            "occurred_at": occurred.isoformat(), "observed_at": observed.isoformat(),
            "available_at": available.isoformat(), "snapshot_observed_at": self.snapshot_observed.isoformat(),
            "clock_consistent": occurred <= observed,
            "exchange_event_minus_local_receipt_seconds": (occurred - observed).total_seconds(),
            "bids": _pairs(bids[:self.export_levels]), "asks": _pairs(asks[:self.export_levels]),
            "known_bid_boundary": str(self.bid_boundary), "known_ask_boundary": str(self.ask_boundary),
            "depth_scope": "bounded_visible_levels_within_snapshot_boundaries",
            "exchange_clock_semantics": "depth_stream_event_time_not_matching_time"}


class DepthJournal:
    """New append-only JSONL session with a verifiable hash chain."""
    def __init__(self, path):
        self.path = Path(path)
        self.stream = self.path.open("x", encoding="utf-8")
        self.sequence, self.digest = 0, "0" * 64

    def append(self, record):
        row = {**record, "journal_sequence": self.sequence + 1, "previous_sha256": self.digest}
        payload = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        self.stream.write(json.dumps({**row, "sha256": digest}, sort_keys=True, allow_nan=False) + "\n")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.sequence, self.digest = row["journal_sequence"], digest
        return {**row, "sha256": digest}

    def close(self):
        self.stream.close()


def verify_depth_journal(path):
    previous, sequence, rows = "0" * 64, 0, []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            digest = row.pop("sha256")
            payload = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if (row["previous_sha256"] != previous or row["journal_sequence"] != sequence + 1
                    or hashlib.sha256(payload.encode()).hexdigest() != digest):
                raise ValueError("depth evidence hash chain mismatch")
            previous, sequence = digest, sequence + 1
            rows.append({**row, "sha256": digest})
    return rows


def displayed_depth_sweep(book, *, quote_notional, side):
    """Counterfactual visible-book consumption; explicitly never a venue fill."""
    if side not in {"buy", "sell"} or not math.isfinite(quote_notional) or quote_notional <= 0:
        raise ValueError("positive notional and explicit side required")
    levels = [(float(p), float(q)) for p, q in book["asks" if side == "buy" else "bids"]]
    reference = (float(book["bids"][0][0]) + float(book["asks"][0][0])) / 2.
    requested, paid, quantity = quote_notional / reference, 0., 0.
    for price, size in levels:
        take = min(size, requested - quantity)
        paid, quantity = paid + price * take, quantity + take
        if quantity >= requested - 1e-12:
            break
    covered = quantity >= requested - 1e-12
    vwap = paid / quantity if quantity else None
    return {"symbol": book["symbol"], "side": side, "quote_notional": quote_notional,
        "available_at": book["available_at"], "last_update_id": book["last_update_id"],
        "status": "displayed_depth_covers" if covered else "beyond_observed_depth",
        "displayed_base_quantity": quantity, "uncovered_base_quantity": max(0., requested - quantity),
        "visible_book_vwap": vwap if covered else None,
        "visible_book_shortfall_bps": (vwap / reference - 1) * (1 if side == "buy" else -1) * 10000 if covered else None,
        "clock_consistent": book["clock_consistent"],
        "eligible_for_execution_calibration": False,
        "realized_fill": False, "realized_capacity_validated": False}


class PublicDepthSnapshot:
    def __init__(self, *, timeout_seconds=5., limit=1000):
        import ccxt
        from core.request_budget import install_exchange_budget
        self.limit, self.timeout = limit, timeout_seconds
        self.client = ccxt.binance({"enableRateLimit": True, "requests_trust_env": True,
            "timeout": int(timeout_seconds * 1000), "maxRetriesOnFailure": 0})
        self.client.session.auth = anonymous_public_auth
        install_exchange_budget(self.client, "binance", priority="research")

    def __call__(self, symbol):
        from core.request_budget import request_scope
        with request_scope(deadline=time.monotonic() + self.timeout, priority="research"):
            return self.client.public_get_depth({"symbol": symbol.replace("/", ""), "limit": self.limit})

    def close(self):
        self.client.session.close()

    def clock_probe(self):
        """Bound the exchange-minus-local wall-clock offset, preserving originals."""
        from core.request_budget import request_scope
        sent, started = utc_now(), time.monotonic()
        with request_scope(deadline=started + self.timeout, priority="research"):
            response = self.client.public_get_time()
        received, elapsed = utc_now(), time.monotonic() - started
        server_ms = _integer(response["serverTime"])
        server = datetime.fromtimestamp(server_ms / 1000., timezone.utc)
        return {"kind": "public_server_time_probe", "server_time_ms": server_ms,
            "server_time": server.isoformat(), "request_sent_at": sent.isoformat(),
            "response_received_at": received.isoformat(), "round_trip_monotonic_seconds": elapsed,
            "exchange_minus_local_offset_lower_seconds": (server - received).total_seconds(),
            "exchange_minus_local_offset_upper_seconds": (server - sent).total_seconds(),
            "system_clock_modified": False, "timestamps_corrected": False,
            "assumption": "serverTime sampled between local send and receive; no clock step during request"}


async def observe_depth(*, symbols, duration_seconds, journal, quote_store, run_id,
        export_levels=20, snapshot_limit=1000, timeout_seconds=5., maximum_reconnects=3,
        snapshot_fetcher=None, session_factory=None):
    """Collect within one explicit wall-clock budget, with bounded resynchronization.

    First receive a diff, then fetch snapshot while the receiver buffers newer
    diffs. Any disconnect, queue overflow, gap or invalid clock discards the book.
    Reconnection starts with a new stream and REST snapshot. No service is started.
    """
    import aiohttp
    if (not symbols or len(set(symbols)) != len(symbols) or len(symbols) > 10
            or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 3600
            or snapshot_limit not in {100, 500, 1000, 5000} or not 0 < timeout_seconds <= 30
            or not 0 <= maximum_reconnects <= 10):
        raise ValueError("invalid bounded depth collection parameters")
    for symbol in symbols:
        SequencedDepthBook(symbol, export_levels=export_levels)
    deadline = time.monotonic() + duration_seconds
    owned_fetchers, snapshot_workers = [], []
    health = {symbol: {"synchronized_observations": 0, "reconnects": 0, "snapshots": 0,
        "diffs": 0, "clock_conflicts": 0, "eligible_quotes": 0,
        "last_error_category": None, "status": "unavailable"} for symbol in symbols}
    # Explicit proxy routing honors deployment transport settings, while
    # trust_env=False prevents aiohttp from consulting account .netrc files.
    proxy = getproxies().get("https")
    factory = session_factory or aiohttp.ClientSession
    async with factory(timeout=aiohttp.ClientTimeout(total=None, connect=timeout_seconds), trust_env=False) as session:
        async def collect_symbol(symbol):
            stats = health[symbol]
            # Each receiver owns its public HTTP client; requests/ccxt clients
            # must not be shared by concurrent snapshot worker threads.
            fetcher = snapshot_fetcher or PublicDepthSnapshot(timeout_seconds=timeout_seconds, limit=snapshot_limit)
            if snapshot_fetcher is None:
                owned_fetchers.append(fetcher)
            for attempt in range(maximum_reconnects + 1):
                if time.monotonic() >= deadline:
                    break
                book = SequencedDepthBook(symbol, export_levels=export_levels)
                queue = asyncio.Queue(maxsize=2000)
                producer = None
                epoch = uuid4().hex
                try:
                    url = "wss://data-stream.binance.vision/ws/" + symbol.replace("/", "").lower() + "@depth@100ms"
                    async with session.ws_connect(url, autoping=True, heartbeat=20, proxy=proxy,
                            timeout=aiohttp.ClientWSTimeout(ws_close=1.),
                            max_msg_size=2 * 1024 * 1024) as websocket:
                        async def receive():
                            while True:
                                message = await websocket.receive()
                                if message.type != aiohttp.WSMsgType.TEXT:
                                    raise DepthGap("stream disconnected")
                                event = normalize_diff(json.loads(message.data), symbol)
                                observed = utc_now().isoformat()
                                journal.append({"kind": "raw_depth_diff", "symbol": symbol,
                                    "collector_run_id": run_id, "connection_epoch": epoch,
                                    "occurred_at": datetime.fromtimestamp(event["E"] / 1000., timezone.utc).isoformat(),
                                    "observed_at": observed, "payload": event})
                                stats["diffs"] += 1
                                queue.put_nowait((event, observed))
                        producer = asyncio.create_task(receive())

                        async def next_event():
                            if producer.done():
                                producer.result()
                            get = asyncio.create_task(queue.get())
                            try:
                                done, _ = await asyncio.wait({get, producer},
                                    timeout=min(timeout_seconds, max(.001, deadline - time.monotonic())),
                                    return_when=asyncio.FIRST_COMPLETED)
                                if producer in done:
                                    producer.result()
                                if get not in done:
                                    raise TimeoutError("no depth event within budget")
                                return get.result()
                            finally:
                                get.cancel()
                                with suppress(asyncio.CancelledError):
                                    await get

                        first, first_observed = await next_event()
                        pending_snapshot = asyncio.create_task(asyncio.to_thread(fetcher, symbol))
                        snapshot_workers.append(pending_snapshot)
                        snapshot = await asyncio.shield(pending_snapshot)
                        snapshot_observed = utc_now().isoformat()
                        snapshot = {"lastUpdateId": _integer(snapshot["lastUpdateId"]),
                            "bids": _pairs(_levels(snapshot["bids"]).items()),
                            "asks": _pairs(_levels(snapshot["asks"]).items())}
                        journal.append({"kind": "raw_depth_snapshot", "symbol": symbol,
                            "collector_run_id": run_id, "connection_epoch": epoch, "occurred_at": None,
                            "occurred_at_status": "unknown", "observed_at": snapshot_observed, "payload": snapshot})
                        stats["snapshots"] += 1
                        if snapshot["lastUpdateId"] < first["U"]:
                            raise DepthGap("snapshot behind buffered stream")
                        book.install_snapshot(snapshot, observed_at=snapshot_observed)
                        event, observed = first, first_observed
                        while time.monotonic() < deadline:
                            if producer.done():
                                producer.result()
                            result = book.apply(event, observed_at=observed, available_at=utc_now())
                            if result:
                                quote_id = f"{run_id}:{symbol}:{epoch}:{result['last_update_id']}"
                                record = {**result, "quote_id": quote_id, "collector_run_id": run_id,
                                    "connection_epoch": epoch}
                                journal.append(record)
                                quote = {"quote_id": quote_id, "exchange_id": "binance",
                                    "environment": "live", "market_type": "spot", "symbol": symbol,
                                    "quote_currency": result["quote_currency"], "bid": result["bids"][0][0],
                                    "ask": result["asks"][0][0], "bid_quantity": result["bids"][0][1],
                                    "ask_quantity": result["asks"][0][1], "occurred_at": result["occurred_at"],
                                    "occurred_at_status": "exchange_timestamp", "observed_at": result["observed_at"],
                                    "available_at": result["available_at"], "source_id": "binance:spot:sequenced_diff_depth",
                                    "collector_run_id": run_id, "independent": True, "observation_kind": "public_order_book"}
                                if result["clock_consistent"]:
                                    quote_store.append(quote)
                                    stats["eligible_quotes"] += 1
                                else:
                                    stats["clock_conflicts"] += 1
                                stats["synchronized_observations"] += 1
                                stats["status"] = "observed_depth"
                            event, observed = await next_event()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    stats["last_error_category"] = type(exc).__name__
                    journal.append({"kind": "resync_required", "symbol": symbol,
                        "collector_run_id": run_id, "connection_epoch": epoch,
                        "observed_at": utc_now().isoformat(), "error_category": type(exc).__name__})
                    stats["status"] = "partial" if stats["synchronized_observations"] else "unavailable"
                finally:
                    book.reset()
                    if producer:
                        producer.cancel()
                        with suppress(asyncio.CancelledError, Exception):
                            await producer
                if time.monotonic() < deadline and attempt < maximum_reconnects:
                    stats["reconnects"] += 1
                    await asyncio.sleep(min(1., max(0., deadline - time.monotonic())))
        tasks = [asyncio.create_task(collect_symbol(symbol)) for symbol in symbols]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=max(.001, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # A canceled consumer does not kill its REST thread. Let the
            # deadline-bound public request finish before closing its client.
            if snapshot_workers:
                await asyncio.gather(*snapshot_workers, return_exceptions=True)
            for fetcher in owned_fetchers:
                fetcher.close()
    all_depth = all(s["synchronized_observations"] for s in health.values())
    clock_conflicts = sum(s["clock_conflicts"] for s in health.values())
    return {"symbols": health, "stopped_at": utc_now().isoformat(), "lifecycle": "stopped",
        "observations": sum(s["synchronized_observations"] for s in health.values()),
        "status": "clock_conflict" if all_depth and clock_conflicts else "observed_depth" if all_depth else "incomplete",
        "public_read_only": True, "orders_submitted": 0}
