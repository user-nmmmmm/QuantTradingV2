"""Point-in-time universe membership and delisting rules (Phase 2 / T-2.12)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

import pandas as pd


@dataclass(frozen=True)
class UniverseMembership:
    symbol: str
    listed_at: pd.Timestamp
    delisted_at: Optional[pd.Timestamp] = None
    source: str = "user_supplied"

    def active_at(self, timestamp) -> bool:
        point = pd.Timestamp(timestamp)
        return self.listed_at <= point and (
            self.delisted_at is None or point < self.delisted_at
        )


class PointInTimeUniverse:
    """Membership is evaluated using only facts effective at each timestamp."""

    def __init__(self, memberships: Iterable[UniverseMembership]) -> None:
        self._memberships: Dict[str, UniverseMembership] = {}
        for item in memberships:
            if not item.symbol:
                raise ValueError("universe symbol is required")
            if item.delisted_at is not None and item.delisted_at <= item.listed_at:
                raise ValueError(f"delisted_at must be after listed_at for {item.symbol}")
            if item.symbol in self._memberships:
                raise ValueError(f"duplicate universe symbol: {item.symbol}")
            self._memberships[item.symbol] = item

    @classmethod
    def from_csv(cls, path: str | Path) -> "PointInTimeUniverse":
        frame = pd.read_csv(path)
        required = {"symbol", "listed_at"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"universe CSV missing columns: {sorted(missing)}")
        memberships = []
        for row in frame.to_dict("records"):
            listed = pd.Timestamp(row["listed_at"])
            raw_delisted = row.get("delisted_at")
            delisted = None if pd.isna(raw_delisted) or raw_delisted in (None, "") else pd.Timestamp(raw_delisted)
            memberships.append(UniverseMembership(
                symbol=str(row["symbol"]),
                listed_at=listed,
                delisted_at=delisted,
                source=str(row.get("source") or Path(path).name),
            ))
        return cls(memberships)

    def active_symbols(self, timestamp) -> list[str]:
        return sorted(
            symbol for symbol, item in self._memberships.items()
            if item.active_at(timestamp)
        )

    def apply(self, data_map: Mapping[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """Clip each series to its contemporaneous listing interval.

        Delisted symbols remain in historical periods before delisting; they are
        not deleted from the whole sample, which is the key survivorship rule.
        """

        filtered: Dict[str, pd.DataFrame] = {}
        for symbol, frame in data_map.items():
            membership = self._memberships.get(symbol)
            if membership is None:
                continue
            current = frame.loc[frame.index >= membership.listed_at]
            if membership.delisted_at is not None:
                current = current.loc[current.index < membership.delisted_at]
            if not current.empty:
                filtered[symbol] = current.copy()
        return filtered

    def to_manifest(self) -> Dict[str, object]:
        return {
            "mode": "point_in_time",
            "membership": {
                symbol: {
                    "listed_at": item.listed_at,
                    "delisted_at": item.delisted_at,
                    "source": item.source,
                }
                for symbol, item in sorted(self._memberships.items())
            },
            "delisting_rule": "bars at or after delisted_at are ineligible; prior history is retained",
        }


def static_universe_manifest(symbols: Iterable[str]) -> Dict[str, object]:
    return {
        "mode": "static",
        "symbols": sorted(symbols),
        "survivorship_bias_controlled": False,
        "warning": "No point-in-time universe file supplied",
    }


__all__ = ["PointInTimeUniverse", "UniverseMembership", "static_universe_manifest"]
