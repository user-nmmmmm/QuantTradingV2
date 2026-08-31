"""Circuit-breaker state machine: daily loss breaker + sticky portfolio drawdown action.

Split out of core/risk.py (A4) — see docs/architecture_review.md.

This is a mixin, not a standalone collaborator object: ``RiskManager``
combines ``CircuitBreakerMixin``, ``PositionSizingMixin``, and
``EntryPolicyMixin`` via inheritance so every method still reads/writes the
same ``self`` attributes it always has. That keeps this split mechanical and
behavior-identical — a full composition redesign (separate ``PositionSizer``/
``CircuitBreaker`` objects with their own state) is a bigger change better
left for a dedicated pass on this money-path-adjacent code, not bundled into
a file-size cleanup.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, Optional

from core.logger import get_logger

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


class CircuitBreakerMixin:
    """Daily loss breaker, sticky portfolio drawdown action, and health gating.

    Expects ``self`` to carry the attributes ``RiskManager.__init__`` sets:
    ``daily_loss_limit``, ``portfolio_drawdown_reduce/block/liquidate/lock``,
    ``reduced_risk_multiplier``, ``circuit_breaker_triggered``,
    ``high_water_equity``, ``portfolio_breaker_action``, ``last_drawdown``,
    ``breaker_audit``, ``health_assessment``.
    """

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
