"""Shared, deterministic interpretation of portfolio risk decisions."""
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskActionPlan:
    action_id: str
    reason: str
    remaining_fraction: float


def plan_risk_action(decision, day, *, block_remaining=None):
    if decision is None:
        return None
    action = decision.action.value
    identity = decision.transition_id or f"epoch-{decision.breaker_epoch}-{action}"
    if decision.daily_loss_triggered:
        return RiskActionPlan(f"daily-{day}", "DailyLossLimit", 0.0)
    if decision.force_liquidate or action in {"liquidate", "locked"}:
        return RiskActionPlan(identity, "AccountLiquidation", 0.0)
    if decision.force_reduce_fraction is not None:
        return RiskActionPlan(identity, "DrawdownReduce", float(decision.force_reduce_fraction))
    if action == "block_new" and block_remaining is not None:
        return RiskActionPlan(identity, "DrawdownReduce", float(block_remaining))
    return None
