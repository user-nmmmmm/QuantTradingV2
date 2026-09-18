"""Predeclared adversarial research matrix. Never tunes or admits a strategy."""
from copy import deepcopy
import argparse
import json
import logging
from pathlib import Path
import sys
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from core.reproducibility import canonical_json, sha256_frame
from core.data import DataHandler
from scripts.run_revalidation60 import (
    load_inputs, protocol, run_one, save, concentration, source_hashes, write_research_outputs,
)


def read_csv(path):
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def analyse(root, summaries):
    runs = root / 'runs'
    def table(name, file):
        return read_csv(runs / name / file)
    def curve(name):
        return table(name, 'equity_requested_period.csv').set_index('timestamp')
    old, new = curve('legacy'), curve('main_1')
    delta = new.equity - old.equity
    delta.to_csv(root / 'equity_delta.csv', header=['new_minus_legacy'])
    trades = table('main_1', 'trades.csv')
    legacy_trades = table('legacy', 'trades.csv')
    events = table('main_1', 'close_events.csv')
    old_events = table('legacy', 'close_events.csv')
    pnl = events.groupby('symbol').realized_pnl.sum().subtract(
        old_events.groupby('symbol').realized_pnl.sum(), fill_value=0).sort_values()
    pnl.to_csv(root / 'symbol_pnl_delta.csv', header=['new_minus_legacy_closed_pnl'])
    extra = events[~events.close_event_id.isin(old_events.close_event_id)].copy()
    # Identities after the fork can reuse sequence IDs, so use date filtering for attribution.
    cutoff = '2022-09-19'
    extra = events[pd.to_datetime(events.timestamp) > pd.Timestamp(cutoff)].copy()
    extra['stage'] = np.where(pd.to_datetime(extra.timestamp) < pd.Timestamp('2023-12-22'),
                              'recovery_probation', 'normal_risk_restore_and_exit')
    extra.to_csv(root / 'post_lock_close_events.csv', index=False)
    stage = extra.groupby('stage').agg(net_closed_pnl=('realized_pnl', 'sum'), close_legs=('realized_pnl', 'size'))
    stage.to_csv(root / 'recovery_stage_pnl.csv')
    audit = table('main_1', 'breaker_audit.csv')
    cohorts = table('main_1', 'strategy_health_cohorts.csv')
    cohorts.to_csv(root / 'health_evidence.csv', index=False)
    accounting = {}
    for name in ('legacy', 'main_1'):
        ev = table(name, 'close_events.csv')
        tr = table(name, 'trades.csv')
        ledger = table(name, 'financing_ledger.csv')
        accounting[name] = {'equity_change': float(curve(name).equity.iloc[-1] - 10000),
            'closed_pnl': float(ev.realized_pnl.sum()), 'commissions_paid': float(tr.commission.sum()),
            'financing_rows': len(ledger),
            'equity_minus_closed_pnl': float(curve(name).equity.iloc[-1] - 10000 - ev.realized_pnl.sum())}
    checks = {}
    comparison_cols = ['signal_time', 'fill_time', 'symbol', 'side', 'qty', 'fill_price', 'commission', 'exit_reason']
    for name, cutoff in [('prefix_2023', '2023-12-21'), ('prefix_2024', '2024-01-06')]:
        short = table(name, 'trades.csv')
        long = trades[pd.to_datetime(trades.fill_time) <= pd.Timestamp(cutoff)]
        checks[name] = canonical_json(short[comparison_cols].to_dict('records')) == canonical_json(long[comparison_cols].to_dict('records'))
    permuted = table('reversed_symbols', 'trades.csv')
    checks['symbol_order_invariant'] = canonical_json(trades[comparison_cols].to_dict('records')) == canonical_json(permuted[comparison_cols].to_dict('records'))
    changed = np.flatnonzero(np.abs(delta.to_numpy()) > 1e-7)
    save(root / 'attribution.json', {'first_equity_divergence': str(delta.index[changed[0]]) if len(changed) else None,
        'final_equity_delta': float(delta.iloc[-1]), 'accounting': accounting,
        'stage_closed_pnl': stage.reset_index().to_dict('records'),
        'note': 'Realized PnL already includes allocated trading costs; commission totals are descriptive, not deducted twice.',
        'worst_symbols': pnl.head(10).to_dict(), 'invariance_checks': checks,
        'last_risk_actions': audit.tail(4).to_dict('records')})
    flat = []
    for row in summaries:
        item = {k: v for k, v in row.items() if k != 'lifecycle'}
        life = row['lifecycle']
        item.update(termination=life.get('termination_timestamp'), reason=life.get('termination_reason'),
                    last_fill=life.get('last_fill_at'), strategy_inactive_days=life.get('strategy_inactive_days'))
        flat.append(item)
    pd.DataFrame(flat).to_csv(root / 'scenario_comparison.csv', index=False)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(13, 12))
    for name in ('legacy', 'main_1', 'cost_2', 'cost_3', 'cost_5'):
        data = curve(name)
        axes[0].plot(pd.to_datetime(data.index), data.equity, label=name)
    axes[0].set_ylabel('USDT'); axes[0].legend()
    axes[1].plot(pd.to_datetime(delta.index), delta)
    axes[1].set_ylabel('Recovery minus legacy, USDT')
    rolling = [r for r in flat if r['name'].startswith('rolling_')]
    axes[2].bar([r['name'].replace('rolling_', '') for r in rolling], [r['return_pct'] for r in rolling])
    axes[2].set_ylabel('Independent window return %')
    for ax in axes:
        ax.grid(alpha=.2)
    fig.tight_layout(); fig.savefig(root / 'deep_comparison.png', dpi=160); plt.close(fig)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    logging.disable(logging.CRITICAL)
    frames, inventory, lifecycle = load_inputs(ROOT / 'reports/revalidation_60_20260908')
    frozen = protocol(frames, lifecycle)
    timeline = pd.to_datetime(frozen['timeline'])
    scenarios = []
    def add(name, **kwargs):
        scenarios.append({'name': name, **kwargs})
    for n in range(1, 4):
        add(f'main_{n}')
    add('legacy', policy='legacy')
    for name, (start, end) in frozen['segments'].items():
        add(name, start=start, end=end, forced=True)
    for i, window in enumerate(frozen['rolling_windows']):
        add(f'rolling_{i:02d}', start=timeline[window['test_start']], end=timeline[window['test_end'] - 1], forced=True)
    for mult in (1.5, 2, 3, 5):
        add(f'cost_{mult}', multiplier=mult)
    add('end_exit_sensitivity', forced=True)
    add('reversed_symbols', transform='reverse')
    add('prefix_2023', end='2023-12-21')
    add('prefix_2024', end='2024-01-06')
    for year in (2020, 2021, 2022, 2023, 2024, 2025):
        add(f'fresh_start_{year}', start=f'{year}-01-01', forced=True)
    add('liquidity_quarter', transform='quarter_volume')
    add('missing_one_percent', transform='missing')
    add('market_outage_7d', transform='outage')
    frozen.update(scenarios=scenarios, data_manifest=inventory,
                  additional_stress='5x costs, quarter volume, deterministic missing bars, seven-day market outage',
                  no_optimization=True, all_history_retrospective=True,
                  outage_dates=['2023-12-23', '2023-12-29'], missing_seed=42)
    save(root / 'frozen_protocol.json', frozen)
    old = json.loads((ROOT / 'reports/revalidation_60_20260908/frozen_protocol.json').read_text(encoding='utf-8'))['parameters']['strategy_health']
    summaries, failures, digests = [], [], []
    for scenario in scenarios:
        spec = dict(scenario)
        name = spec.pop('name')
        policy = spec.pop('policy', None)
        transform = spec.pop('transform', None)
        current = deepcopy(frozen)
        data = frames
        if policy == 'legacy':
            current['parameters']['strategy_health'] = old
        if transform == 'reverse':
            data = dict(reversed(list(frames.items())))
        elif transform:
            data = {}
            rng = np.random.default_rng(42)
            for symbol, frame in frames.items():
                altered = frame.copy()
                if transform == 'quarter_volume':
                    altered['volume'] *= .25
                elif transform == 'missing':
                    drop = rng.random(len(altered)) < .01
                    drop &= ~altered.scheduled_exit.to_numpy(dtype=bool)
                    drop[0] = drop[-1] = False
                    altered = altered.loc[~drop]
                elif transform == 'outage':
                    altered = altered.loc[~((altered.index >= '2023-12-23') & (altered.index <= '2023-12-29'))]
                data[symbol] = DataHandler.annotate_quality(altered)
            save(root / f'{name}_input_hashes.json', {s: sha256_frame(f) for s, f in data.items()})
        try:
            result, row = run_one(name, root, data, current, **spec)
            summaries.append(row)
            if name.startswith('main_'):
                digests.append(result['_digest'])
            if name in ('main_1', 'final20'):
                save(root / f'{name}_concentration.json', concentration(result))
            del result
        except Exception as exc:
            failures.append({'name': name, 'type': type(exc).__name__, 'error': str(exc), 'traceback': traceback.format_exc()})
            print(f'FAILED {name}: {type(exc).__name__}: {exc}', flush=True)
        save(root / 'progress.json', {'completed': len(summaries), 'planned': len(scenarios), 'failures': failures})
        save(root / 'all_run_summaries.json', summaries)
    save(root / 'determinism.json', {'passed': len(digests) == 3 and digests[0] == digests[1] == digests[2], 'runs': digests})
    checks = analyse(root, summaries)
    write_research_outputs(root, frames, summaries)
    save(root / 'completion.json', {'runs_planned': len(scenarios), 'runs_completed': len(summaries), 'failures': failures,
        'source_unchanged': source_hashes() == frozen['source_hashes'], 'invariance_checks': checks,
        'admission': 'paused_revalidation', 'research_admitted': False})


if __name__ == '__main__':
    main()
