"""Structured data/system health assessment for fail-closed live risk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import pandas as pd

from core.clock import as_utc
from core.timeframes import timeframe_delta


@dataclass(frozen=True)
class DataHealthPolicy:
    """Freshness and timeline integrity limits for facts used by new risk."""

    market_data_max_age_intervals: float = 1.5
    account_sync_max_age: timedelta = timedelta(minutes=2)
    order_sync_max_age: timedelta = timedelta(minutes=2)
    future_tolerance: timedelta = timedelta(seconds=5)
    gap_tolerance_intervals: float = 1.5
    gap_lookback_bars: int = 200

    def __post_init__(self) -> None:
        if self.market_data_max_age_intervals <= 0:
            raise ValueError("market_data_max_age_intervals must be positive")
        if self.account_sync_max_age.total_seconds() <= 0:
            raise ValueError("account_sync_max_age must be positive")
        if self.order_sync_max_age.total_seconds() <= 0:
            raise ValueError("order_sync_max_age must be positive")
        if self.gap_tolerance_intervals <= 1:
            raise ValueError("gap_tolerance_intervals must be greater than one")
        if self.gap_lookback_bars < 2:
            raise ValueError("gap_lookback_bars must be at least two")


@dataclass(frozen=True)
class HealthReason:
    code: str
    category: str
    subject: str
    message: str
    observed_at: Optional[datetime] = None
    age_seconds: Optional[float] = None
    limit_seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "subject": self.subject,
            "message": self.message,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "age_seconds": self.age_seconds,
            "limit_seconds": self.limit_seconds,
        }


@dataclass(frozen=True)
class HealthAssessment:
    assessed_at: datetime
    reasons: Sequence[HealthReason] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return not self.reasons

    @property
    def allows_new_risk(self) -> bool:
        return self.healthy

    @property
    def reason_codes(self) -> list[str]:
        return [reason.code for reason in self.reasons]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "assessed_at": self.assessed_at.isoformat(),
            "healthy": self.healthy,
            "allows_new_risk": self.allows_new_risk,
            "reason_codes": self.reason_codes,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


class DataHealthMonitor:
    """Stateful detector for freshness, gaps, future facts and regressions."""

    def __init__(self, policy: Optional[DataHealthPolicy] = None) -> None:
        self.policy = policy or DataHealthPolicy()
        self._latest_market_time: Dict[str, datetime] = {}

    @staticmethod
    def _reason(code, category, subject, message, *, observed_at=None, age=None, limit=None):
        return HealthReason(
            code=code, category=category, subject=subject, message=message,
            observed_at=observed_at,
            age_seconds=max(age.total_seconds(), 0.0) if age is not None else None,
            limit_seconds=limit.total_seconds() if limit is not None else None,
        )

    def assess(
        self, *, now: datetime, symbols: Iterable[str], timeframe: str,
        data_map: Mapping[str, pd.DataFrame],
        account_synced_at: Optional[datetime], order_synced_at: Optional[datetime],
        regressed_symbols: Iterable[str] = (),
    ) -> HealthAssessment:
        now = as_utc(now)
        interval = timeframe_delta(timeframe)
        market_limit = interval * self.policy.market_data_max_age_intervals
        reasons = []
        explicit_regressions = set(regressed_symbols)
        for symbol in symbols:
            subject = f"{symbol}@{timeframe}"
            frame = data_map.get(symbol)
            if frame is None or frame.empty:
                reasons.append(self._reason("MARKET_DATA_MISSING", "market_data", subject,
                                            f"no market data is available for {subject}"))
                continue
            valid = pd.DatetimeIndex(pd.to_datetime(frame.index, errors="coerce"))
            valid = valid[~pd.isna(valid)]
            if valid.empty:
                reasons.append(self._reason("MARKET_DATA_MISSING", "market_data", subject,
                                            f"market data for {subject} has no valid timestamps"))
                continue
            index = valid.tz_localize("UTC") if valid.tz is None else valid.tz_convert("UTC")
            future = index[index > pd.Timestamp(now + self.policy.future_tolerance)]
            if len(future):
                reasons.append(self._reason("MARKET_DATA_FUTURE", "market_data", subject,
                                            f"market data for {subject} is timestamped in the future",
                                            observed_at=future.max().to_pydatetime()))
            closed = index[
                index + interval <= pd.Timestamp(now + self.policy.future_tolerance)
            ]
            if closed.empty:
                reasons.append(self._reason(
                    "MARKET_DATA_NO_CLOSED_BAR", "market_data", subject,
                    f"market data for {subject} has no closed bar",
                ))
                continue
            latest_open = closed.max().to_pydatetime()
            latest_fact = latest_open + interval
            previous = self._latest_market_time.get(subject)
            if symbol in explicit_regressions or (previous is not None and latest_open < previous):
                reasons.append(self._reason("MARKET_DATA_REGRESSION", "market_data", subject,
                                            f"latest timestamp for {subject} moved backwards",
                                            observed_at=latest_open))
            self._latest_market_time[subject] = max(previous, latest_open) if previous else latest_open
            ordered = index.sort_values().unique()[-self.policy.gap_lookback_bars:]
            if len(ordered) >= 2:
                largest = max(ordered[1:] - ordered[:-1])
                gap_limit = interval * self.policy.gap_tolerance_intervals
                if largest > gap_limit:
                    reasons.append(self._reason("MARKET_DATA_GAP", "market_data", subject,
                                                f"market data for {subject} contains a gap of {largest}",
                                                limit=gap_limit))
            age = now - latest_fact
            if age > market_limit:
                reasons.append(self._reason("MARKET_DATA_STALE", "market_data", subject,
                                            f"latest closed fact for {subject} is stale",
                                            observed_at=latest_fact, age=age, limit=market_limit))
        reasons.extend(self._sync_reasons(now, "account", account_synced_at,
                                          self.policy.account_sync_max_age))
        reasons.extend(self._sync_reasons(now, "order", order_synced_at,
                                          self.policy.order_sync_max_age))
        return HealthAssessment(now, tuple(reasons))

    def _sync_reasons(self, now, kind, synced_at, limit):
        prefix = kind.upper()
        if synced_at is None:
            return [self._reason(f"{prefix}_SYNC_MISSING", f"{kind}_sync", kind,
                                 f"no successful {kind} synchronization has been observed")]
        observed = as_utc(synced_at)
        if observed > now + self.policy.future_tolerance:
            return [self._reason(f"{prefix}_SYNC_FUTURE", f"{kind}_sync", kind,
                                 f"{kind} synchronization timestamp is in the future",
                                 observed_at=observed)]
        age = now - observed
        if age > limit:
            return [self._reason(f"{prefix}_SYNC_STALE", f"{kind}_sync", kind,
                                 f"last successful {kind} synchronization is stale",
                                 observed_at=observed, age=age, limit=limit)]
        return []
