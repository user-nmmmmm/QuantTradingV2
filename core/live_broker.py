import ccxt
import os
from datetime import datetime
from typing import Any, Dict, Optional

from core.logger import get_logger
from core.portfolio import Portfolio

logger = get_logger(__name__)

"""
LiveBroker（实盘 Broker）模块

本模块是回测 Broker 的“实盘版本”，通过 CCXT 直接与交易所交互：
- 下单：create_order（market/limit 等）
- 同步账户：fetch_balance（以及未来可扩展 fetch_positions）

重要说明：
- 实盘环境无法像回测一样使用“下一根 bar 的 OHLC”确定是否成交，成交由交易所撮合决定。
- 本实现为了简化，主要聚焦：
  1) 下单打通
  2) 通过 sync() 将余额/持仓同步回 Portfolio，便于引擎计算权益与风控校验

风险提示：
- 默认 exchange options 中提到 futures/spot 的选择非常关键；不同交易所的字段含义也不同。
- 若用于真实资金，请先在 sandbox 模式验证，并补齐异常处理、重试与风控保护。
"""


class LiveBroker:
    """
    Broker implementation for Live Trading using CCXT.
    Interacts directly with the exchange.
    """

    def __init__(
        self,
        portfolio: Portfolio,
        exchange_id: str = "binance",
        api_key: str = None,
        secret: str = None,
        sandbox: bool = False,
        market_type: str = "spot",
        base_currency: str = "USDT",
        password: Optional[str] = None,
        exchange_options: Optional[Dict[str, Any]] = None,
    ):
        """
        参数：
        - portfolio：本地账户快照（用于引擎层统一计算权益/持仓）
        - exchange_id：ccxt 交易所标识（默认 binance）
        - api_key/secret：可通过参数传入，或从环境变量 EXCHANGE_API_KEY/EXCHANGE_SECRET 读取
        - sandbox：是否启用测试网（强烈建议先 True）
        """
        self.portfolio = portfolio
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.base_currency = base_currency

        # Initialize Exchange
        exchange_class = getattr(ccxt, exchange_id)
        options = dict(exchange_options or {})
        options.setdefault("defaultType", market_type)
        config = {
            "apiKey": api_key or os.getenv("EXCHANGE_API_KEY"),
            "secret": secret or os.getenv("EXCHANGE_SECRET"),
            "password": password or os.getenv("EXCHANGE_PASSWORD"),
            "enableRateLimit": True,
            "options": options,
        }

        self.exchange = exchange_class(config)
        if hasattr(self.exchange, "session") and hasattr(self.exchange.session, "trust_env"):
            self.exchange.session.trust_env = True
        if sandbox:
            self.exchange.set_sandbox_mode(True)
            logger.info(
                "Initialized %s in SANDBOX mode with market_type=%s",
                exchange_id,
                market_type,
            )
        else:
            logger.info(
                "Initialized %s in LIVE mode with market_type=%s",
                exchange_id,
                market_type,
            )

        self.trades = []

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _extract_avg_price(self, payload: Dict[str, Any]) -> float:
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        for candidate in (
            payload.get("entryPrice"),
            payload.get("avgPrice"),
            payload.get("average"),
            info.get("entryPrice"),
            info.get("avgPrice"),
        ):
            price = self._as_float(candidate, default=0.0)
            if price > 0:
                return price
        return 0.0

    def _extract_qty(self, payload: Dict[str, Any]) -> float:
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        for candidate in (
            payload.get("contracts"),
            payload.get("positionAmt"),
            payload.get("qty"),
            payload.get("size"),
            info.get("positionAmt"),
            info.get("contracts"),
            info.get("size"),
        ):
            qty = self._as_float(candidate, default=0.0)
            if qty != 0:
                return qty
        return 0.0

    def _sync_spot_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        positions: Dict[str, Dict[str, float]] = {}
        totals = balance.get("total", {}) if isinstance(balance, dict) else {}

        for currency, amount in totals.items():
            qty = self._as_float(amount, default=0.0)
            if currency == self.base_currency or qty == 0:
                continue
            positions[f"{currency}/{self.base_currency}"] = {
                "qty": qty,
                "avg_price": 0.0,
            }

        return positions

    def _sync_derivatives_positions(
        self, balance: Dict[str, Any]
    ) -> Dict[str, Dict[str, float]]:
        fetch_positions = getattr(self.exchange, "fetch_positions", None)
        raw_positions = []

        if callable(fetch_positions):
            try:
                raw_positions = fetch_positions() or []
            except Exception as exc:
                logger.warning(
                    "fetch_positions failed for %s; falling back to balance payload: %s",
                    self.exchange_id,
                    exc,
                )

        if not raw_positions and isinstance(balance, dict):
            raw_positions = balance.get("positions", []) or balance.get("info", {}).get(
                "positions", []
            )

        positions: Dict[str, Dict[str, float]] = {}
        for raw_position in raw_positions:
            symbol = raw_position.get("symbol") or raw_position.get("info", {}).get(
                "symbol"
            )
            qty = self._extract_qty(raw_position)
            if not symbol or qty == 0:
                continue
            positions[symbol] = {
                "qty": qty,
                "avg_price": self._extract_avg_price(raw_position),
            }

        return positions

    def sync(self):
        """
        将交易所账户状态同步到本地 Portfolio（余额与持仓快照）。

        当前实现策略：
        - 余额：使用 USDT free 作为 cash
        - 持仓：对 spot 场景，遍历 balance["total"] 中非 USDT 且 amount > 0 的币种作为持仓
          avg_price 无法直接得到，这里置为 0.0（若要精确 PnL，需要自行记录成交回放）

        备注：
        - futures/杠杆账户的仓位获取通常需要 fetch_positions()，不同交易所差异较大
        """
        try:
            balance = self.exchange.fetch_balance()
            free_balances = balance.get("free", {}) if isinstance(balance, dict) else {}
            total_balances = (
                balance.get("total", {}) if isinstance(balance, dict) else {}
            )

            cash = self._as_float(
                free_balances.get(self.base_currency),
                default=self._as_float(total_balances.get(self.base_currency)),
            )
            self.portfolio.cash = cash

            if self.market_type in {"future", "futures", "swap", "margin"}:
                positions = self._sync_derivatives_positions(balance)
                if not positions:
                    logger.warning(
                        "No derivative positions returned; falling back to spot-style balances"
                    )
                    positions = self._sync_spot_positions(balance)
            else:
                positions = self._sync_spot_positions(balance)

            self.portfolio.positions = positions
            logger.info(
                "Synced portfolio cash=%.2f positions=%s market_type=%s",
                self.portfolio.cash,
                len(self.portfolio.positions),
                self.market_type,
            )

        except Exception as e:
            logger.error(f"Failed to sync portfolio: {e}")

    def submit_order(
        self,
        symbol: str,
        side: str,  # 'buy', 'sell', 'short', 'cover'
        qty: float,
        price: float = None,
        order_type: str = "market",
        timestamp: Any = None,
        slippage: float = 0.0,  # Ignored in live
        strategy_id: str = "Manual",
        exit_reason: str = "signal",
    ) -> None:
        """
        向交易所提交订单（实盘撮合）。

        方向映射：
        - 本项目策略侧使用 buy/sell/short/cover 四种动作
        - CCXT 标准 side 为 buy/sell：
          - short -> sell（开空）
          - cover -> buy（平空/回补）

        注意：
        - slippage 参数在实盘里不适用（成交价格由订单簿决定），这里忽略
        - 若使用合约账户，平仓单可能需要 reduceOnly 等参数（此处留有扩展位）
        """
        if qty <= 0:
            logger.warning(f"Order rejected: Qty {qty} <= 0")
            return

        if self.market_type not in {"future", "futures", "swap", "margin"} and side in {
            "short",
            "cover",
        }:
            logger.warning(
                "Order rejected: %s is not supported for market_type=%s",
                side,
                self.market_type,
            )
            return

        # Map side/type to CCXT
        # CCXT sides: 'buy', 'sell'
        # Our sides: 'buy', 'sell' (long close), 'short', 'cover' (short close)

        ccxt_side = side
        if side in ["short"]:
            ccxt_side = "sell"
        elif side in ["cover"]:
            ccxt_side = "buy"

        # If using Futures, we might need 'reduceOnly' for close orders
        params = {}
        if side in ["sell", "cover"]:
            # This is a closing trade
            # params['reduceOnly'] = True # Only for futures
            pass

        ccxt_type = order_type.lower()

        try:
            logger.info(
                f"Submitting Order: {symbol} {ccxt_side} {qty} {ccxt_type} @ {price}"
            )

            order = self.exchange.create_order(
                symbol=symbol,
                type=ccxt_type,
                side=ccxt_side,
                amount=qty,
                price=price,
                params=params,
            )

            logger.info(f"Order Executed: {order['id']} - Status: {order['status']}")

            # Record trade locally
            self.trades.append(
                {
                    "id": order["id"],
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": order.get("average") or order.get("price") or price,
                    "timestamp": datetime.now(),
                    "strategy_id": strategy_id,
                    "exit_reason": exit_reason,
                }
            )

            # Sync portfolio after trade
            self.sync()

        except Exception as e:
            logger.error(f"Order Failed: {e}")
