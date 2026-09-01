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
- core/risk_circuit_breaker.py — daily loss breaker, sticky drawdown action
- core/risk_position_sizing.py — notional caps, qty clamping
- core/risk_entry_policy.py    — final admission gate, decision/reservation publishing

``RiskManager`` composes the three mixins below via inheritance rather than
holding separate collaborator objects, so every method still reads/writes
the exact same ``self`` attributes as before the split — behavior-identical,
mechanical. ``BreakerAction`` is re-exported here unchanged since it's part
of this module's public API.
"""
from typing import Dict, Optional

from core.risk_circuit_breaker import (
    BreakerAction,
    CircuitBreakerMixin,
    RiskControlDecision,
)
from core.risk_position_sizing import PositionSizingMixin
from core.risk_entry_policy import EntryPolicyMixin

__all__ = ["BreakerAction", "RiskControlDecision", "RiskManager"]


class RiskManager(CircuitBreakerMixin, PositionSizingMixin, EntryPolicyMixin):
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
        self.daily_loss_triggered = False
        self.high_water_equity: Optional[float] = None
        self.portfolio_breaker_action = BreakerAction.NORMAL
        self.last_drawdown = 0.0
        self.breaker_audit: list[Dict[str, object]] = []
        self.breaker_epoch = 0
        # SR3-2: correlation-aware notional budgets, installed by
        # composition.factory. None keeps the pre-SR3 behaviour.
        self.cluster_policy = None
        self._breaker_transition_sequence = 0
        self.current_transition_id: Optional[str] = None
        self.current_daily_action_id: Optional[str] = None
        self.health_assessment = None
        # Populated by _entry_notional_caps so the gate/clamp can report the
        # equity/exposure figures behind a decision without recomputing them.
        self._last_entry_context: Dict[str, float] = {}
