"""Market-data adapters for the shared trading runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from time import perf_counter
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from core.indicators import Indicators
from core.data_fetcher import DataFetcher
from core.runtime import MarketDataSlice
from core.timeframes import closed_bars
from core.temporal_data import TemporalOHLCV, compatibility_audit, temporal_policy as _temporal_policy


def _calculate_indicators(frame):
    """计算期间暂移证据映射，避免 pandas 切列时反复复制；完成后原样恢复。"""
    proof = frame.attrs.pop("temporal_source_versions", None)
    try:
        Indicators.calculate_all(frame)
    finally:
        if proof is not None:
            frame.attrs["temporal_source_versions"] = proof


def normalize_market_frame(df: pd.DataFrame) -> pd.DataFrame:
    """复制并规范化索引：剔除无效时间、重复保留末行、排序，带时区值转 naive UTC。

    无时区索引沿用已有 UTC 约定；本函数不推断来源时区，也不证明数据当时可用。
    """

    if df is None or df.empty:
        return pd.DataFrame()
    normalized = df.copy()
    if not isinstance(normalized.index, pd.DatetimeIndex):
        normalized.index = pd.to_datetime(normalized.index, errors="coerce")
    if normalized.index.hasnans:
        normalized = normalized.loc[~pd.isna(normalized.index)]
    if normalized.empty:
        return normalized
    if normalized.index.tz is not None:
        normalized.index = normalized.index.tz_convert("UTC").tz_localize(None)
    # Check ordering first: pandas can establish uniqueness of a sorted index
    # without building a hash table for every timestamp.
    ordered = normalized.index.is_monotonic_increasing
    if not normalized.index.is_unique:
        normalized = normalized.loc[~normalized.index.duplicated(keep="last")]
    if not ordered:
        normalized = normalized.sort_index()
    return normalized


class _FastBar:
    """A row snapshot with fast scalar access for the historical engine.

    ``DataFrame._mgr.fast_xs`` supplies the same promoted row values used by
    ``iloc``. The copy preserves the row when strategies subsequently add or
    replace columns in the history frame. Uncommon Series operations can still
    materialize a genuine Series from that snapshot.
    """

    __slots__ = ("_name", "_columns", "_locations", "_values", "_series")

    def __init__(self, name, columns, locations, values: np.ndarray) -> None:
        self._columns = columns
        self._locations = locations
        self._values = values.copy()
        self._series: Optional[pd.Series] = None
        self._name = name

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value) -> None:
        self._name = value
        if self._series is not None:
            self._series.name = value

    def _as_series(self) -> pd.Series:
        if self._series is None:
            self._series = pd.Series(self._values, index=self._columns, name=self.name)
        return self._series

    def __getitem__(self, key):
        if self._series is not None:
            return self._series[key]
        try:
            return self._values[self._locations[key]]
        except (KeyError, TypeError):
            return self._as_series()[key]

    def get(self, key, default=None):
        if self._series is not None:
            return self._series.get(key, default)
        try:
            return self._values[self._locations[key]]
        except KeyError:
            return default
        except TypeError:
            return self._as_series().get(key, default)

    def __setitem__(self, key, value) -> None:
        self._as_series()[key] = value

    def __len__(self) -> int:
        return len(self._series) if self._series is not None else len(self._values)

    def __iter__(self):
        return iter(self._series) if self._series is not None else iter(self._values)

    def __contains__(self, key) -> bool:
        return key in self._series if self._series is not None else key in self._locations

    def __getattr__(self, name):
        if name in self._locations:
            return self[name]
        return getattr(self._as_series(), name)

    def __repr__(self) -> str:
        return repr(self._as_series())

    def __bool__(self) -> bool:
        return bool(self._as_series())

    def __array__(self, dtype=None, copy=None):
        return np.asarray(self._as_series(), dtype=dtype, copy=copy)

    def keys(self):
        return self._series.keys() if self._series is not None else self._columns

    def items(self):
        return self._series.items() if self._series is not None else zip(self._columns, self._values)

    def copy(self, deep: bool = True) -> pd.Series:
        if self._series is not None:
            return self._series.copy(deep=deep)
        return pd.Series(self._values, index=self._columns, name=self.name).copy(deep=deep)


class HistoricalMarketDataAdapter:
    """按真实 bar 的并集或交集回放；strict 模式逐时点重建策略可见历史。

    常规模式预计算指标。strict 模式只对当时可见的原始版本计算指标，
    撮合使用的已实现 OHLCV 则保留在事件 bars 中，由运行时隔离两种视图。
    """

    _POSITION_CHUNK_SIZE = 2048

    def __init__(
        self,
        data_map: Dict[str, pd.DataFrame],
        *,
        timeframe: str = "unknown",
        calculate_indicators: bool = True,
        alignment_mode: str = "union",
        universe: Optional[object] = None,
        temporal_policy=None,
    ) -> None:
        if alignment_mode not in {"union", "intersection"}:
            raise ValueError("alignment_mode must be 'union' or 'intersection'")
        self.timeframe = timeframe
        self.alignment_mode = alignment_mode
        self.temporal_policy = _temporal_policy(temporal_policy)
        self.temporal_audit = {"policy": self.temporal_policy.mode, "symbols": {}}
        self.temporal_identity = {}
        self._temporal_histories = {}
        self._calculate_indicators = calculate_indicators
        if universe is not None:
            apply_universe = getattr(universe, "apply", None)
            if not callable(apply_universe):
                raise TypeError("universe must provide apply(data_map)")
            data_map = apply_universe(data_map)
        self.data_map: Dict[str, pd.DataFrame] = {}
        for symbol, frame in data_map.items():
            if (self.temporal_policy.mode == "strict" or self.temporal_policy.store_path or
                    frame.attrs.get("temporal_identity")):
                dataset = (frame.attrs.get("temporal_identity") or {}).get("dataset_id", f"historical|{symbol}|{timeframe}")
                versions = TemporalOHLCV(dataset,
                                        timeframe=timeframe, policy=self.temporal_policy)
                if not versions.import_identity(frame):
                    versions.ingest(frame, source_reference="historical_frame_import")
                self._temporal_histories[symbol] = versions
                self.temporal_identity[symbol] = versions.identity
                self.temporal_audit["symbols"][symbol] = versions.audit
            else:
                self.temporal_audit["symbols"][symbol] = compatibility_audit()
            prepared = normalize_market_frame(frame)
            if prepared.empty:
                continue
            if calculate_indicators and self.temporal_policy.mode != "strict":
                _calculate_indicators(prepared)
            self.data_map[symbol] = prepared

    @property
    def timestamps(self) -> pd.DatetimeIndex:
        frames = list(self.data_map.values())
        if not frames:
            return pd.DatetimeIndex([])
        timeline = frames[0].index
        for frame in frames[1:]:
            if self.alignment_mode == "intersection":
                timeline = timeline.intersection(frame.index)
            else:
                timeline = timeline.union(frame.index)
        return timeline.sort_values()

    def stream(self, *, start_at=None, fast_bars: bool = False) -> Iterable[MarketDataSlice]:
        if self.temporal_policy.mode == "strict":
            yield from self._strict_stream(start_at=start_at)
            return
        timeline = self.timestamps
        start_position = 0
        # Fully aligned frames already have the timeline's row positions.
        # Retain the index identity so a replaced index uses the lookup path.
        sources = [
            (symbol, frame, frame.index, frame.index.equals(timeline), frame._ixs)
            for symbol, frame in self.data_map.items()
        ]
        column_cache = {}
        if start_at is not None:
            point = pd.Timestamp(start_at)
            if point.tzinfo is not None:
                point = point.tz_convert("UTC").tz_localize(None)
            # Slice the sorted timeline instead of allocating a full boolean mask.
            start_position = int(timeline.searchsorted(point))
            timeline = timeline[start_position:]
        for offset in range(0, len(timeline), self._POSITION_CHUNK_SIZE):
            chunk = timeline[offset:offset + self._POSITION_CHUNK_SIZE]
            lookups = []
            for symbol, frame, original_index, aligned, row_access in sources:
                if aligned and frame.index is original_index:
                    rows = range(start_position + offset,
                                 start_position + offset + len(chunk))
                else:
                    # Native position arrays are bounded by the chunk size,
                    # even for sparse universes. searchsorted avoids both
                    # Python Timestamp dictionaries and a retained full matrix.
                    rows = frame.index.searchsorted(chunk)
                    rows[rows == len(frame)] = 0
                    rows[frame.index.take(rows) != chunk] = -1
                lookups.append((symbol, row_access, rows))
            for local_pos, timestamp in enumerate(chunk):
                bars: Dict[str, pd.Series] = {}
                positions: Dict[str, int] = {}
                for symbol, row_access, rows in lookups:
                    pos = int(rows[local_pos])
                    if pos >= 0:
                        # Read the current frame on every event so columns added
                        # by a strategy during a stream are visible immediately.
                        if fast_bars:
                            frame = self.data_map[symbol]
                            columns = frame.columns
                            manager = getattr(frame, "_mgr", None)
                            blocks = getattr(manager, "blocks", None)
                            cached = column_cache.get(symbol)
                            if (cached is None or cached[0] is not columns
                                    or cached[1] is not blocks):
                                if (columns.is_unique and manager is not None
                                        and not getattr(manager, "any_extension_types", True)):
                                    locations = {name: i for i, name in enumerate(columns)}
                                else:
                                    # Extension arrays and repeated column names
                                    # retain pandas' full Series behavior.
                                    locations = None
                                column_cache[symbol] = (columns, blocks, locations)
                            else:
                                locations = cached[2]
                            fast_xs = getattr(manager, "fast_xs", None)
                            if locations is not None and callable(fast_xs):
                                values = fast_xs(pos).array
                                if isinstance(values, np.ndarray):
                                    bars[symbol] = _FastBar(
                                        frame.index[pos], columns, locations, values
                                    )
                                else:
                                    bars[symbol] = row_access(pos)
                            else:
                                bars[symbol] = row_access(pos)
                        else:
                            # iloc's integer-row branch delegates to _ixs after
                            # validation; pos is already an in-range row number.
                            bars[symbol] = row_access(pos)
                        positions[symbol] = pos
                yield MarketDataSlice(
                    timestamp=timestamp,
                    bars=bars,
                    histories=self.data_map,
                    positions=positions,
                    timeframe=self.timeframe,
                    source="historical",
                )

    def _strict_stream(self, *, start_at=None):
        """保留撮合时间线，按收盘时刻加决策延迟重建策略历史。

        bars 供撮合与估值使用；histories/positions 才声明策略当时可见的输入。
        当前 bar 缺少可用证据时，不提供它在策略历史中的位置。
        """
        for timestamp in self.timestamps:
            if start_at is not None and timestamp < pd.Timestamp(start_at).tz_localize(None):
                continue
            histories, bars, positions = {}, {}, {}
            for symbol, realised in self.data_map.items():
                versions = self._temporal_histories[symbol]
                cutoff = (pd.Timestamp(timestamp).tz_localize("UTC") + versions.delta +
                          pd.Timedelta(seconds=self.temporal_policy.decision_delay_seconds))
                visible = versions.as_of(cutoff, allowed_times=realised.index)
                # 延迟只放宽当前 bar 的接收时间，不能提前读取下一根 bar。
                visible = visible.loc[visible.index <= timestamp].copy()
                if self._calculate_indicators and not visible.empty:
                    _calculate_indicators(visible)
                histories[symbol] = visible
                if timestamp in realised.index:
                    # 已实现的价格可供撮合/估值；策略必须另取当时可见的版本，
                    # 不能把事后修订后的价格当作决策时已知事实。
                    bars[symbol] = realised.loc[timestamp, [c for c in realised.columns
                        if c in {"open", "high", "low", "close", "volume", "spread_bps", "entry_blocked"}]]
                    if timestamp in visible.index:
                        positions[symbol] = int(visible.index.get_loc(timestamp))
                    else:
                        versions.audit["unavailable_current_bars"] = versions.audit.get("unavailable_current_bars", 0)+1
                versions.audit["execution_inputs"] = "frozen_realised_bars_for_matching_and_valuation_only"
                versions.audit["strategy_inputs"] = "visible_raw_versions_then_recomputed_indicators"
            yield MarketDataSlice(timestamp=timestamp, bars=bars, histories=histories,
                positions=positions, timeframe=self.timeframe, source="historical_strict")


class LiveMarketDataAdapter:
    """维护滚动行情和派生指标；poll 的进程内水位线只发出新收盘 bar。

    refresh 失败时保留旧缓存并记录 failed_symbols，调用方需据此判断健康状态。
    水位线不持久化；实盘重启后的去重与恢复由调度器及 StateStore 负责。
    """

    def __init__(
        self,
        symbols: List[str],
        fetcher,
        *,
        timeframe: str = "1d",
        lookback: int = 100,
        close_grace_seconds: float = 2.0,
        exchange_id: Optional[str] = None,
        market_type: str = "spot",
        max_workers: int = 1,
        temporal_policy=None,
        clock=None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
            raise ValueError("max_workers must be an integer between 1 and 32")
        self.symbols = list(symbols)
        self.fetcher = fetcher
        self.timeframe = timeframe
        self.lookback = max(int(lookback), 1)
        self.close_grace_seconds = close_grace_seconds
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.temporal_policy = _temporal_policy(temporal_policy)
        self._temporal_enabled = temporal_policy is not None
        self._temporal_clock = clock or (lambda: datetime.now(timezone.utc))
        self._temporal_histories = {}
        self.temporal_audit = {"policy": self.temporal_policy.mode, "symbols": {}}
        self.temporal_identity = {}
        self.data_map: Dict[str, pd.DataFrame] = {}
        self._watermarks: Dict[str, pd.Timestamp] = {}
        self._last_fetched_latest: Dict[str, pd.Timestamp] = {}
        self.regressed_symbols: set[str] = set()
        self.failed_symbols: Dict[str, str] = {}
        self.max_workers = max_workers
        self._executor: Optional[ThreadPoolExecutor] = None
        self._refresh_lock = RLock()
        # 策略会向 data_map 增加状态/通道列，因此单独缓存原始行情。
        # 合并派生视图会使重叠新行留下 NaN，并误让惰性指标认为无需重算。
        self._raw_data_map: Dict[str, pd.DataFrame] = {}
        self.refresh_metrics: Dict[str, object] = {}

    def _fetch_symbol(self, symbol):
        started = perf_counter()
        try:
            frame = normalize_market_frame(self.fetcher.fetch_ccxt(
                symbol, timeframe=self.timeframe, limit=self.lookback,
                **({"exchange_id": self.exchange_id} if self.exchange_id else {}),
                **({"market_type": self.market_type} if self.market_type not in {"spot", "margin"} else {}),
                **({"max_retries": 1} if isinstance(self.fetcher, DataFetcher) else {}),
            ))
            error = "empty_response" if frame.empty else None
        except Exception as exc:
            frame, error = pd.DataFrame(), type(exc).__name__
        return symbol, frame, perf_counter() - started, error

    def close(self) -> None:
        with self._refresh_lock:
            if self._executor is not None:
                self._executor.shutdown(wait=True, cancel_futures=True)
                self._executor = None

    def refresh(self) -> Dict[str, pd.DataFrame]:
        with self._refresh_lock:
            return self._refresh()

    def _refresh(self) -> Dict[str, pd.DataFrame]:
        started = perf_counter()
        self.regressed_symbols = set()
        self.failed_symbols = {}
        if self.max_workers == 1:
            results = [self._fetch_symbol(symbol) for symbol in self.symbols]
        else:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_workers, thread_name_prefix="market-data")
            # map 保持标的输入顺序；工作线程只抓行情，缓存提交和指标重算
            # 仍由调用线程串行完成，避免策略看到一半更新的状态。
            results = list(self._executor.map(self._fetch_symbol, self.symbols))
        fetched_at = perf_counter()
        recomputed = reused = 0
        for symbol, fetched, elapsed, error in results:
            if error is not None:
                # 留存旧行情便于恢复/诊断，但失败标记不能被缓存命中掩盖。
                self.failed_symbols[symbol] = error
                continue
            if self._temporal_enabled or fetched.attrs.get("temporal_identity"):
                versions = self._temporal_histories.get(symbol)
                if versions is None:
                    dataset = (fetched.attrs.get("temporal_identity") or {}).get("dataset_id",
                        f"{self.exchange_id or 'live'}|{self.market_type}|{symbol}|{self.timeframe}")
                    versions = TemporalOHLCV(dataset,
                                            timeframe=self.timeframe, policy=self.temporal_policy)
                    self._temporal_histories[symbol] = versions
                received = self._temporal_clock()
                if fetched.attrs.get("temporal_identity"):
                    versions.import_identity(fetched)
                else:
                    versions.ingest(fetched, observed_at=received, local_receipt=True,
                                    source_reference="live_adapter_local_receipt")
                if self.temporal_policy.mode == "strict":
                    fetched = versions.as_of(received).tail(self.lookback)
                self.temporal_identity[symbol] = versions.identity
                self.temporal_audit["symbols"][symbol] = versions.audit
                if fetched.empty:
                    self.failed_symbols[symbol] = "no_temporally_available_bars"
                    continue
            else:
                self.temporal_audit["symbols"][symbol] = compatibility_audit()
            fetched_latest = pd.Timestamp(fetched.index[-1])
            previous_latest = self._last_fetched_latest.get(symbol)
            if previous_latest is not None and fetched_latest < previous_latest:
                self.regressed_symbols.add(symbol)
            self._last_fetched_latest[symbol] = max(fetched_latest, previous_latest or fetched_latest)
            current = self._raw_data_map.get(symbol)
            if current is None and symbol in self.data_map:
                # Adopt externally primed source history without derived columns.
                current = normalize_market_frame(self.data_map[symbol]).reindex(columns=fetched.columns)
            if current is None or current.empty:
                combined = fetched.iloc[-self.lookback:].copy()
            else:
                combined = pd.concat([current, fetched], copy=False)
                combined = combined.loc[
                    ~combined.index.duplicated(keep="last")
                ]
                if not combined.index.is_monotonic_increasing:
                    combined = combined.sort_index()
                combined = combined.iloc[-self.lookback:].copy()
            existing = self.data_map.get(symbol)
            # 同时校验原始缓存与公开视图，只有两者均未变化才复用派生列。
            # 提供商修订历史值或调用方改写视图，都必须触发重新计算。
            if (symbol in self._raw_data_map and current is not None and current.equals(combined)
                    and existing is not None and set(combined.columns).issubset(existing.columns)
                    and existing.loc[:, combined.columns].equals(combined)):
                reused += 1
                continue
            self._raw_data_map[symbol] = combined.copy(deep=True)
            _calculate_indicators(combined)
            self.data_map[symbol] = combined
            recomputed += 1
        self.refresh_metrics = {
            "workers": self.max_workers, "symbols": len(self.symbols),
            "fetch_seconds": fetched_at - started,
            "compute_seconds": perf_counter() - fetched_at,
            "total_seconds": perf_counter() - started,
            "recomputed_symbols": recomputed, "reused_symbols": reused,
            "failed_symbols": dict(self.failed_symbols),
            "symbol_fetch_seconds": {symbol: elapsed for symbol, _, elapsed, _ in results},
            "temporal": self.temporal_audit,
        }
        return self.data_map

    def poll(self, now: datetime) -> list[MarketDataSlice]:
        self.refresh()
        eligible: Dict[str, pd.DataFrame] = {}
        timeline = pd.DatetimeIndex([])
        for symbol, frame in self.data_map.items():
            if self.temporal_policy.mode == "strict" and symbol in self._temporal_histories:
                frame = self._temporal_histories[symbol].as_of(now).tail(self.lookback)
                if not frame.empty:
                    _calculate_indicators(frame)
            closed = closed_bars(frame, self.timeframe, now, self.close_grace_seconds).copy()
            eligible[symbol] = closed
            watermark = self._watermarks.get(symbol)
            unseen = closed.index if watermark is None else closed.index[closed.index > watermark]
            timeline = timeline.union(unseen)

        events: list[MarketDataSlice] = []
        for timestamp in timeline.sort_values():
            bars = {
                symbol: frame.loc[timestamp]
                for symbol, frame in eligible.items()
                if timestamp in frame.index
                and (symbol not in self._watermarks or timestamp > self._watermarks[symbol])
            }
            if not bars:
                continue
            events.append(
                MarketDataSlice(
                    timestamp=timestamp,
                    bars=bars,
                    histories=eligible,
                    timeframe=self.timeframe,
                    source="live",
                )
            )
            for symbol in bars:
                self._watermarks[symbol] = timestamp
        return events

    def stream(self) -> Iterable[MarketDataSlice]:
        raise RuntimeError("LiveMarketDataAdapter is polling-based; call poll(now)")

