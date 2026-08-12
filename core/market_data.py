"""Market-data adapters for the shared trading runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, List

import pandas as pd

from core.indicators import Indicators
from core.runtime import MarketDataSlice
from core.timeframes import closed_bars


def normalize_market_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize all adapter boundaries to sorted, unique, UTC-naive indices."""

    if df is None or df.empty:
        return pd.DataFrame()
    normalized = df.copy()
    normalized.index = pd.to_datetime(normalized.index, errors="coerce")
    normalized = normalized.loc[~pd.isna(normalized.index)].copy()
    if normalized.empty:
        return normalized
    if normalized.index.tz is not None:
        normalized.index = normalized.index.tz_convert("UTC").tz_localize(None)
    return normalized[~normalized.index.duplicated(keep="last")].sort_index()


class HistoricalMarketDataAdapter:
    """Turns fixed OHLCV frames into a union-of-real-bars event timeline."""

    def __init__(
        self,
        data_map: Dict[str, pd.DataFrame],
        *,
        timeframe: str = "unknown",
        calculate_indicators: bool = True,
    ) -> None:
        self.timeframe = timeframe
        self.data_map: Dict[str, pd.DataFrame] = {}
        for symbol, frame in data_map.items():
            prepared = normalize_market_frame(frame)
            if prepared.empty:
                continue
            if calculate_indicators:
                Indicators.calculate_all(prepared)
            self.data_map[symbol] = prepared
        self._positions: Dict[str, Dict[pd.Timestamp, int]] = {
            symbol: {ts: i for i, ts in enumerate(frame.index)}
            for symbol, frame in self.data_map.items()
        }

    @property
    def timestamps(self) -> pd.DatetimeIndex:
        timeline = pd.DatetimeIndex([])
        for frame in self.data_map.values():
            timeline = timeline.union(frame.index)
        return timeline.sort_values()

    def stream(self) -> Iterable[MarketDataSlice]:
        for timestamp in self.timestamps:
            bars: Dict[str, pd.Series] = {}
            positions: Dict[str, int] = {}
            for symbol, frame in self.data_map.items():
                pos = self._positions[symbol].get(timestamp)
                if pos is not None:
                    bars[symbol] = frame.iloc[pos]
                    positions[symbol] = pos
            yield MarketDataSlice(
                timestamp=timestamp,
                bars=bars,
                histories=self.data_map,
                positions=positions,
                timeframe=self.timeframe,
                source="historical",
            )


class LiveMarketDataAdapter:
    """Polls a fetcher and emits each newly closed bar once per process."""

    def __init__(
        self,
        symbols: List[str],
        fetcher,
        *,
        timeframe: str = "1d",
        lookback: int = 100,
        close_grace_seconds: float = 2.0,
    ) -> None:
        self.symbols = list(symbols)
        self.fetcher = fetcher
        self.timeframe = timeframe
        self.lookback = max(int(lookback), 1)
        self.close_grace_seconds = close_grace_seconds
        self.data_map: Dict[str, pd.DataFrame] = {}
        self._watermarks: Dict[str, pd.Timestamp] = {}
        self._last_fetched_latest: Dict[str, pd.Timestamp] = {}
        self.regressed_symbols: set[str] = set()

    def refresh(self) -> Dict[str, pd.DataFrame]:
        self.regressed_symbols = set()
        for symbol in self.symbols:
            fetched = normalize_market_frame(
                self.fetcher.fetch_ccxt(
                    symbol, timeframe=self.timeframe, limit=self.lookback
                )
            )
            if fetched.empty:
                continue
            fetched_latest = pd.Timestamp(fetched.index[-1])
            previous_latest = self._last_fetched_latest.get(symbol)
            if previous_latest is not None and fetched_latest < previous_latest:
                self.regressed_symbols.add(symbol)
            self._last_fetched_latest[symbol] = fetched_latest
            current = normalize_market_frame(self.data_map.get(symbol, pd.DataFrame()))
            combined = fetched if current.empty else pd.concat([current, fetched])
            combined = normalize_market_frame(combined)
            combined = combined.iloc[-self.lookback:]
            Indicators.calculate_all(combined)
            self.data_map[symbol] = combined
        return self.data_map

    def poll(self, now: datetime) -> list[MarketDataSlice]:
        self.refresh()
        eligible: Dict[str, pd.DataFrame] = {}
        timeline = pd.DatetimeIndex([])
        for symbol, frame in self.data_map.items():
            closed = closed_bars(frame, self.timeframe, now, self.close_grace_seconds)
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

