"""Circuit-breaker state machine: daily loss breaker + sticky portfolio drawdown action.

Split out of core/risk.py (A4) — see docs/architecture_review.md.

``CircuitBreaker`` is a collaborator that ``RiskManager`` holds (``_circuit_breaker``); it was a mixin
before B4 (ADR 0018). It reads and writes the breaker state through its ``host`` reference, and every call
back into the risk manager goes through the host's own methods, so overrides and ``patch.object`` on the
``RiskManager`` still intercept it. The state attributes themselves are still owned by the ``RiskManager``
(external code, state export and recovery read them directly); moving that state into the collaborator is a
separate step.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
import math
import pandas as pd

from core.foundation.logger import get_logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.risk import RiskManager

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


class CircuitBreaker:
    """Daily loss breaker, sticky portfolio drawdown action, and health gating.

    Expects ``self`` to carry the attributes ``RiskManager.__init__`` sets:
    ``daily_loss_limit``, ``portfolio_drawdown_reduce/block/liquidate/lock``,
    ``reduced_risk_multiplier``, ``circuit_breaker_triggered``,
    ``high_water_equity``, ``portfolio_breaker_action``, ``last_drawdown``,
    ``breaker_audit``, ``health_assessment``.
    """

    def __init__(self, host: "RiskManager") -> None:
        self._host = host

    def set_health_assessment(self, assessment) -> None:
        """Install the latest live health fact used by opening-risk checks."""
        host = self._host
        host.health_assessment = assessment

    def _health_allows_new_risk(self) -> bool:
        host = self._host
        assessment = host.health_assessment
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
        host = self._host
        if host.daily_loss_triggered:
            logger.info("Resetting daily circuit breaker state")
        host.daily_loss_triggered = False
        host.current_daily_action_id = None
        # Kept as a compatibility summary for live safety consumers.  Unlike
        # daily_loss_triggered it may remain true because of portfolio state.
        host.circuit_breaker_triggered = (
            _BREAKER_RANK[host.portfolio_breaker_action]
            >= _BREAKER_RANK[BreakerAction.BLOCK_NEW]
        )

    @property
    def breaker_action(self) -> BreakerAction:
        host = self._host
        return host.portfolio_breaker_action

    @property
    def risk_multiplier(self) -> float:
        host = self._host
        if host._blocks_new_risk():
            return 0.0
        if host.probation_equity is not None:
            return host.recovery_policy.probation_risk_multiplier
        if host.portfolio_breaker_action is BreakerAction.REDUCE:
            return host.reduced_risk_multiplier
        return 1.0

    def _blocks_new_risk(self) -> bool:
        host = self._host
        return host.circuit_breaker_triggered or (
            _BREAKER_RANK[host.portfolio_breaker_action]
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
        host = self._host
        if not approved_by or not approved_by.strip():
            raise ValueError("approved_by is required for manual recovery")
        previous = host.portfolio_breaker_action
        host.blocked_until = None
        host.probation_equity = None
        if rebase_high_water or host.high_water_equity is None:
            host.high_water_equity = float(current_equity)
        host.portfolio_breaker_action = BreakerAction.NORMAL
        host.circuit_breaker_triggered = False
        host.daily_loss_triggered = False
        host.current_daily_action_id = None
        host.breaker_epoch += 1
        host._breaker_transition_sequence = 0
        host.current_transition_id = None
        host.last_drawdown = max(
            0.0,
            1.0 - float(current_equity) / max(float(host.high_water_equity), 1e-12),
        )
        host.breaker_audit.append({
            "event": "manual_resume",
            "approved_by": approved_by.strip(),
            "equity": float(current_equity),
            "previous_action": previous.value,
            "rebase_high_water": bool(rebase_high_water),
            "breaker_epoch": host.breaker_epoch,
            "occurred_at": occurred_at,
            "bar_index": bar_index,
            "action_id": f"epoch-{host.breaker_epoch}-resume",
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
        host = self._host
        current_equity = float(current_equity)
        if not math.isfinite(current_equity):
            raise ValueError("breaker equity must be finite")
        if host.high_water_equity is None or current_equity > host.high_water_equity:
            # Insolvency must produce a terminal decision, not bypass risk
            # handling by raising before liquidation can be requested.
            host.high_water_equity = max(current_equity, 1e-12)
        if host.high_water_equity > 0:
            host.last_drawdown = max(
                0.0, 1.0 - current_equity / host.high_water_equity
            )

        target = BreakerAction.NORMAL
        if host.last_drawdown >= host.portfolio_drawdown_lock:
            target = BreakerAction.LOCKED
        elif host.last_drawdown >= host.portfolio_drawdown_liquidate:
            target = BreakerAction.LIQUIDATE
        elif host.last_drawdown >= host.portfolio_drawdown_block:
            target = BreakerAction.BLOCK_NEW
        elif host.last_drawdown >= host.portfolio_drawdown_reduce:
            target = BreakerAction.REDUCE

        # Recovery changes only the opening-risk policy. Absolute liquidation
        # and manual-lock thresholds always use the original high-water mark.
        now = None if occurred_at is None else pd.Timestamp(occurred_at)
        if now is not None:
            if pd.isna(now):
                raise ValueError("invalid breaker timestamp")
            now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        terminal = host.portfolio_breaker_action in {BreakerAction.LIQUIDATE, BreakerAction.LOCKED}
        if host.recovery_policy.enabled and not terminal and target not in {
            BreakerAction.LIQUIDATE, BreakerAction.LOCKED
        }:
            if host.portfolio_breaker_action is BreakerAction.BLOCK_NEW:
                # Old snapshots without a deadline start a full cooldown.
                if host.blocked_until is None and now is not None:
                    host.blocked_until = (now + pd.Timedelta(days=host.recovery_policy.cooldown_days)).isoformat()
                if now is not None and now >= pd.Timestamp(host.blocked_until):
                    healthy = host.health_assessment is None or bool(
                        getattr(host.health_assessment, "allows_new_risk", False)
                    )
                    daily_safe = (daily_start_equity > 0 and
                                  1 - current_equity / daily_start_equity < host.daily_loss_limit)
                    if healthy and daily_safe and not host.daily_loss_triggered:
                        host.probation_equity = current_equity
                        host.blocked_until = None
                        host.recovery_count += 1
                        host._recovery_transition(BreakerAction.REDUCE, "cooldown_expired", now, current_equity, bar_index)
            if host.probation_equity is not None:
                loss = 1 - current_equity / host.probation_equity
                if loss >= host.recovery_policy.probation_loss_limit:
                    target = BreakerAction.BLOCK_NEW
                    host.probation_equity = None
                elif host.last_drawdown < host.portfolio_drawdown_reduce:
                    host.probation_equity = None
                    host._recovery_transition(BreakerAction.REDUCE, "probation_recovered", now, current_equity, bar_index)
                    target = BreakerAction.REDUCE
                else:
                    # Still underwater: permit bounded new risk rather than
                    # immediately re-entering BLOCK_NEW on the same drawdown.
                    target = BreakerAction.REDUCE

        # Escalations stay sticky except for the explicit, audited BLOCK_NEW
        # recovery above. Terminal actions still require manual approval.
        if _BREAKER_RANK[target] > _BREAKER_RANK[host.portfolio_breaker_action]:
            previous = host.portfolio_breaker_action
            host.portfolio_breaker_action = target
            if target is BreakerAction.BLOCK_NEW:
                host.blocked_until = (
                    (now + pd.Timedelta(days=host.recovery_policy.cooldown_days)).isoformat()
                    if now is not None and host.recovery_policy.enabled else None
                )
            if target in {BreakerAction.BLOCK_NEW, BreakerAction.LIQUIDATE, BreakerAction.LOCKED}:
                host.probation_equity = None
            host._breaker_transition_sequence += 1
            host.current_transition_id = (
                f"epoch-{host.breaker_epoch}-transition-"
                f"{host._breaker_transition_sequence}"
            )
            threshold = {
                BreakerAction.REDUCE: host.portfolio_drawdown_reduce,
                BreakerAction.BLOCK_NEW: host.portfolio_drawdown_block,
                BreakerAction.LIQUIDATE: host.portfolio_drawdown_liquidate,
                BreakerAction.LOCKED: host.portfolio_drawdown_lock,
            }[target]
            host.breaker_audit.append({
                "event": "portfolio_drawdown_action",
                "from": previous.value,
                "to": target.value,
                "equity": current_equity,
                "pre_action_equity": current_equity,
                "post_action_equity": None,
                "cost": None,
                "high_water_equity": host.high_water_equity,
                "drawdown": host.last_drawdown,
                "threshold": threshold,
                "occurred_at": occurred_at,
                "bar_index": bar_index,
                "reason_codes": [f"portfolio_drawdown_{target.value}"],
                "action_id": host.current_transition_id,
                "breaker_epoch": host.breaker_epoch,
                "positions_before": None,
                "positions_after": None,
                "blocked_until": host.blocked_until,
            })
            logger.error(
                "Portfolio drawdown action %s at %.2f%% (high-water %.2f, equity %.2f)",
                target.value,
                host.last_drawdown * 100,
                host.high_water_equity,
                current_equity,
            )

        daily_drawdown = 0.0
        if daily_start_equity > 0:
            daily_drawdown = max(0.0, 1.0 - current_equity / daily_start_equity)
        if daily_drawdown >= host.daily_loss_limit:
            first_daily_trigger = not host.daily_loss_triggered
            host.daily_loss_triggered = True
            host.circuit_breaker_triggered = True
            if first_daily_trigger:
                host.current_daily_action_id = (
                    f"epoch-{host.breaker_epoch}-daily-{bar_index}"
                )
                host.breaker_audit.append({
                    "event": "daily_loss_triggered",
                    "occurred_at": occurred_at,
                    "bar_index": bar_index,
                    "threshold": host.daily_loss_limit,
                    "pre_action_equity": current_equity,
                    "post_action_equity": None,
                    "cost": None,
                    "daily_start_equity": float(daily_start_equity),
                    "drawdown": daily_drawdown,
                    "reason_codes": ["daily_loss_limit"],
                    "action_id": host.current_daily_action_id,
                    "breaker_epoch": host.breaker_epoch,
                    "positions_before": None,
                    "positions_after": None,
                })
            logger.error(
                "Daily loss breaker triggered: %.2f%% >= %.2f%%",
                daily_drawdown * 100,
                host.daily_loss_limit * 100,
            )

        action = host.portfolio_breaker_action
        daily_block = bool(host.daily_loss_triggered)
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
                host.reduced_risk_multiplier
                if (action is BreakerAction.REDUCE and host.probation_equity is None
                    and "recovery" not in (host.current_transition_id or "")) else None
            ),
            force_liquidate=action in {
                BreakerAction.LIQUIDATE, BreakerAction.LOCKED
            } or (daily_block and action is BreakerAction.NORMAL),
            terminal=action in {BreakerAction.LIQUIDATE, BreakerAction.LOCKED},
            reason_codes=tuple(reason_codes),
            transition_id=(
                host.current_transition_id
                if action in {BreakerAction.LIQUIDATE, BreakerAction.LOCKED}
                else host.current_daily_action_id or host.current_transition_id
            ),
            breaker_epoch=host.breaker_epoch,
            daily_loss_triggered=daily_block,
        )

    def _recovery_transition(self, action, reason, now, equity, bar_index):
        host = self._host
        previous = host.portfolio_breaker_action
        host.portfolio_breaker_action = action
        host.circuit_breaker_triggered = host.daily_loss_triggered
        host._breaker_transition_sequence += 1
        host.current_transition_id = f"epoch-{host.breaker_epoch}-recovery-{host._breaker_transition_sequence}"
        host.breaker_audit.append({
            "event": "portfolio_recovery", "from": previous.value, "to": action.value,
            "reason_codes": [reason], "occurred_at": now, "bar_index": bar_index,
            "action_id": host.current_transition_id, "breaker_epoch": host.breaker_epoch,
            "equity": equity, "high_water_equity": host.high_water_equity,
            "drawdown": host.last_drawdown, "rebase_high_water": False,
            "probation_equity": host.probation_equity, "risk_multiplier": host.risk_multiplier,
        })

    def breaker_checkpoint(self):
        """Durable control state, separate from the operator-facing summary."""
        host = self._host
        return {
            "schema_version": 1,
            "action": host.portfolio_breaker_action.value,
            "high_water_equity": host.high_water_equity,
            "last_drawdown": host.last_drawdown,
            "daily_loss_triggered": host.daily_loss_triggered,
            "circuit_breaker_triggered": host.circuit_breaker_triggered,
            "blocked_until": host.blocked_until,
            "probation_equity": host.probation_equity,
            "recovery_count": host.recovery_count,
            "breaker_epoch": host.breaker_epoch,
            "transition_sequence": host._breaker_transition_sequence,
            "current_transition_id": host.current_transition_id,
            "current_daily_action_id": host.current_daily_action_id,
        }

    def restore_breaker_checkpoint(self, state):
        host = self._host
        if state.get("schema_version") != 1:
            raise ValueError("unsupported breaker checkpoint schema")
        action = BreakerAction(state["action"])
        for name in ("high_water_equity", "probation_equity"):
            value = state[name]
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"invalid breaker checkpoint {name}")
        if state["blocked_until"] is not None:
            point = pd.Timestamp(state["blocked_until"])
            if pd.isna(point) or point.tzinfo is None:
                raise ValueError("invalid recovery deadline")
        if state["probation_equity"] is not None and action is not BreakerAction.REDUCE:
            raise ValueError("probation must use reduced action")
        for name in ("recovery_count", "breaker_epoch", "transition_sequence"):
            if type(state[name]) is not int or state[name] < 0:
                raise ValueError(f"invalid breaker checkpoint {name}")
        for name in ("daily_loss_triggered", "circuit_breaker_triggered"):
            if type(state[name]) is not bool:
                raise ValueError(f"invalid breaker checkpoint {name}")
        if not math.isfinite(state["last_drawdown"]) or state["last_drawdown"] < 0:
            raise ValueError("invalid checkpoint drawdown")
        host.portfolio_breaker_action = action
        for name in ("high_water_equity", "last_drawdown", "daily_loss_triggered",
                     "circuit_breaker_triggered", "blocked_until", "probation_equity",
                     "recovery_count", "breaker_epoch", "current_transition_id", "current_daily_action_id"):
            setattr(host, name, state[name])
        host._breaker_transition_sequence = state["transition_sequence"]

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
        host = self._host
        if not action_id:
            return
        for entry in reversed(host.breaker_audit):
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
