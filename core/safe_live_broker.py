"""Safety-enforcing, persistent broker used by the live entry point."""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.domain import OrderErrorCode, OrderSubmissionResult
from core.live_broker import LiveBroker
from core.logger import get_logger
from core.order_store import OrderStore
from core.portfolio import Portfolio


logger = get_logger(__name__)


class SafeLiveBroker(LiveBroker):
    """LiveBroker whose public constructor cannot accept plaintext credentials."""

    def __init__(
        self,
        portfolio: Portfolio,
        safety_guard: Any,
        exchange_id: str = "binance",
        sandbox: bool = True,
        market_type: str = "spot",
        base_currency: str = "USDT",
        exchange_options: Optional[Dict[str, Any]] = None,
        order_store: Optional[OrderStore] = None,
        order_store_path: str = "reports/live_orders.db",
        account_id: Optional[str] = None,
        position_mode: str = "one_way",
    ) -> None:
        self.safety_guard = safety_guard
        super().__init__(
            portfolio=portfolio,
            exchange_id=exchange_id,
            sandbox=sandbox,
            market_type=market_type,
            base_currency=base_currency,
            exchange_options=exchange_options,
            order_store=order_store or OrderStore(order_store_path),
            account_id=account_id,
            position_mode=position_mode,
        )

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float = None,
        order_type: str = "market",
        timestamp: Any = None,
        slippage: float = 0.0,
        strategy_id: str = "Manual",
        exit_reason: str = "signal",
        time_in_force: Optional[str] = None,
        position_side: Optional[str] = None,
        reduce_only: Optional[bool] = None,
        sequence: int = 0,
    ) -> OrderSubmissionResult:
        intent = self._build_intent(
            symbol, side, qty, price, order_type, timestamp, strategy_id,
            time_in_force, position_side, reduce_only, sequence,
        )
        # Idempotent replay must not reserve the daily risk limit a second time.
        if self.order_store.get(intent.client_order_id) is None:
            try:
                self.safety_guard.assert_order_allowed(symbol, side, intent.requested_qty, price)
            except ValueError as exc:
                logger.critical(
                    "Order blocked by safety policy client_order_id=%s reason=%s",
                    intent.client_order_id, exc,
                )
                return self.record_local_rejection(
                    intent, str(exc), OrderErrorCode.SAFETY_POLICY
                )
        return super().submit_order(
            symbol=symbol, side=side, qty=qty, price=price,
            order_type=order_type, timestamp=timestamp, slippage=slippage,
            strategy_id=strategy_id, exit_reason=exit_reason,
            time_in_force=time_in_force, position_side=position_side,
            reduce_only=reduce_only, sequence=sequence,
        )
