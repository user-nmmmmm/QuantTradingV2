"""Independent public bid/ask observations, never part of order submission.

The optional sampler owns its own anonymous public-data client and background
thread. It cannot submit/cancel orders. A missing venue timestamp stays unknown;
the local receipt clock must never stand in for exchange occurrence time.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from threading import Event, RLock, Thread
import time
from uuid import uuid4


_FIELDS = frozenset({"quote_id", "exchange_id", "environment", "market_type", "symbol",
    "quote_currency", "bid", "ask", "bid_quantity", "ask_quantity", "occurred_at",
    "occurred_at_status", "observed_at", "available_at", "request_started_at",
    "request_duration_seconds", "source_id", "collector_run_id", "independent",
    "observation_kind", "sequence"})


def aware_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quote timestamps must be timezone aware")
    return value.astimezone(timezone.utc)


def utc_now():
    return datetime.now(timezone.utc)


def validate_quote(row):
    """Return only bounded scalar evidence fields, rejecting malformed BBOs."""
    result = {key: value for key, value in row.items() if key in _FIELDS and key != "sequence"}
    for key in ("quote_id", "exchange_id", "environment", "market_type", "symbol",
                "quote_currency", "source_id", "collector_run_id"):
        if not isinstance(result.get(key), str) or not 0 < len(result[key]) <= 256:
            raise ValueError(f"invalid quote {key}")
    if result["environment"] not in {"live", "sandbox"}:
        raise ValueError("explicit live or sandbox quote environment required")
    if result.get("independent") is not True or result.get("observation_kind") != "public_order_book":
        raise ValueError("independent public order-book provenance required")
    for key in ("bid", "ask"):
        value = float(result[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError("quote prices must be finite and positive")
        result[key] = value
    if result["bid"] > result["ask"]:
        raise ValueError("crossed quote")
    for key in ("bid_quantity", "ask_quantity", "request_duration_seconds"):
        if result.get(key) is not None:
            value = float(result[key])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid quote {key}")
            result[key] = value
    observed, available = aware_utc(result["observed_at"]), aware_utc(result["available_at"])
    if available < observed:
        raise ValueError("quote availability precedes local observation")
    for key in ("observed_at", "available_at", "request_started_at"):
        if result.get(key) is not None:
            result[key] = aware_utc(result[key]).isoformat()
    if result.get("request_started_at") and aware_utc(result["request_started_at"]) > observed:
        raise ValueError("quote request starts after response")
    if result.get("occurred_at") is None:
        if result.get("occurred_at_status") != "unknown":
            raise ValueError("missing venue clock must be explicitly unknown")
        result["occurred_at"] = None
    else:
        occurred = aware_utc(result["occurred_at"])
        if result.get("occurred_at_status") != "exchange_timestamp":
            raise ValueError("quote occurrence requires venue timestamp provenance")
        # Small clock skew is still evidence of incompatible clocks, not license
        # to replace exchange time with a local timestamp.
        if occurred > observed:
            raise ValueError("venue quote time follows local observation")
        result["occurred_at"] = occurred.isoformat()
    return result


def read_quote_observations(path, *, after_sequence=0, limit=None, lock_timeout_seconds=.05):
    """Read an existing append-only quote database without creating it."""
    if after_sequence < 0 or (limit is not None and limit < 1):
        raise ValueError("invalid cursor")
    target = Path(path).resolve()
    with closing(sqlite3.connect(target.as_uri()+"?mode=ro", uri=True,
                                timeout=min(max(float(lock_timeout_seconds), 0.), 1.))) as connection:
        query = "SELECT sequence, quote_id, payload FROM quote_observations WHERE sequence>? ORDER BY sequence"
        params = [int(after_sequence)]
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        result = []
        for sequence, quote_id, payload in connection.execute(query, params):
            row = validate_quote(json.loads(payload))
            if row["quote_id"] != quote_id:
                raise ValueError("quote identity mismatch")
            result.append({**row, "sequence": sequence})
        return result


class QuoteObservationStore:
    def __init__(self, path, *, lock_timeout_seconds=.05):
        if str(path) == ":memory:":
            raise ValueError("quote sampling requires a durable sidecar path")
        if not math.isfinite(lock_timeout_seconds) or not 0 <= lock_timeout_seconds <= 1:
            raise ValueError("bounded database lock timeout required")
        self.path, self.lock_timeout_seconds = str(path), lock_timeout_seconds
        self._lock = RLock()
        self._health = {"write_failures": 0, "last_error_category": None}
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS quote_observations ("
                               "sequence INTEGER PRIMARY KEY AUTOINCREMENT, quote_id TEXT NOT NULL UNIQUE, "
                               "payload TEXT NOT NULL)")
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER IF NOT EXISTS quotes_no_{operation.lower()} "
                    f"BEFORE {operation} ON quote_observations BEGIN "
                    "SELECT RAISE(ABORT, 'quote observations are append-only'); END")
            self.persisted_records = connection.execute("SELECT COUNT(*) FROM quote_observations").fetchone()[0]

    def _connect(self):
        return sqlite3.connect(self.path, timeout=self.lock_timeout_seconds)

    def append(self, observation):
        row = validate_quote(observation)
        payload = json.dumps(row, sort_keys=True, allow_nan=False, separators=(",", ":"))
        try:
            with self._lock, closing(self._connect()) as connection, connection:
                prior = connection.execute("SELECT sequence,payload FROM quote_observations WHERE quote_id=?",
                                           (row["quote_id"],)).fetchone()
                if prior:
                    if prior[1] != payload:
                        raise ValueError("conflicting quote identity")
                    return {**row, "sequence": prior[0]}
                cursor = connection.execute("INSERT INTO quote_observations(quote_id,payload) VALUES (?,?)",
                                            (row["quote_id"], payload))
                sequence = cursor.lastrowid
            with self._lock:
                self.persisted_records += 1
            return {**row, "sequence": sequence}
        except Exception as exc:
            with self._lock:
                self._health["write_failures"] += 1
                self._health["last_error_category"] = type(exc).__name__
            raise

    def read_all(self):
        return read_quote_observations(self.path, lock_timeout_seconds=self.lock_timeout_seconds)

    def health(self):
        with self._lock:
            return {"path": self.path, "persisted_records": self.persisted_records,
                    "status": "degraded" if self._health["write_failures"] else "ok", **self._health}


class _PublicBookFetcher:
    """Lazy anonymous ccxt client; only the public order-book method is exposed."""
    def __init__(self, exchange_id, market_type, environment, timeout_seconds):
        import ccxt
        from core.request_budget import install_exchange_budget
        if exchange_id not in ccxt.exchanges:
            raise ValueError("unsupported public exchange")
        market = {"spot_margin": "spot", "margin": "spot", "perpetual": "swap"}.get(market_type, market_type)
        self.client = getattr(ccxt, exchange_id)({"enableRateLimit": True,
            # Honor existing environment transport/proxy configuration. This
            # observer neither clears proxies nor changes any system setting.
            "requests_trust_env": True,
            "timeout": int(timeout_seconds*1000), "maxRetriesOnFailure": 0,
            "options": {"defaultType": market, "fetchMarkets": [market],
                        "fetchCurrencies": False, "fetchMargins": False}})
        # requests trust_env also enables .netrc discovery. A truthy no-op auth
        # handler prevents it, keeping the venue request strictly anonymous.
        self.client.session.auth = anonymous_public_auth
        if environment == "sandbox":
            self.client.set_sandbox_mode(True)
        install_exchange_budget(self.client, exchange_id, priority="research")

    def __call__(self, symbol):
        return self.client.fetch_order_book(symbol, limit=5)

    def close(self):
        session = getattr(self.client, "session", None)
        if callable(getattr(session, "close", None)):
            session.close()


def anonymous_public_auth(request):
    """Allow configured proxy routing without importing venue .netrc secrets."""
    request.headers.pop("Authorization", None)
    return request


def quote_error_details(exc):
    """Whitelisted classifications only: exception messages can contain secrets."""
    chain, codes, seen = [], [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(type(exc).__name__)
        codes.extend({"field": field, "value": getattr(exc, field)} for field in ("errno", "winerror")
                     if isinstance(getattr(exc, field, None), int))
        exc = exc.__cause__ or exc.__context__
    return {"exception_chain": chain, "os_error_codes": codes}


class BackgroundQuoteSampler:
    """A stoppable, opt-in observer with no broker or order callback.

    ``start`` does no network I/O. ``stop`` returns within its join budget even
    if a faulty injected client ignores the network timeout; health then says
    stop_pending. Real public requests use both SDK timeout and request budget.
    """
    def __init__(self, *, exchange_id, symbols, market_type, store, environment="live",
                 interval_seconds=1., timeout_seconds=5., stale_after_seconds=5., fetcher=None):
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("nonempty distinct quote symbols required")
        for value in (interval_seconds, timeout_seconds, stale_after_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("positive finite sampling times required")
        if environment not in {"live", "sandbox"} or market_type not in {"spot", "spot_margin", "margin", "swap", "future", "perpetual"}:
            raise ValueError("explicit supported market and environment required")
        if any(not isinstance(s, str) or "/" not in s for s in symbols):
            raise ValueError("use unambiguous ccxt symbols such as BTC/USDT")
        self.exchange_id, self.symbols, self.market_type = exchange_id, tuple(symbols), market_type
        self.store, self.environment = store, environment
        self.interval_seconds, self.timeout_seconds = interval_seconds, timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self._fetcher, self._owns_fetcher = fetcher, fetcher is None
        self.run_id = "public-quotes-"+uuid4().hex
        self._stop, self._lock, self._thread = Event(), RLock(), None
        self._started_at = None
        self._symbols = {s: {"attempts": 0, "observations": 0, "errors": 0,
                            "last_observed_at": None, "last_error_category": None,
                            "unknown_exchange_timestamps": 0} for s in symbols}

    def start(self):
        if self._thread is not None:
            raise RuntimeError("sampler instances cannot be restarted")
        self._started_at = utc_now().isoformat()
        self._thread = Thread(target=self._run, name=self.run_id, daemon=True)
        self._thread.start()
        return self

    def _error(self, symbol, exc):
        with self._lock:
            self._symbols[symbol]["errors"] += 1
            self._symbols[symbol]["last_error_category"] = type(exc).__name__
            self._symbols[symbol]["last_error_details"] = quote_error_details(exc)
            self._symbols[symbol]["last_error_at"] = utc_now().isoformat()

    def _run(self):
        from core.request_budget import request_scope
        try:
            if self._fetcher is None:
                self._fetcher = _PublicBookFetcher(self.exchange_id, self.market_type, self.environment, self.timeout_seconds)
            while not self._stop.is_set():
                cycle = time.monotonic()
                for symbol in self.symbols:
                    if self._stop.is_set():
                        break
                    with self._lock:
                        self._symbols[symbol]["attempts"] += 1
                    started, monotonic = utc_now(), time.monotonic()
                    try:
                        with request_scope(deadline=monotonic+self.timeout_seconds, priority="research"):
                            book = self._fetcher(symbol)
                        observed, duration = utc_now(), time.monotonic()-monotonic
                        # A stop request never causes a delayed response to be
                        # relabeled with the earlier stopping time.
                        if self._stop.is_set():
                            break
                        bid, bid_qty = book["bids"][0][:2]
                        ask, ask_qty = book["asks"][0][:2]
                        timestamp = book.get("timestamp")
                        occurred = (datetime.fromtimestamp(float(timestamp)/1000, timezone.utc).isoformat()
                                    if timestamp is not None else None)
                        quote = {"quote_id": uuid4().hex, "exchange_id": self.exchange_id,
                            "environment": self.environment, "market_type": self.market_type, "symbol": symbol,
                            "quote_currency": symbol.split("/")[1].split(":")[0], "bid": bid, "ask": ask,
                            "bid_quantity": bid_qty, "ask_quantity": ask_qty,
                            "occurred_at": occurred, "occurred_at_status": "exchange_timestamp" if occurred else "unknown",
                            "observed_at": observed.isoformat(), "available_at": utc_now().isoformat(),
                            "request_started_at": started.isoformat(), "request_duration_seconds": duration,
                            "source_id": self.exchange_id+":public_rest_order_book", "collector_run_id": self.run_id,
                            "independent": True, "observation_kind": "public_order_book"}
                        self.store.append(quote)
                        with self._lock:
                            state = self._symbols[symbol]
                            state["observations"] += 1
                            state["last_observed_at"] = observed.isoformat()
                            state["unknown_exchange_timestamps"] += occurred is None
                            state["last_error_category"] = None
                            state.pop("last_error_details", None)
                    except Exception as exc:
                        self._error(symbol, exc)
                self._stop.wait(max(0., self.interval_seconds-(time.monotonic()-cycle)))
        except Exception as exc:
            for symbol in self.symbols:
                self._error(symbol, exc)
        finally:
            if self._owns_fetcher and callable(getattr(self._fetcher, "close", None)):
                try:
                    self._fetcher.close()
                except Exception as exc:
                    for symbol in self.symbols:
                        self._error(symbol, exc)

    def stop(self, *, join_timeout_seconds=1.):
        if not math.isfinite(join_timeout_seconds) or join_timeout_seconds < 0:
            raise ValueError("finite nonnegative stop wait required")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(join_timeout_seconds)
        return self.health()

    def health(self, *, as_of=None):
        now = aware_utc(as_of) if as_of is not None else utc_now()
        with self._lock:
            symbols = {s: dict(row) for s, row in self._symbols.items()}
        for row in symbols.values():
            age = (now-aware_utc(row["last_observed_at"])).total_seconds() if row["last_observed_at"] else None
            row.update(observation_age_seconds=age,
                       status="disconnected" if row["last_error_category"] else
                       "unavailable" if age is None else "stale" if age > self.stale_after_seconds else "ok")
        alive = bool(self._thread and self._thread.is_alive())
        store_health = self.store.health()
        return {"schema": "public-quote-sampler/v1", "collector_run_id": self.run_id,
            "exchange_id": self.exchange_id, "environment": self.environment, "market_type": self.market_type,
            "started_at": self._started_at, "as_of": now.isoformat(),
            "lifecycle": "stop_pending" if alive and self._stop.is_set() else
                         "running" if alive else "stopped" if self._stop.is_set() else "idle_or_failed",
            "status": "ok" if store_health["status"] == "ok" and all(s["status"] == "ok" for s in symbols.values()) else "degraded",
            "symbols": symbols, "store": store_health, "public_read_only": True,
            "orders_submitted": 0, "interval_seconds": self.interval_seconds,
            "timeout_seconds": self.timeout_seconds, "stale_after_seconds": self.stale_after_seconds,
            "availability_semantics": "locally received and validated independent collector observation",
            "exchange_time_unknown_is_not_imputed": True}
