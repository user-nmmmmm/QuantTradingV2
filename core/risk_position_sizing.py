"""Position sizing and notional cap calculation for candidate entries.

Split out of core/risk.py (A4) — see docs/architecture_review.md. See
core/risk_circuit_breaker.py's module docstring for why this is a mixin
rather than a standalone collaborator object.
"""
from __future__ import annotations

from typing import Dict, Optional

from core.accounts import AccountMode
from core.logger import get_logger
from core.portfolio import Portfolio
from core.portfolio_risk import exposure_by_cluster
from core.risk_reservation import RiskReservationProjection

logger = get_logger(__name__)


class PositionSizingMixin:
    """Notional-cap and qty-clamping logic.

    Expects ``self`` to carry ``risk_per_trade``, ``max_leverage``,
    ``max_pos_size_pct``, ``min_entry_notional_pct``, ``liquidity_limit_pct``,
    ``CAP_SAFETY_MARGIN``, plus the ``risk_multiplier``/``_blocks_new_risk``/
    ``_health_allows_new_risk`` gates from ``CircuitBreakerMixin``, and sets
    ``self._last_entry_context``.
    """

    def calculate_position_size(self, equity: float, entry_price: float, stop_loss_price: float) -> float:
        """
        按“固定风险比例”计算仓位数量（Risk-based Sizing）。

        直觉：
        - 先确定单笔最多愿意亏多少钱：risk_amount = equity * risk_per_trade
        - 再用止损距离换算数量：
          qty = risk_amount / |entry_price - stop_loss_price|

        返回：
        - qty：建议下单数量；若参数不合法或熔断器触发，则返回 0。
        """
        if self._blocks_new_risk():
            return 0.0
        if not self._health_allows_new_risk():
            return 0.0

        if entry_price <= 0 or stop_loss_price <= 0:
            return 0.0

        risk_amount = equity * self.risk_per_trade
        price_diff = abs(entry_price - stop_loss_price)

        if price_diff == 0:
            return 0.0

        qty = risk_amount / price_diff * self.risk_multiplier
        return qty

    def calculate_position_size_fixed_pct(self, equity: float, entry_price: float, pct: float = 0.10) -> float:
        """
        按“固定资金占比”计算仓位数量（Notional-based Sizing）。

        直觉：
        - allocation = equity * pct
        - qty = allocation / entry_price

        适用场景：
        - 没有明确止损价（无法按风险比例定仓）
        - 快速原型/简化回测
        """
        if self._blocks_new_risk():
            return 0.0
        if not self._health_allows_new_risk():
            return 0.0

        if entry_price <= 0:
            return 0.0

        allocation = equity * pct
        return allocation / entry_price * self.risk_multiplier

    def _entry_notional_caps(
        self,
        portfolio: Portfolio,
        symbol: str,
        price: float,
        current_prices: Optional[Dict[str, float]],
        reserved_by_symbol: Dict[str, float],
        action: str,
    ) -> Optional[Dict[str, float]]:
        """一次开仓在各项风控约束下允许的**最大名义金额**（trade value）。

        这是 `check_entry_risk`（布尔闸门）与 `max_entry_notional`（削减到上限）
        的唯一口径来源——两者必须看同一份上限，否则会重演“双重限速口径”那类漂移。

        返回：
        - dict：{约束名: 该约束允许的最大 trade_value}（可能为负，表示已超限）
        - None：无法核实敞口（fail closed，调用方应拒绝交易）
        """
        if current_prices is None:
            if portfolio.positions:
                # No price map means existing positions can only be valued
                # via stale avg_price, which would understate/overstate real
                # exposure. Fail closed instead of risking a silent misread.
                logger.warning(
                    "Trade Rejected: no current prices supplied to value existing positions"
                )
                return None
            current_exposure = 0.0
            current_equity = portfolio.cash
        else:
            current_exposure = portfolio.get_total_exposure(current_prices)
            current_equity = portfolio.get_total_value(current_prices)

        if current_equity <= 0:
            return None

        reserved_exposure = sum(
            max(float(value), 0.0) for value in reserved_by_symbol.values()
        )
        reserved_symbol_value = max(float(reserved_by_symbol.get(symbol, 0.0)), 0.0)
        current_pos_value = abs(portfolio.get_position(symbol)["qty"]) * price

        caps: Dict[str, float] = {
            # Leverage: gross exposure (incl. other pending opens) vs equity.
            "leverage": (
                current_equity * self.max_leverage
                - current_exposure
                - reserved_exposure
            ),
            # Concentration: this symbol's own position vs equity.
            "concentration": (
                current_equity * self.max_pos_size_pct
                - current_pos_value
                - reserved_symbol_value
            ),
        }
        # SR3-2 (STR-P1-04): correlated coins are not independent positions.
        # These caps live here, in the one place both clamp_entry_qty and
        # check_entry_risk read, so the reduction and the gate cannot drift.
        cluster_policy = getattr(self, "cluster_policy", None)
        if cluster_policy is not None and cluster_policy.has_notional_caps:
            by_cluster = exposure_by_cluster(
                cluster_policy, portfolio, current_prices, reserved_by_symbol
            )
            cluster = cluster_policy.cluster_for(symbol)
            if cluster_policy.max_cluster_exposure_pct is not None:
                caps["cluster_exposure"] = (
                    current_equity * float(cluster_policy.max_cluster_exposure_pct)
                    - by_cluster.get(cluster, 0.0)
                )
            if cluster_policy.max_crypto_beta_exposure is not None:
                caps["crypto_beta_exposure"] = (
                    current_equity * float(cluster_policy.max_crypto_beta_exposure)
                    - sum(by_cluster.values())
                )

        if portfolio.account_mode is AccountMode.SPOT and action == "short":
            caps["account_mode"] = 0.0
        # Spot buys exchange cash for inventory. Leveraged account notional is
        # instead limited by reconciled initial margin.
        if portfolio.account_mode is AccountMode.SPOT and action != "short":
            caps["cash"] = portfolio.cash - reserved_exposure
        elif portfolio.account_mode.uses_margin:
            caps["initial_margin"] = (
                current_equity / portfolio.initial_margin_rate
                - current_exposure
                - reserved_exposure
            )

        self._last_entry_context = {
            "equity": current_equity,
            "exposure": current_exposure,
            "reserved": reserved_exposure,
            "position_value": current_pos_value,
        }
        return caps

    def max_entry_notional(
        self,
        portfolio: Portfolio,
        symbol: str,
        price: float,
        *,
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
        pending_open_notional: Optional[Dict[str, float]] = None,
        reservation_projection: Optional[RiskReservationProjection] = None,
        action: str = "buy",
    ) -> float:
        """本次开仓允许的最大名义金额（0 表示一分钱都不能开）。

        供上层把“算出来的仓位”**削减到风控上限**，而不是整单作废。
        风险定仓下 notional/equity = risk_per_trade ÷ (止损距离/价格)，止损越紧
        仓位越大——直接拒单会让风险最小的信号反而永远无法成交（见
        docs/backtest_assumptions.md 第 4 节）。削减后实际承担的风险只会更小。
        """
        if price <= 0:
            return 0.0
        if self._blocks_new_risk() or not self._health_allows_new_risk():
            return 0.0

        reserved_by_symbol = (
            reservation_projection.pending_notional(current_prices)
            if reservation_projection is not None else pending_open_notional or {}
        )
        caps = self._entry_notional_caps(
            portfolio, symbol, price, current_prices, reserved_by_symbol, action
        )
        if caps is None:
            return 0.0

        budget = min(caps.values())
        if current_volume > 0:
            budget = min(budget, current_volume * self.liquidity_limit_pct * price)
        if budget <= 0:
            return 0.0

        # Stay strictly inside the caps: qty*price can round just above budget,
        # which check_entry_risk would then reject as a limit breach.
        return budget * self.risk_multiplier * (1.0 - self.CAP_SAFETY_MARGIN)

    def clamp_entry_qty(
        self,
        portfolio: Portfolio,
        symbol: str,
        qty: float,
        price: float,
        *,
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
        pending_open_notional: Optional[Dict[str, float]] = None,
        reservation_projection: Optional[RiskReservationProjection] = None,
        action: str = "buy",
    ) -> float:
        """把 ``qty`` 削减到风控上限内；低于最小开仓门槛时返回 0。

        最小门槛（``min_entry_notional_pct``）用于避免“上限只剩一点点额度”时
        成交出无意义的尘埃仓位——那种仓位只会白付手续费。
        """
        if qty <= 0 or price <= 0:
            return 0.0

        allowed_notional = self.max_entry_notional(
            portfolio, symbol, price,
            current_volume=current_volume,
            current_prices=current_prices,
            pending_open_notional=pending_open_notional,
            reservation_projection=reservation_projection,
            action=action,
        )
        if allowed_notional <= 0:
            return 0.0

        clamped = min(qty, allowed_notional / price)
        equity = float(self._last_entry_context.get("equity", 0.0))
        if equity > 0:
            min_notional = equity * self.min_entry_notional_pct
            if clamped * price < min_notional:
                logger.info(
                    "Entry skipped: clamped notional %.2f below minimum %.2f "
                    "(%.2f%% of equity) for %s",
                    clamped * price, min_notional,
                    self.min_entry_notional_pct * 100, symbol,
                )
                return 0.0

        if clamped < qty:
            logger.info(
                "Entry clamped to risk limits: %s qty %.8f -> %.8f "
                "(notional %.2f -> %.2f)",
                symbol, qty, clamped, qty * price, clamped * price,
            )
        return clamped
