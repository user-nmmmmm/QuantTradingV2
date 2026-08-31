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

from dataclasses import dataclass
from enum import Enum
from typing import Optional

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


@dataclass(frozen=True)
class RiskControlDecision:
    """Explicit capabilities and forced actions for one risk evaluation."""

    action: BreakerAction
    allow_position_management: bool
    allow_new_entries: bool
    force_reduce_fraction: Optional[float] = None
    force_liquidate: bool = False
    terminal: bool = False
    reason_codes: tuple[str, ...] = ()
    transition_id: Optional[str] = None
    breaker_epoch: int = 0
    daily_loss_triggered: bool = False

    def __bool__(self) -> bool:
        """Compatibility: truth still means opening new risk is blocked."""
        return not self.allow_new_entries


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
        if self.daily_loss_triggered:
            logger.info("Resetting daily circuit breaker state")
        self.daily_loss_triggered = False
        self.current_daily_action_id = None
        # Kept as a compatibility summary for live safety consumers.  Unlike
        # daily_loss_triggered it may remain true because of portfolio state.
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
        occurred_at=None,
        bar_index: Optional[int] = None,
    ) -> None:
        """Resume a persistent portfolio breaker only after named approval."""
        if not approved_by or not approved_by.strip():
            raise ValueError("approved_by is required for manual recovery")
        previous = self.portfolio_breaker_action
        if rebase_high_water or self.high_water_equity is None:
            self.high_water_equity = float(current_equity)
        self.portfolio_breaker_action = BreakerAction.NORMAL
        self.circuit_breaker_triggered = False
        self.daily_loss_triggered = False
        self.current_daily_action_id = None
        self.breaker_epoch += 1
        self._breaker_transition_sequence = 0
        self.current_transition_id = None
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
            "breaker_epoch": self.breaker_epoch,
            "occurred_at": occurred_at,
            "bar_index": bar_index,
            "action_id": f"epoch-{self.breaker_epoch}-resume",
        })

    def check_circuit_breaker(
        self,
        current_equity: float,
        daily_start_equity: float,
        *,
        occurred_at=None,
        bar_index: Optional[int] = None,
    ) -> RiskControlDecision:
        """
        检查日内回撤并触发熔断器。

        规则：
        - drawdown = 1 - current_equity / daily_start_equity
        - 当 drawdown > max_drawdown_limit 时触发熔断：
          - circuit_breaker_triggered = True
          - 后续 calculate_position_size / check_entry_risk 会拒绝开仓

        返回结构化决策；其布尔值为 True 时表示禁止新增风险。
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
            self._breaker_transition_sequence += 1
            self.current_transition_id = (
                f"epoch-{self.breaker_epoch}-transition-"
                f"{self._breaker_transition_sequence}"
            )
            threshold = {
                BreakerAction.REDUCE: self.portfolio_drawdown_reduce,
                BreakerAction.BLOCK_NEW: self.portfolio_drawdown_block,
                BreakerAction.LIQUIDATE: self.portfolio_drawdown_liquidate,
                BreakerAction.LOCKED: self.portfolio_drawdown_lock,
            }[target]
            self.breaker_audit.append({
                "event": "portfolio_drawdown_action",
                "from": previous.value,
                "to": target.value,
                "equity": current_equity,
                "pre_action_equity": current_equity,
                "post_action_equity": None,
                "cost": None,
                "high_water_equity": self.high_water_equity,
                "drawdown": self.last_drawdown,
                "threshold": threshold,
                "occurred_at": occurred_at,
                "bar_index": bar_index,
                "reason_codes": [f"portfolio_drawdown_{target.value}"],
                "action_id": self.current_transition_id,
                "breaker_epoch": self.breaker_epoch,
                "positions_before": None,
                "positions_after": None,
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
            first_daily_trigger = not self.daily_loss_triggered
            self.daily_loss_triggered = True
            self.circuit_breaker_triggered = True
            if first_daily_trigger:
                self.current_daily_action_id = (
                    f"epoch-{self.breaker_epoch}-daily-{bar_index}"
                )
                self.breaker_audit.append({
                    "event": "daily_loss_triggered",
                    "occurred_at": occurred_at,
                    "bar_index": bar_index,
                    "threshold": self.daily_loss_limit,
                    "pre_action_equity": current_equity,
                    "post_action_equity": None,
                    "cost": None,
                    "daily_start_equity": float(daily_start_equity),
                    "drawdown": daily_drawdown,
                    "reason_codes": ["daily_loss_limit"],
                    "action_id": self.current_daily_action_id,
                    "breaker_epoch": self.breaker_epoch,
                    "positions_before": None,
                    "positions_after": None,
                })
            logger.error(
                "Daily loss breaker triggered: %.2f%% >= %.2f%%",
                daily_drawdown * 100,
                self.daily_loss_limit * 100,
            )

        action = self.portfolio_breaker_action
        daily_block = bool(self.daily_loss_triggered)
        reason_codes = []
        if action is not BreakerAction.NORMAL:
            reason_codes.append(f"portfolio_drawdown_{action.value}")
        if daily_block:
            reason_codes.append("daily_loss_limit")
        return RiskControlDecision(
            action=action,
            allow_position_management=action not in {
                BreakerAction.LIQUIDATE, BreakerAction.LOCKED
            },
            allow_new_entries=(
                not daily_block
                and _BREAKER_RANK[action] < _BREAKER_RANK[BreakerAction.BLOCK_NEW]
            ),
            force_reduce_fraction=(
                self.reduced_risk_multiplier
                if action is BreakerAction.REDUCE else None
            ),
            force_liquidate=action in {
                BreakerAction.LIQUIDATE, BreakerAction.LOCKED
            } or (daily_block and action is BreakerAction.NORMAL),
            terminal=action in {BreakerAction.LIQUIDATE, BreakerAction.LOCKED},
            reason_codes=tuple(reason_codes),
            transition_id=(
                self.current_transition_id
                if action in {BreakerAction.LIQUIDATE, BreakerAction.LOCKED}
                else self.current_daily_action_id or self.current_transition_id
            ),
            breaker_epoch=self.breaker_epoch,
            daily_loss_triggered=daily_block,
        )

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
        """Complete the matching transition audit after the broker acts."""
        if not action_id:
            return
        for entry in reversed(self.breaker_audit):
            if entry.get("action_id") == action_id:
                if entry.get("post_action_equity") is not None:
                    return
                entry.update({
                    "post_action_equity": float(post_action_equity),
                    "cost": float(cost),
                    "positions_before": int(positions_before),
                    "positions_after": int(positions_after),
                    "executed": bool(executed),
                    "overridden_by": overridden_by,
                })
                return
