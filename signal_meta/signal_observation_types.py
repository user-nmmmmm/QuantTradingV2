"""Immutable P0 research facts. None of these contracts authorises an order."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Mapping

import pandas as pd

from core.timeframes import as_utc_timestamp, timeframe_delta


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def finite(value: Any):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def iso(value: Any) -> str:
    return as_utc_timestamp(value).isoformat()


@dataclass(frozen=True)
class ObservationPolicy:
    enabled: bool = False
    horizons: tuple[int, ...] = (1, 3, 5, 20)
    reference_notional: float = 1000.0
    ghost_horizon: int = 5
    ghost_capital: float = 10000.0
    schema: str = "signal_observation/v1"

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("observation enabled must be boolean")
        if (not self.horizons or any(type(n) is not int or n < 1 for n in self.horizons)
                or len(set(self.horizons)) != len(self.horizons)):
            raise ValueError("horizons must contain distinct positive integers")
        if type(self.ghost_horizon) is not int or self.ghost_horizon < 1:
            raise ValueError("ghost_horizon must be a positive integer")
        for value in (self.reference_notional, self.ghost_capital):
            if finite(value) is None or value <= 0:
                raise ValueError("research notional and capital must be positive and finite")

    @classmethod
    def from_mapping(cls, value: Mapping | None):
        data = dict(value or {})
        if "horizons" in data:
            data["horizons"] = tuple(data["horizons"])
        return cls(**data)


@dataclass(frozen=True)
class ContextSnapshot:
    bar_time: str
    available_at: str
    timeframe: str
    market_state: str
    health_status: str
    health_risk_multiplier: float
    account_action: str
    account_risk_multiplier: float
    position_qty: float
    entry_pending: bool
    features_json: str
    snapshot_version: str

    def to_dict(self):
        data = asdict(self)
        data["features"] = json.loads(data.pop("features_json"))
        return data


@dataclass(frozen=True)
class SignalCandidateEvent:
    candidate_id: str
    timestamp: str
    symbol: str
    strategy: str
    direction: str
    native_score: float
    reference_price: float
    signal_version: str
    signal_json: str
    context: ContextSnapshot
    estimated_round_trip_cost_bps: float
    schema: str = "signal_candidate/v1"

    def to_dict(self):
        data = asdict(self)
        data["signal"] = json.loads(data.pop("signal_json"))
        data["context"] = self.context.to_dict()
        return data


def context_features(frame: pd.DataFrame) -> dict[str, Any]:
    """Trailing descriptors, no fitted buckets or whole-sample normalisation."""
    close = frame["close"].astype(float)
    row = frame.iloc[-1]
    diff = close.diff().abs()
    er_den = diff.iloc[-10:].sum() if len(close) > 10 else 0.0
    returns = close.pct_change(fill_method=None)
    short_vol = returns.iloc[-8:].std() if len(close) >= 9 else None
    long_vol = returns.iloc[-48:].std() if len(close) >= 49 else None
    prev = close.shift(1)
    tr = pd.concat([frame.high-frame.low, (frame.high-prev).abs(),
                    (frame.low-prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    return {
        "efficiency_ratio_10": finite(abs(close.iloc[-1]-close.iloc[-11]) / er_den)
        if er_den > 0 else None,
        "volatility_ratio_8_48": finite(short_vol / long_vol)
        if long_vol is not None and long_vol > 0 else None,
        "atr_pct_14": finite(atr / close.iloc[-1]),
        "adx_14": finite(row.get("ADX_14")),
        "return_12": finite(close.iloc[-1] / close.iloc[-13]-1) if len(close) > 12 else None,
        "turnover": finite(row.get("volume", 0) * close.iloc[-1]),
        "spread_bps": finite(row.get("spread_bps")),
        "last_input_bar": iso(frame.index[-1]),
        "available_history_bars": len(frame),
    }


def close_time(timestamp: Any, timeframe: str) -> str:
    # MarketDataSlice timestamps label bar OPENS in both adapters.
    return iso(as_utc_timestamp(timestamp) + timeframe_delta(timeframe))
