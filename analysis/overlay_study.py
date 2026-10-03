"""Paired overlay diagnostics; reference trades are proxies, not claimed savings."""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd


def read_csv(path):
    try:
        return pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def run_diagnostics(folder):
    folder = Path(folder)
    summary = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))
    equity = read_csv(folder / 'equity_engine.csv')
    trips = read_csv(folder / 'closed_trades.csv')
    fills = read_csv(folder / 'trades.csv')
    activity = read_csv(folder / 'strategy_activity.csv')
    entries = read_csv(folder / 'entry_observations.csv')
    audit = read_csv(folder / 'drawdown_budget_audit.csv')
    out = {key: summary[key] for key in ('return_pct', 'max_drawdown_pct', 'final_equity', 'fill_count', 'accounting_ok')}
    out['cash_days'] = int((equity.gross_exposure.abs() < 1e-10).sum())
    out['mean_exposure'] = float((equity.gross_exposure / equity.equity).mean())
    out['health_days'] = activity.strategy_states.map(
        lambda value: ast.literal_eval(value).get('TrendBreakout', 'unrouted')).value_counts().to_dict()
    out['entry_reasons'] = entries.reason.value_counts().to_dict() if not entries.empty else {}
    out['net_closed_pnl'] = float(trips.net_pnl.sum()) if not trips.empty else 0.
    out['commission'] = float(fills.commission.sum()) if not fills.empty else 0.
    out['execution_price_cost'] = float((fills.slip.abs() * fills.qty.abs()).sum()) if not fills.empty else 0.
    if not trips.empty:
        net = float(trips.net_pnl.sum())
        gross = float(trips.gross_pnl_theoretical.sum())
        costs = float(trips.commission.sum() + trips.slippage.sum())
        if abs(gross - costs - net) > 1e-6:
            raise ValueError('closed PnL cost identity failed')
        financing = read_csv(folder / 'financing_ledger.csv')
        # The engine's full cash/lot/carry identity is checked separately.
        out['closed_cost_identity_ok'] = True
        out['financing_rows'] = len(financing)
        positive = trips.net_pnl.loc[trips.net_pnl > 0].sort_values(ascending=False)
        out['net_closed_pnl_without_top_5'] = net - float(positive.head(5).sum())
    else:
        out['closed_cost_identity_ok'] = True
        out['net_closed_pnl_without_top_5'] = 0.
    reductions = fills.loc[fills.exit_reason == 'DrawdownBudgetReduce'] if not fills.empty else fills
    out['budget_reduce_fill_count'] = len(reductions)
    out['budget_reduce_execution_cost'] = float(
        (reductions.commission + reductions.slip.abs() * reductions.qty.abs()).sum()) if not reductions.empty else 0.
    out['budget_review_actions'] = audit.action.dropna().value_counts().to_dict() if 'action' in audit else {}
    out['budget_reduce_actions'] = int((audit.action == 'reduce').sum()) if 'action' in audit else 0
    recovery = []
    if not entries.empty and 'health_multiplier' in entries and not trips.empty:
        accepted = entries.loc[(entries.reason == 'order_accepted') & (entries.health_multiplier < 1)]
        openings = fills.loc[fills.side.isin(['buy','short'])]
        for row in accepted.itertuples():
            matched = openings.loc[openings.order_id == row.order_id]
            if matched.empty:
                continue
            first = pd.to_datetime(matched.fill_time).min()
            closed = trips.loc[(trips.symbol == row.symbol) & (pd.to_datetime(trips.entry_time) == first)]
            recovery.append({'symbol': row.symbol, 'entry_time': str(first),
                             'health_multiplier': row.health_multiplier,
                             'net_pnl': float(closed.net_pnl.sum()), 'closed_positions': len(closed)})
    out['recovery_entries'] = recovery
    out['recovery_closed_pnl'] = sum(row['net_pnl'] for row in recovery)
    out['recovery_filled_entries'] = len(recovery)
    lifecycle = read_csv(folder / 'exit_lifecycle_audit.csv')
    released = lifecycle.loc[(lifecycle.latch_released == True) & (lifecycle.status == 'canceled')] if not lifecycle.empty else lifecycle
    out['canceled_exit_latches_released'] = len(released)
    return out


def health_reference_attribution(control_folder, reference_folder):
    """Link reference-only fills to the control's observed health-block gate.

    Reference quantities and exit paths differ. These totals describe the
    reference book on matching blocked dates, not an additive decomposition
    of the control's profit or an executable counterfactual position.
    """
    control = Path(control_folder)
    reference = Path(reference_folder)
    gates = read_csv(control / 'entry_observations.csv')
    reference_entries = read_csv(reference / 'entry_observations.csv')
    reference_fills = read_csv(reference / 'trades.csv')
    reference_trips = read_csv(reference / 'closed_trades.csv')
    control_fills = read_csv(control / 'trades.csv')
    if any(table.empty for table in (gates, reference_entries, reference_fills, reference_trips)):
        return {'rows': [], 'reference_missed_gain_proxy': 0., 'reference_avoided_loss_proxy': 0.,
                'sample_status': 'no_linked_reference_trades'}
    blocked = set(zip(pd.to_datetime(gates.loc[gates.reason == 'strategy_health_block', 'timestamp']),
                      gates.loc[gates.reason == 'strategy_health_block', 'symbol']))
    already_entered = set(zip(pd.to_datetime(control_fills.loc[control_fills.side.isin(['buy','short']), 'signal_time']),
                             control_fills.loc[control_fills.side.isin(['buy','short']), 'symbol'])) if not control_fills.empty else set()
    rows = []
    seen = set()
    for entry in reference_entries.loc[reference_entries.reason == 'order_accepted'].itertuples():
        key = (pd.Timestamp(entry.timestamp), entry.symbol)
        if key not in blocked or key in already_entered:
            continue
        opening = reference_fills.loc[(reference_fills.order_id == entry.order_id)
                                      & reference_fills.side.isin(['buy','short'])]
        if opening.empty:
            continue
        at = pd.to_datetime(opening.fill_time).min()
        positions = reference_trips.loc[(reference_trips.symbol == entry.symbol)
                                        & (pd.to_datetime(reference_trips.entry_time) == at)]
        for position in positions.itertuples():
            if position.position_id in seen:
                continue
            seen.add(position.position_id)
            rows.append({'symbol': entry.symbol, 'blocked_signal_time': entry.timestamp,
                         'reference_entry_time': str(at), 'reference_position_id': position.position_id,
                         'reference_net_pnl': position.net_pnl})
    return {'rows': rows,
            'reference_missed_gain_proxy': sum(max(row['reference_net_pnl'],0) for row in rows),
            'reference_avoided_loss_proxy': sum(-min(row['reference_net_pnl'],0) for row in rows),
            'sample_status': 'linked_reference_trades' if rows else 'no_linked_reference_trades',
            'scope': 'Reference-only positions linked to observed health blocks. Reference sizing/exit paths; non-additive, not proven realized savings.'}
