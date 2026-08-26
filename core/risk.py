from dataclasses import replace
from enum import Enum
from typing import Dict, Optional, Tuple

from core.accounts import AccountMode
from core.domain import OrderIntent, RiskDecision, RiskReservation
from core.events import TradingEventPipeline, stable_uuid5
from core.portfolio import Portfolio
from core.logger import get_logger
from core.risk_reservation import RiskReservationProjection

logger = get_logger(__name__)


class BreakerAction(str, Enum):
    NORMAL = "normal"
    REDUCE = "reduce"
    BLOCK_NEW = "block_new"
    LIQUIDATE = "liquidate"
    LOCKED = "locked"


_BREAKER_RANK = {
    BreakerAction.NORMAL: 0,
    BreakerAction.REDUCE: 1,
    BreakerAction.BLOCK_NEW: 2,
    BreakerAction.LIQUIDATE: 3,
    BreakerAction.LOCKED: 4,
}

"""
RiskManager（风险管理）模块

本模块对“准备下单”的交易做风控校验，并提供仓位计算工具。

核心职责：
- 仓位规模：按风险百分比（Risk%）或按固定资金占比（Fixed%）换算数量 qty
- 风险约束：流动性上限、杠杆上限、单标的集中度上限
- 熔断器：当出现超限回撤时，阻止后续开仓（将 qty 置 0 或直接拒单）

注意：
- 本 RiskManager 不直接下单；它只返回“是否允许交易/建议 qty”等结果。
- 回测与实盘共享同一套接口，但实盘更建议结合交易所真实仓位、保证金与订单簿深度做更严格校验。
"""

class RiskManager:
    # Shrink a clamped order this far below the cap so float rounding in
    # qty*price cannot push it back over the limit it was clamped to.
    CAP_SAFETY_MARGIN = 1e-9

    def __init__(
        self,
        risk_per_trade: float = 0.01,
        max_leverage: float = 3.0,
        max_drawdown_limit: float = 0.20,
        liquidity_limit_pct: float = 0.01, # Max 1% of bar volume
        max_pos_size_pct: float = 0.20, # Max 20% equity per position
        min_entry_notional_pct: float = 0.01, # Skip dust entries below 1% of equity
        daily_loss_limit: Optional[float] = None,
        portfolio_drawdown_reduce: Optional[float] = None,
        portfolio_drawdown_block: Optional[float] = None,
        portfolio_drawdown_liquidate: Optional[float] = None,
        portfolio_drawdown_lock: Optional[float] = None,
        reduced_risk_multiplier: float = 0.5,
    ):
        """
        初始化风控参数。

        参数：
        - risk_per_trade：单笔风险占权益比例（用于按止损距离定仓）
        - max_leverage：最大杠杆（gross exposure / equity）
        - max_drawdown_limit：日内回撤熔断阈值（示例：0.20 表示 -20%）
        - liquidity_limit_pct：流动性上限（单笔 qty 不超过当根 bar 成交量的比例）
        - max_pos_size_pct：单标的最大仓位占比（position value / equity）
        - min_entry_notional_pct：最小开仓名义金额占权益比例。仓位被削减到风控
          上限后若低于该门槛，则放弃这笔交易——避免只剩一点点额度时成交出
          纯付手续费的尘埃仓位。
        """
        self.risk_per_trade = risk_per_trade
        self.max_leverage = max_leverage
        self.max_drawdown_limit = max_drawdown_limit
        self.daily_loss_limit = (
            max_drawdown_limit if daily_loss_limit is None else float(daily_loss_limit)
        )
        self.portfolio_drawdown_reduce = (
            max_drawdown_limit * 0.50
            if portfolio_drawdown_reduce is None else float(portfolio_drawdown_reduce)
        )
        self.portfolio_drawdown_block = (
            max_drawdown_limit * 0.75
            if portfolio_drawdown_block is None else float(portfolio_drawdown_block)
        )
        self.portfolio_drawdown_liquidate = (
            max_drawdown_limit
            if portfolio_drawdown_liquidate is None else float(portfolio_drawdown_liquidate)
        )
        self.portfolio_drawdown_lock = (
            self.portfolio_drawdown_liquidate
            if portfolio_drawdown_lock is None else float(portfolio_drawdown_lock)
        )
        thresholds = [
            self.portfolio_drawdown_reduce,
            self.portfolio_drawdown_block,
            self.portfolio_drawdown_liquidate,
            self.portfolio_drawdown_lock,
        ]
        if not 0 < self.daily_loss_limit <= 1:
            raise ValueError("daily_loss_limit must be in (0, 1]")
        if any(value <= 0 or value > 1 for value in thresholds):
            raise ValueError("portfolio drawdown thresholds must be in (0, 1]")
        if thresholds != sorted(thresholds):
            raise ValueError("portfolio drawdown thresholds must be ordered")
        if not 0 < reduced_risk_multiplier <= 1:
            raise ValueError("reduced_risk_multiplier must be in (0, 1]")
        self.reduced_risk_multiplier = float(reduced_risk_multiplier)
        self.liquidity_limit_pct = liquidity_limit_pct
        self.max_pos_size_pct = max_pos_size_pct
        self.min_entry_notional_pct = min_entry_notional_pct

        self.circuit_breaker_triggered = False
        self.high_water_equity: Optional[float] = None
        self.portfolio_breaker_action = BreakerAction.NORMAL
        self.last_drawdown = 0.0
        self.breaker_audit: list[Dict[str, object]] = []
        self.health_assessment = None
        # Populated by _entry_notional_caps so the gate/clamp can report the
        # equity/exposure figures behind a decision without recomputing them.
        self._last_entry_context: Dict[str, float] = {}

    def set_health_assessment(self, assessment) -> None:
        """Install the latest live health fact used by opening-risk checks."""
        self.health_assessment = assessment

    def _health_allows_new_risk(self) -> bool:
        assessment = self.health_assessment
        allowed = assessment is None or bool(
            getattr(assessment, "allows_new_risk", False)
        )
        if not allowed:
            logger.critical(
                "New risk rejected by data/system health: %s",
                ",".join(getattr(assessment, "reason_codes", [])) or "UNHEALTHY",
            )
        return allowed

    def reset_daily_breaker(self) -> None:
        if self.circuit_breaker_triggered:
            logger.info("Resetting daily circuit breaker state")
        self.circuit_breaker_triggered = (
            _BREAKER_RANK[self.portfolio_breaker_action]
            >= _BREAKER_RANK[BreakerAction.BLOCK_NEW]
        )

    @property
    def breaker_action(self) -> BreakerAction:
        return self.portfolio_breaker_action

    @property
    def risk_multiplier(self) -> float:
        if self.portfolio_breaker_action is BreakerAction.REDUCE:
            return self.reduced_risk_multiplier
        if self._blocks_new_risk():
            return 0.0
        return 1.0

    def _blocks_new_risk(self) -> bool:
        return self.circuit_breaker_triggered or (
            _BREAKER_RANK[self.portfolio_breaker_action]
            >= _BREAKER_RANK[BreakerAction.BLOCK_NEW]
        )

    def manual_resume(
        self,
        *,
        approved_by: str,
        current_equity: float,
        rebase_high_water: bool = False,
    ) -> None:
        """Resume a persistent portfolio breaker only after named approval."""
        if not approved_by or not approved_by.strip():
            raise ValueError("approved_by is required for manual recovery")
        previous = self.portfolio_breaker_action
        if rebase_high_water or self.high_water_equity is None:
            self.high_water_equity = float(current_equity)
        self.portfolio_breaker_action = BreakerAction.NORMAL
        self.circuit_breaker_triggered = False
        self.last_drawdown = max(
            0.0,
            1.0 - float(current_equity) / max(float(self.high_water_equity), 1e-12),
        )
        self.breaker_audit.append({
            "event": "manual_resume",
            "approved_by": approved_by.strip(),
            "equity": float(current_equity),
            "previous_action": previous.value,
            "rebase_high_water": bool(rebase_high_water),
        })

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

    def check_entry_risk(
        self,
        portfolio: Portfolio,
        symbol: str,
        qty: float,
        price: float,
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
        pending_open_notional: Optional[Dict[str, float]] = None,
        reservation_projection: Optional[RiskReservationProjection] = None,
        action: str = "buy",
    ) -> bool:
        """
        校验一笔“拟进入的交易”是否违反风控规则。

        输入：
        - portfolio：当前账户状态（现金、持仓）
        - symbol/qty/price：拟下单信息（qty 为绝对数量，side 在上层决定）
        - current_volume：当前 bar 的成交量（用于流动性上限）
        - current_prices：symbol -> 当前价格（用于敞口/权益估算）
        - action：'buy' 或 'short'。做多需要用现金全额支付（现货无杠杆融资建模），
          做空暂不做现金占用校验（保证金/借券成本尚未建模，见 backtest_assumptions.md）。

        返回：
        - True：允许交易
        - False：拒绝交易

        注意：这是**闸门**，不做削减。上层应先用 `clamp_entry_qty` 把仓位削到
        上限内，本方法作为最后一道防线（含实盘路径）。
        """
        if self._blocks_new_risk():
            logger.warning("Trade Rejected: Circuit Breaker Active")
            return False
        if not self._health_allows_new_risk():
            return False

        if qty <= 0 or price <= 0:
            return False

        # 1. Liquidity Check
        if current_volume > 0:
            max_qty = current_volume * self.liquidity_limit_pct
            if qty > max_qty:
                logger.warning(f"Trade Rejected: Liquidity Limit. Qty {qty:.4f} > Max {max_qty:.4f} (1% of {current_volume})")
                return False

        trade_value = qty * price

        reserved_by_symbol = (
            reservation_projection.pending_notional(current_prices)
            if reservation_projection is not None else pending_open_notional or {}
        )
        caps = self._entry_notional_caps(
            portfolio, symbol, price, current_prices, reserved_by_symbol, action
        )
        if caps is None:
            return False

        context = self._last_entry_context
        equity = context["equity"]

        # 2. Cash Sufficiency Check
        if "cash" in caps and trade_value > caps["cash"]:
            logger.warning(
                f"Trade Rejected: Insufficient Cash. Need {trade_value:.2f}, "
                f"free cash {caps['cash']:.2f} (cash={portfolio.cash:.2f}, "
                f"reserved={context['reserved']:.2f})"
            )
            return False

        # 3. Leverage Check
        if trade_value > caps["leverage"]:
            projected_leverage = (
                context["exposure"] + context["reserved"] + trade_value
            ) / equity
            logger.warning(f"Trade Rejected: Leverage Limit. Projected {projected_leverage:.2f} > Max {self.max_leverage}")
            return False

        # 4. Concentration Check (Max Position Size)
        if trade_value > caps["concentration"]:
            new_pos_value = context["position_value"] + trade_value
            logger.warning(f"Trade Rejected: Concentration Limit. Symbol {symbol} would be {new_pos_value/equity:.1%} > Max {self.max_pos_size_pct:.1%}")
            return False

        return True


    def approve_and_create_intent(
        self,
        portfolio: Portfolio,
        intent: OrderIntent,
        *,
        reference_price: float,
        event_pipeline: TradingEventPipeline,
        reservation_projection: RiskReservationProjection,
        occurred_at,
        source: str = "risk",
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Tuple[RiskDecision, Optional[OrderIntent]]:
        """Evaluate and atomically create an approved intent and reservation."""
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        if intent.action not in {"buy", "short"} or intent.reduce_only:
            raise ValueError("risk reservations are only created for opening intents")
        decision_id = intent.risk_decision_id or str(
            stable_uuid5("risk-decision", intent.account, intent.intent_id)
        )
        reservation_id = intent.reservation_id or str(
            stable_uuid5("risk-reservation", intent.account, intent.intent_id)
        )
        with reservation_projection.transaction():
            reserved = reservation_projection.pending_notional(current_prices)
            approved = self.check_entry_risk(
                portfolio,
                intent.symbol,
                intent.requested_qty,
                reference_price,
                current_volume=current_volume,
                current_prices=current_prices,
                pending_open_notional=reserved,
                action=intent.action,
            )
            decision = RiskDecision(
                decision_id=decision_id,
                account=intent.account,
                symbol=intent.symbol,
                action=intent.action,
                requested_qty=intent.requested_qty,
                approved_qty=intent.requested_qty if approved else 0,
                reference_price=reference_price,
                approved=approved,
                reason="approved" if approved else "risk_limit",
                intent_id=intent.intent_id,
            )
            if not approved:
                event_pipeline.publish(
                    decision,
                    occurred_at=occurred_at,
                    correlation_id=intent.correlation_id,
                    idempotency_key=decision_id,
                    account_id=intent.account,
                    symbol=intent.symbol,
                    timeframe=intent.timeframe,
                    source=source,
                )
                return decision, None
            enriched = replace(
                intent,
                risk_decision_id=decision_id,
                reservation_id=reservation_id,
            )
            reservation = RiskReservation(
                reservation_id=reservation_id,
                risk_decision_id=decision_id,
                intent_id=enriched.intent_id,
                account=enriched.account,
                symbol=enriched.symbol,
                action=enriched.action,
                reserved_qty=enriched.requested_qty,
                reference_price=reference_price,
            )
            event_pipeline.publish_approved_intent(
                decision,
                reservation,
                enriched,
                occurred_at=occurred_at,
                source=source,
            )
            return decision, enriched

    def check_circuit_breaker(self, current_equity: float, daily_start_equity: float) -> bool:
        """
        检查日内回撤并触发熔断器。

        规则：
        - drawdown = 1 - current_equity / daily_start_equity
        - 当 drawdown > max_drawdown_limit 时触发熔断：
          - circuit_breaker_triggered = True
          - 后续 calculate_position_size / check_entry_risk 会拒绝开仓

        返回：
        - True：熔断器处于触发状态（本次触发或此前已触发）
        - False：未触发
        """
        current_equity = float(current_equity)
        if self.high_water_equity is None or current_equity > self.high_water_equity:
            self.high_water_equity = current_equity
        if self.high_water_equity > 0:
            self.last_drawdown = max(
                0.0, 1.0 - current_equity / self.high_water_equity
            )

        target = BreakerAction.NORMAL
        if self.last_drawdown >= self.portfolio_drawdown_lock:
            target = BreakerAction.LOCKED
        elif self.last_drawdown >= self.portfolio_drawdown_liquidate:
            target = BreakerAction.LIQUIDATE
        elif self.last_drawdown >= self.portfolio_drawdown_block:
            target = BreakerAction.BLOCK_NEW
        elif self.last_drawdown >= self.portfolio_drawdown_reduce:
            target = BreakerAction.REDUCE

        # Portfolio protection is sticky: only manual_resume may reduce the
        # action level.  A new high-water mark cannot silently re-enable risk.
        if _BREAKER_RANK[target] > _BREAKER_RANK[self.portfolio_breaker_action]:
            previous = self.portfolio_breaker_action
            self.portfolio_breaker_action = target
            self.breaker_audit.append({
                "event": "portfolio_drawdown_action",
                "from": previous.value,
                "to": target.value,
                "equity": current_equity,
                "high_water_equity": self.high_water_equity,
                "drawdown": self.last_drawdown,
            })
            logger.error(
                "Portfolio drawdown action %s at %.2f%% (high-water %.2f, equity %.2f)",
                target.value,
                self.last_drawdown * 100,
                self.high_water_equity,
                current_equity,
            )

        daily_drawdown = 0.0
        if daily_start_equity > 0:
            daily_drawdown = max(0.0, 1.0 - current_equity / daily_start_equity)
        if daily_drawdown >= self.daily_loss_limit:
            self.circuit_breaker_triggered = True
            logger.error(
                "Daily loss breaker triggered: %.2f%% >= %.2f%%",
                daily_drawdown * 100,
                self.daily_loss_limit * 100,
            )

        return self._blocks_new_risk()
