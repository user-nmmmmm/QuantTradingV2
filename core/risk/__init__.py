"""RiskManager（风险管理）模块

本模块对"准备下单"的交易做风控校验，并提供仓位计算工具。

核心职责：
- 仓位规模：按风险百分比（Risk%）或按固定资金占比（Fixed%）换算数量 qty
- 风险约束：流动性上限、杠杆上限、单标的集中度上限
- 熔断器：当出现超限回撤时，阻止后续开仓（将 qty 置 0 或直接拒单）

注意：
- 本 RiskManager 不直接下单；它只返回"是否允许交易/建议 qty"等结果。
- 回测与实盘共享同一套接口，但实盘更建议结合交易所真实仓位、保证金与订单簿深度做更严格校验。

Split by change reason (A4) — see docs/architecture_review.md:
- core/risk/circuit_breaker.py — daily loss breaker, sticky drawdown action
- core/risk/position_sizing.py — notional caps, qty clamping
- core/risk/entry_policy.py    — final admission gate, decision/reservation publishing

``RiskManager`` holds the three collaborators below (``_circuit_breaker``, ``_position_sizing``,
``_entry_policy``) and forwards the public methods to them with the same signatures. The collaborators reach
the shared state through their ``host`` reference, so behavior is identical to the former mixins (B4, ADR
0018). ``BreakerAction`` is re-exported here unchanged since it's part of this module's public API.
"""
import math
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    import pandas as pd

from core.risk.circuit_breaker import (
    BreakerAction,
    CircuitBreaker,
    RiskControlDecision,
)
from core.risk.position_sizing import PositionSizing
from core.risk.entry_policy import EntryPolicy
from core.risk.delegation import Delegated
from core.trading.portfolio import Portfolio
from core.risk.reservation import RiskReservationProjection
from typing import Tuple
from core.foundation.domain import OrderIntent
from core.events import TradingEventPipeline
from core.foundation.domain import RiskDecision
from functools import cached_property

__all__ = ["BreakerAction", "RiskControlDecision", "RiskManager"]


class RiskManager:
    # Shrink a clamped order this far below the cap so float rounding in
    # qty*price cannot push it back over the limit it was clamped to.
    CAP_SAFETY_MARGIN = 1e-9

    # The circuit breaker's durable state lives on CircuitBreaker (B6, ADR 0027); these names forward to it.
    blocked_until = Delegated[Optional["pd.Timestamp"]]("_circuit_breaker")
    probation_equity = Delegated[Optional[float]]("_circuit_breaker")
    recovery_count = Delegated[int]("_circuit_breaker")
    circuit_breaker_triggered = Delegated[bool]("_circuit_breaker")
    daily_loss_triggered = Delegated[bool]("_circuit_breaker")
    high_water_equity = Delegated[Optional[float]]("_circuit_breaker")
    portfolio_breaker_action = Delegated[BreakerAction]("_circuit_breaker")
    last_drawdown = Delegated[float]("_circuit_breaker")
    breaker_epoch = Delegated[int]("_circuit_breaker")
    _breaker_transition_sequence = Delegated[int]("_circuit_breaker")
    current_transition_id = Delegated[Optional[str]]("_circuit_breaker")
    current_daily_action_id = Delegated[Optional[str]]("_circuit_breaker")
    health_assessment = Delegated[Any]("_circuit_breaker")

    @cached_property
    def _circuit_breaker(self) -> CircuitBreaker:
        return CircuitBreaker(self)

    @cached_property
    def _position_sizing(self) -> PositionSizing:
        return PositionSizing(self)

    @cached_property
    def _entry_policy(self) -> EntryPolicy:
        return EntryPolicy(self)

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
        recovery_policy: Optional[Dict] = None,
        minimum_entry_policy: Optional[Dict] = None,
        drawdown_budget_policy: Optional[Dict] = None,
        execution_costs: Optional[Dict] = None,
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
        from core.risk.recovery import DrawdownRecoveryPolicy
        self.recovery_policy = DrawdownRecoveryPolicy(**(recovery_policy or {}))
        if self.recovery_policy.probation_risk_multiplier > self.reduced_risk_multiplier:
            raise ValueError("probation risk must not exceed reduced risk")
        self.liquidity_limit_pct = liquidity_limit_pct
        self.max_pos_size_pct = max_pos_size_pct
        self.min_entry_notional_pct = min_entry_notional_pct
        minimum = minimum_entry_policy or {}
        self.scale_minimum_with_risk = bool(minimum.get("scale_with_risk", False))
        self.research_notional_floor = float(minimum.get("research_notional_floor", 0.0))
        if not math.isfinite(self.research_notional_floor) or self.research_notional_floor < 0:
            raise ValueError("research_notional_floor must be non-negative")
        from core.risk.drawdown_budget import DrawdownBudget
        self.drawdown_budget = DrawdownBudget(self, drawdown_budget_policy, execution_costs)

        self.breaker_audit: list[Dict[str, object]] = []
        # SR3-2: correlation-aware notional budgets, installed by
        # composition.factory. None keeps the pre-SR3 behaviour.
        self.cluster_policy = None
        # Populated by _entry_notional_caps so the gate/clamp can report the
        # equity/exposure figures behind a decision without recomputing them.
        self._last_entry_context: Dict[str, float] = {}

    def set_health_assessment(self, assessment) -> None:
        return self._circuit_breaker.set_health_assessment(assessment)

    def _health_allows_new_risk(self) -> bool:
        return self._circuit_breaker._health_allows_new_risk()

    def reset_daily_breaker(self) -> None:
        return self._circuit_breaker.reset_daily_breaker()

    @property
    def breaker_action(self) -> BreakerAction:
        return self._circuit_breaker.breaker_action

    @property
    def risk_multiplier(self) -> float:
        return self._circuit_breaker.risk_multiplier

    def _blocks_new_risk(self) -> bool:
        return self._circuit_breaker._blocks_new_risk()

    def manual_resume(
        self,
        *,
        approved_by: str,
        current_equity: float,
        rebase_high_water: bool = False,
        occurred_at=None,
        bar_index: Optional[int] = None,
    ) -> None:
        return self._circuit_breaker.manual_resume(
            approved_by=approved_by,
            current_equity=current_equity,
            rebase_high_water=rebase_high_water,
            occurred_at=occurred_at,
            bar_index=bar_index,
        )

    def check_circuit_breaker(
        self,
        current_equity: float,
        daily_start_equity: float,
        *,
        occurred_at=None,
        bar_index: Optional[int] = None,
    ) -> RiskControlDecision:
        return self._circuit_breaker.check_circuit_breaker(
            current_equity,
            daily_start_equity,
            occurred_at=occurred_at,
            bar_index=bar_index,
        )

    def _recovery_transition(self, action, reason, now, equity, bar_index):
        return self._circuit_breaker._recovery_transition(action, reason, now, equity, bar_index)

    def breaker_checkpoint(self):
        return self._circuit_breaker.breaker_checkpoint()

    def restore_breaker_checkpoint(self, state):
        return self._circuit_breaker.restore_breaker_checkpoint(state)

    def record_breaker_action_result(
        self,
        action_id: Optional[str],
        *,
        post_action_equity: float,
        cost: float,
        positions_before: int,
        positions_after: int,
        executed: bool = True,
        overridden_by: Optional[str] = None,
    ) -> None:
        return self._circuit_breaker.record_breaker_action_result(
            action_id,
            post_action_equity=post_action_equity,
            cost=cost,
            positions_before=positions_before,
            positions_after=positions_after,
            executed=executed,
            overridden_by=overridden_by,
        )

    def calculate_position_size(self, equity: float, entry_price: float, stop_loss_price: float) -> float:
        return self._position_sizing.calculate_position_size(equity, entry_price, stop_loss_price)

    def calculate_position_size_fixed_pct(self, equity: float, entry_price: float, pct: float = 0.1) -> float:
        return self._position_sizing.calculate_position_size_fixed_pct(equity, entry_price, pct)

    def _entry_notional_caps(
        self,
        portfolio: Portfolio,
        symbol: str,
        price: float,
        current_prices: Optional[Dict[str, float]],
        reserved_by_symbol: Dict[str, float],
        action: str,
        reservation_projection=None,
    ) -> Optional[Dict[str, float]]:
        return self._position_sizing._entry_notional_caps(
            portfolio,
            symbol,
            price,
            current_prices,
            reserved_by_symbol,
            action,
            reservation_projection,
        )

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
        action: str = 'buy',
    ) -> float:
        return self._position_sizing.max_entry_notional(
            portfolio,
            symbol,
            price,
            current_volume=current_volume,
            current_prices=current_prices,
            pending_open_notional=pending_open_notional,
            reservation_projection=reservation_projection,
            action=action,
        )

    def minimum_entry_notional(
        self,
        equity: float,
        health_multiplier: float = 1.0,
        *,
        live: bool = False,
    ) -> float:
        return self._position_sizing.minimum_entry_notional(equity, health_multiplier, live=live)

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
        action: str = 'buy',
        health_multiplier: float = 1.0,
    ) -> float:
        return self._position_sizing.clamp_entry_qty(
            portfolio,
            symbol,
            qty,
            price,
            current_volume=current_volume,
            current_prices=current_prices,
            pending_open_notional=pending_open_notional,
            reservation_projection=reservation_projection,
            action=action,
            health_multiplier=health_multiplier,
        )

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
        action: str = 'buy',
    ) -> bool:
        return self._entry_policy.check_entry_risk(
            portfolio,
            symbol,
            qty,
            price,
            current_volume,
            current_prices,
            pending_open_notional,
            reservation_projection,
            action,
        )

    def approve_and_create_intent(
        self,
        portfolio: Portfolio,
        intent: OrderIntent,
        *,
        reference_price: float,
        event_pipeline: TradingEventPipeline,
        reservation_projection: RiskReservationProjection,
        occurred_at,
        source: str = 'risk',
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Tuple[RiskDecision, Optional[OrderIntent]]:
        return self._entry_policy.approve_and_create_intent(
            portfolio,
            intent,
            reference_price=reference_price,
            event_pipeline=event_pipeline,
            reservation_projection=reservation_projection,
            occurred_at=occurred_at,
            source=source,
            current_volume=current_volume,
            current_prices=current_prices,
        )
