"""Explicit account-mode and margin contracts for backtests.

Phase 3 removes the old ambiguity where spot cash flows, borrowed spot and
perpetual margin were represented by the same ``cash`` number.  The enum and
snapshots in this module are deliberately small and serialisable so reports,
tests and live adapters can share the same vocabulary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict


class AccountMode(str, Enum):
    SPOT = "spot"
    SPOT_MARGIN = "spot_margin"
    PERPETUAL = "perpetual"

    @property
    def uses_margin(self) -> bool:
        return self in {AccountMode.SPOT_MARGIN, AccountMode.PERPETUAL}

    @property
    def allows_short(self) -> bool:
        return self is not AccountMode.SPOT


@dataclass(frozen=True)
class MarginSnapshot:
    timestamp: Any
    account_mode: str
    equity: float
    gross_notional: float
    initial_margin: float
    maintenance_margin: float
    available_margin: float
    margin_ratio: float
    liquidation_required: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FinancingEntry:
    timestamp: Any
    symbol: str
    kind: str
    rate: float
    notional: float
    amount: float
    source: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

