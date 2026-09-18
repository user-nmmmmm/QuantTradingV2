"""Execute the shared drawdown budget through real simulated order/fill facts."""
from __future__ import annotations


class BacktestDrawdownReducer:
    def __init__(self, budget):
        self.budget = budget
        self.sequence = 0

    def step(self, event):
        budget = self.budget
        if not budget.policy.enabled:
            return []
        broker = budget.broker
        snap = budget.snapshot()
        row = {"timestamp": str(event.timestamp), "event": "budget_review", **snap.to_dict()}
        budget.audit.append(row)
        if snap.issues:
            row['action'] = 'unverifiable_block_new'
            return []
        # Open-risk reservations cannot be released until cancellation facts
        # have been published into the same projection that admission reads.
        if snap.over_budget:
            broker.cancel_opening_orders(timestamp=event.timestamp)
            snap = budget.snapshot()
        pending = [o for o in broker.pending_orders + broker.active_orders
                   if o.exit_reason == 'DrawdownBudgetReduce']
        if pending:
            row['action'] = 'reduction_in_flight'
            row['pending_reduce_ids'] = [o.id for o in pending]
            return []
        if snap.issues or not snap.over_budget:
            row['action'] = 'within_budget'
            return []
        self.sequence += 1
        action_id = f'drawdown-budget-{event.timestamp.isoformat()}-{self.sequence}'
        fraction = budget.reduction_fraction(snap)
        targets = {symbol: abs(float(pos['qty'])) * fraction
                   for symbol, pos in broker.portfolio.positions.items() if pos['qty']}
        trades = broker.force_liquidate(dict(event.bars), timestamp=event.timestamp,
            reason='DrawdownBudgetReduce', remaining_fraction=fraction, risk_action_id=action_id)
        row.update(action='reduce', action_id=action_id, remaining_fraction=fraction,
                   target_quantities=targets, after=budget.snapshot().to_dict())
        return trades
