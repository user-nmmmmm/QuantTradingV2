"""Account fact sync: cash and position reconciliation against the exchange.

Split out of core/live_broker.py (A4) — see docs/architecture_review.md. See
core/live_broker_submission.py's module docstring for why this is a mixin
rather than a standalone collaborator object.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.domain import SyncResult
from core.logger import get_logger

# Same logger name as core.live_broker (logging.getLogger caches by name, so
# this is the identical object) -- tests use assertLogs("core.live_broker")
# and must keep catching records logged from this mixin too.
logger = get_logger("core.live_broker")

DERIVATIVE_TYPES = {"future", "futures", "swap", "margin"}


class AccountSyncMixin:
    """Fetch and apply the exchange's cash/position facts to the portfolio.

    Expects ``self`` to carry ``portfolio``, ``exchange``, ``exchange_boundary``,
    ``market_type``, ``base_currency``, ``last_account_sync_at``, plus the
    shared plumbing on ``LiveBroker`` itself (``_retry_exchange_call``,
    ``_alert``, ``_clock``, ``_as_float``).
    """

    def sync(self) -> SyncResult:
        try:
            next_cash, positions = self._retry_exchange_call(self._load_portfolio_fact)
            self.portfolio.cash = next_cash
            self.portfolio.positions = positions
            self.last_account_sync_at = self._clock()
            return SyncResult(True, self.last_account_sync_at)
        except Exception as exc:
            logger.exception("Failed to sync portfolio category=%s", type(exc).__name__)
            self._alert("error", "account_sync_failed", {
                "error": type(exc).__name__,
                "retry_attempts": self.retry_max_attempts,
            })
            return SyncResult(False, self._clock(), type(exc).__name__)

    def _load_portfolio_fact(self):
        balance = self.exchange.fetch_balance()
        free = balance.get("free", {}) if isinstance(balance, dict) else {}
        total = balance.get("total", {}) if isinstance(balance, dict) else {}
        next_cash = self._as_float(
            total.get(self.base_currency), self._as_float(free.get(self.base_currency))
        )
        positions = (
            self._sync_derivatives_positions(balance)
            if self.market_type in DERIVATIVE_TYPES
            else self._sync_spot_positions(balance)
        )
        return next_cash, positions

    def _sync_spot_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        positions: Dict[str, Dict[str, float]] = {}
        for currency, amount in (balance.get("total", {}) or {}).items():
            qty = self._as_float(amount)
            if currency != self.base_currency and qty != 0:
                positions[f"{currency}/{self.base_currency}"] = {"qty": qty, "avg_price": 0.0}
        return positions

    def _sync_derivatives_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        fetch_positions = getattr(self.exchange, "fetch_positions", None)
        info = balance.get("info", {}) if isinstance(balance, dict) else {}
        if callable(fetch_positions):
            # An explicit, even empty, response from the dedicated endpoint is
            # an authoritative fact: genuinely flat.
            raw_positions = fetch_positions() or []
        elif isinstance(balance, dict) and "positions" in balance:
            raw_positions = balance.get("positions") or []
        elif isinstance(info, dict) and "positions" in info:
            raw_positions = info.get("positions") or []
        else:
            # No source at all exposes derivative positions; treating this as
            # "flat" would let trading continue on an unverified fake empty
            # snapshot. Fail closed instead.
            raise ValueError(
                "exchange does not expose derivative positions via fetch_positions "
                "or balance payload; cannot verify account fact"
            )
        positions: Dict[str, Dict[str, float]] = {}
        for raw in raw_positions:
            parsed = self.exchange_boundary.position_parser.parse(raw)
            if parsed is None:
                continue
            symbol = parsed.symbol
            if symbol in positions:
                raise ValueError(f"multiple derivative position legs are unsupported for {symbol}")
            positions[symbol] = {
                "qty": float(parsed.qty),
                "avg_price": float(parsed.average_entry_price),
            }
        return positions

    def _extract_qty(self, payload: Dict[str, Any]) -> float:
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        direction = self._position_direction(payload, info)

        for value in (payload.get("positionAmt"), info.get("positionAmt")):
            qty = self._as_float(value)
            if qty == 0:
                continue
            if direction is not None and (qty > 0) != (direction > 0):
                raise ValueError("derivative position side conflicts with signed quantity")
            return qty

        for value in (
            payload.get("contracts"),
            payload.get("qty"),
            payload.get("size"),
            info.get("contracts"),
            info.get("qty"),
            info.get("size"),
        ):
            magnitude = abs(self._as_float(value))
            if magnitude == 0:
                continue
            if direction is None:
                raise ValueError("derivative position direction is unavailable")
            return magnitude * direction
        return 0.0

    @staticmethod
    def _position_direction(
        payload: Dict[str, Any], info: Dict[str, Any]
    ) -> Optional[float]:
        for value in (
            payload.get("side"),
            payload.get("positionSide"),
            info.get("side"),
            info.get("positionSide"),
        ):
            normalized = str(value or "").strip().lower()
            if normalized in {"long", "buy"}:
                return 1.0
            if normalized in {"short", "sell"}:
                return -1.0
        return None

    def _extract_avg_price(self, payload: Dict[str, Any]) -> float:
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        for value in (payload.get("entryPrice"), payload.get("avgPrice"), payload.get("average"), info.get("entryPrice"), info.get("avgPrice")):
            price = self._as_float(value)
            if price > 0:
                return price
        return 0.0
