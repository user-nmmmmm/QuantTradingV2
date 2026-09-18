"""Frozen 79-engine-run remediation batch: 15 baseline + 64 revised runs."""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from core.reproducibility import canonical_json, sha256_file, sha256_frame
from backtest.reporting.operating_periods import requested_period_curve, split_execution_records
from scripts.run_revalidation60 import (
    load_inputs, protocol, run_one, save, source_hashes, concentration,
    write_research_outputs, START, END,
)


def scenarios(frozen):
    timeline = pd.to_datetime(frozen['timeline'])
    specs = [dict(name=f'main_{i}') for i in (1, 2, 3)]
    rolling = [dict(name=f'rolling_{i:02d}', start=timeline[w['test_start']],
                    end=timeline[w['test_end']-1], forced=True)
               for i, w in enumerate(frozen['rolling_windows'])]
    assert len(rolling) == 11
    specs += rolling
    specs += [dict(name=k, start=s, end=e, forced=True) for k, (s, e) in frozen['segments'].items()]
    specs += [dict(name=f'cost_{m}', multiplier=m) for m in (1.5, 2, 3)]
    specs += [dict(name=f'fresh_{y}', start=f'{y}-01-01') for y in (2022, 2023, 2024, 2025)]
    specs += [dict(name='end_exit_sensitivity', forced=True),
              dict(name='reversed_symbols', transform='reversed_symbols'),
              dict(name='prefix_2023', end='2023-12-21'),
              dict(name='prefix_2024', end='2024-01-06')]
    specs += [dict(name=k, transform=k) for k in ('liquidity_quarter', 'missing_one_percent', 'market_outage_7d')]
    for ablation in ('no_obv_confirmation', 'no_regime_restrictions', 'no_health'):
        specs += [{**s, 'name': f'{ablation}_{s["name"]}', 'ablation': ablation} for s in rolling]
    assert len(specs) == 64 and len({s['name'] for s in specs}) == 64
    return specs


def transform_frames(frames, name):
    if name is None:
        return frames
    if name == 'reversed_symbols':
        return dict(reversed(list(frames.items())))
    result = {}
    rng = np.random.default_rng(42)
    for symbol, original in frames.items():
        frame = original.copy()
        if name == 'liquidity_quarter':
            frame['volume'] *= .25
        elif name == 'missing_one_percent':
            remove = rng.random(len(frame)) < .01
            # Preserve known mandatory venue-exit facts, as in the earlier batch.
            remove &= ~frame['scheduled_exit'].to_numpy(dtype=bool)
            frame = frame.loc[~remove].copy()
        elif name == 'market_outage_7d':
            frame = frame.loc[~((frame.index >= '2023-12-23') & (frame.index <= '2023-12-29'))].copy()
        else:
            raise ValueError(name)
        result[symbol] = frame
    return result


def ablated_protocol(frozen, name):
    item = deepcopy(frozen)
    if name:
        cfg = item['parameters']
        cfg['research']['strategy_ablation'] = name
        if name == 'no_health':
            cfg['strategy_health']['enabled'] = False
        elif name == 'no_regime_restrictions':
            cfg['routing'] = {key: 'TrendBreakout' for key in cfg['routing']}
    return item


def read_csv(path):
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def actual_trades(root, name):
    folder = root / 'runs' / name
    frame = read_csv(folder / 'trades.csv')
    cfg = json.loads((folder / 'resolved_config.json').read_text(encoding='utf-8'))
    if not frame.empty and cfg['backtest']['end_of_backtest_mode'] == 'mark_to_market':
        frame = frame.loc[frame.exit_reason.ne('EndOfBacktest')]
    return frame


def comparable_fills(frame, cutoff=None):
    if frame.empty:
        return []
    frame = frame.copy()
    time = pd.to_datetime(frame['fill_time'], utc=True)
    if cutoff is not None:
        frame = frame.loc[time < pd.Timestamp(cutoff, tz='UTC') + pd.Timedelta(days=1)]
    # IDs include canonical sequence identifiers; compare economic facts as
    # well as full deterministic engine digests in their separate checks.
    cols = [c for c in ('symbol', 'side', 'qty', 'fill_price', 'commission', 'fill_time',
                       'exit_reason', 'strategy_id', 'approved_risk_amount') if c in frame]
    return sorted(canonical_json(row) for row in frame[cols].to_dict('records'))


def publish(root, frames, revised):
    """Descriptive analysis only: never selects or mutates execution rules."""
    baseline = json.loads((root / 'baseline_summaries.json').read_text(encoding='utf-8'))
    for row in baseline:
        folder = root/'runs'/row['name']
        normalized = folder/'normalized'; normalized.mkdir(exist_ok=True)
        activity = read_csv(folder/'strategy_activity.csv')
        if not activity.empty:
            activity['strategy_states'] = activity.strategy_states.map(ast.literal_eval)
            activity['account_action'] = activity.account_blocks_new_risk.map(
                {True:'blocks_new_risk',False:'not_blocked_exact_action_unavailable'})
        raw = read_csv(folder/'trades.csv')
        actual,marks = split_execution_records(raw.to_dict('records'),mark_to_market=not row['forced_exit'])
        pd.DataFrame(actual,columns=raw.columns).to_csv(normalized/'trades.csv',index=False)
        pd.DataFrame(marks,columns=raw.columns).to_csv(normalized/'valuation_transfers.csv',index=False)
        curve = pd.read_csv(folder/'equity_engine.csv',index_col='timestamp',parse_dates=True)
        requested_period_curve(curve,row['start'],row['end'],capital=10000,lifecycle=row['lifecycle'],
            activity=activity.to_dict('records')).to_csv(normalized/'equity_requested_period.csv',index_label='timestamp')
    all_rows = baseline + revised
    save(root / 'all_run_summaries.json', all_rows)
    pd.json_normalize(all_rows).to_csv(root / 'all_run_summaries.csv', index=False)
    digests = [json.loads((root / f'runs/main_{i}/digest.json').read_text()) for i in (1, 2, 3)]
    save(root / 'determinism.json', {'passed': digests[0] == digests[1] == digests[2], 'runs': digests})
    main_fills = actual_trades(root, 'main_1')
    checks = {'determinism': digests[0] == digests[1] == digests[2],
              'symbol_order_economic_fills': comparable_fills(main_fills) == comparable_fills(actual_trades(root, 'reversed_symbols')),
              'all_accounting_identities': all(r['accounting_ok'] for r in all_rows)}
    for name, cutoff in (('prefix_2023', '2023-12-21'), ('prefix_2024', '2024-01-06')):
        checks[name] = comparable_fills(main_fills, cutoff) == comparable_fills(actual_trades(root, name), cutoff)
    records = []
    budget_checks = []
    for row in all_rows:
        name = row['name']; folder = root / 'runs' / name
        events = json.loads((folder / 'close_events.json').read_text())
        stats = concentration({'close_events': events}, mark_to_market=not row['forced_exit'])
        save(folder / 'cohort_concentration.json', stats)
        trades = actual_trades(root, name)
        ids = [event['close_event_id'] for event in events]
        if len(set(ids)) != len(ids):
            raise ValueError(f'Duplicate close event: {name}')
        # Assert executions have a real bar, including transformed-data stress.
        stress = name if name in {'liquidity_quarter', 'missing_one_percent', 'market_outage_7d'} else None
        source = transform_frames(frames, stress)
        for trade in trades.to_dict('records'):
            day = pd.Timestamp(trade['fill_time']).tz_localize(None).normalize()
            if day not in source[trade['symbol']].index:
                raise ValueError(f'Execution without observed market bar: {name}, {trade}')
        observations = read_csv(folder / 'entry_observations.csv')
        if not observations.empty and 'reason' in observations:
            observations.groupby('reason', dropna=False).size().to_csv(folder / 'signal_reason_counts.csv', header=['count'])
        if events:
            event_frame = pd.DataFrame(events)
            event_frame.groupby('exit_reason').agg(events=('close_event_id','size'),
                realized_pnl=('realized_pnl','sum')).to_csv(folder / 'exit_attribution.csv')
        records.append({**{k: row[k] for k in ('name','return_pct','max_drawdown_pct','final_equity')},
            'actual_fills': len(trades), 'cohorts': stats['cohort_count'],
            'cohort_pf': stats['scenarios']['0']['profit_factor'],
            'pf_ci': stats['scenarios']['0']['pf_95pct_ci'],
            'remove_top5_pnl': stats['scenarios']['5']['remaining_net_closed_pnl'],
            'remove_top10_pnl': stats['scenarios']['10']['remaining_net_closed_pnl']})
        audit_path=folder/'drawdown_budget_audit.csv'
        if audit_path.exists():
            audit=read_csv(audit_path)
            if not audit.empty:
                approvals=audit.loc[audit.event.eq('entry_approval') & audit.approved.eq(True)] if 'approved' in audit else pd.DataFrame()
                safe=bool((approvals.requested_risk_with_cost <= approvals.available+1e-7).all()) if not approvals.empty else True
                budget_checks.append({'name':name,'final_approvals_within_budget':safe,
                    'reviews_over_budget':int(audit.over_budget.eq(True).sum()),
                    'unverifiable_reviews':int(audit.verifiable.eq(False).sum()),
                    'reduction_actions':int(audit.get('action',pd.Series(dtype=str)).eq('reduce').sum())})
    checks['actual_fills_on_real_bars'] = True
    checks['unique_close_events'] = True
    checks['final_approvals_within_budget'] = all(r['final_approvals_within_budget'] for r in budget_checks)
    save(root/'drawdown_budget_checks.json',budget_checks)
    outage = pd.read_csv(root / 'runs/market_outage_7d/equity_requested_period.csv', index_col='timestamp', parse_dates=True).loc['2023-12-23':'2023-12-29']
    checks['seven_day_outage_disclosed'] = len(outage) == 7 and (outage.phase == 'market_data_missing').all() and outage.valuation_stale.all()
    save(root / 'experiment_checks.json', {**checks, 'passed': all(checks.values())})
    comparison = pd.DataFrame(records)
    comparison.to_csv(root / 'comparison.csv', index=False)
    pairs = [('baseline_main', 'main_1')] + [(f'baseline_rolling_{i:02d}', f'rolling_{i:02d}') for i in range(11)] + [(f'baseline_{k}',k) for k in ('train60','validation20','final20')]
    lookup = comparison.set_index('name')
    paired = []
    for before, after in pairs:
        item = {'baseline':before,'revised':after}
        for field in ('return_pct','max_drawdown_pct','actual_fills','cohorts','cohort_pf'):
            item[f'before_{field}'] = lookup.loc[before,field]
            item[f'after_{field}'] = lookup.loc[after,field]
        paired.append(item)
    pd.DataFrame(paired).to_csv(root / 'before_after.csv', index=False)
    write_research_outputs(root, frames, revised)
    # The legacy report helper treats all EOB records as marks; overwrite the
    # final-window gates with the correct, costed execution-cohort assessment.
    gates = json.loads((root / 'research_gates.json').read_text())
    for name in ('main_1','final20'):
        stat = json.loads((root / 'runs' / name / 'cohort_concentration.json').read_text())
        save(root / f'{name}_cohort_concentration.json', stat)
        s = stat['scenarios']['0']; enough = stat['sample_status'] == 'sufficient_for_estimation'
        gates[name]['cohort_sample'] = stat['sample_status']
        gates[name]['PF_above_1_15_and_block_CI_lower_above_1'] = ('insufficient' if not enough else
            'pass' if s['profit_factor'] is not None and s['profit_factor'] > 1.15 and s['pf_95pct_ci'] and s['pf_95pct_ci'][0] > 1 else 'fail')
        gates[name]['remove_top5_top10_remains_profitable'] = ('insufficient' if not enough else
            'pass' if all(stat['scenarios'][str(n)]['remaining_net_closed_pnl'] > 0 for n in (5,10)) else 'fail')
    save(root / 'research_gates.json', gates)
    plot_comparison(root)


def plot_comparison(root):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2,1,figsize=(13,8),sharex=True)
    for name,label in (('baseline_main','Before remediation'),('main_1','After remediation')):
        curve = pd.read_csv(root / 'runs' / name / 'equity_requested_period.csv', index_col='timestamp', parse_dates=True).equity
        axes[0].plot(curve.index,curve,label=label)
        axes[1].plot(curve.index,(curve/curve.cummax()-1)*100,label=label)
    axes[0].axhline(10000,color='gray',linestyle=':',label='Cash')
    axes[1].axhline(-20,color='red',linestyle=':',label='Liquidation threshold')
    axes[0].set_ylabel('Equity (USDT)'); axes[1].set_ylabel('Drawdown (%)')
    for ax in axes:
        ax.legend(); ax.grid(alpha=.2)
    fig.suptitle('Frozen 60-coin daily experiment | 2016-01-01 to 2026-06-30')
    fig.tight_layout(); fig.savefig(root / 'before_after_equity_drawdown.png',dpi=160); plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT/'reports/strategy_remediation_20260914')
    parser.add_argument('--freeze-only',action='store_true')
    parser.add_argument('--publish-only',action='store_true')
    args = parser.parse_args(); root=args.root.resolve()
    logging.disable(logging.CRITICAL)
    manifest = json.loads((root/'baseline_manifest.json').read_text(encoding='utf-8'))
    baseline_done = json.loads((root/'baseline_completion.json').read_text())
    assert baseline_done == {'completed':15,'frozen_source_unchanged':True}
    for rel,digest in manifest['source_hashes'].items():
        assert sha256_file(root/'baseline_source'/rel)==digest
    frames,inventory,lifecycle = load_inputs(Path(manifest['input_root']))
    frozen = protocol(frames,lifecycle)
    frozen['parameters'].setdefault('research',{})['entry_audit'] = True
    specs=scenarios(frozen)
    frozen.update(scenarios=specs,expected_engine_runs=79,baseline_engine_runs=15,
        minimum_notional_assumption='10 USDT research floor; no historical exchange minimum series',
        recovery_rules_status='Pre-fixed research rules, not established effectiveness')
    frozen['transformed_input_hashes'] = {name:{s:sha256_frame(f) for s,f in transform_frames(frames,name).items()}
        for name in ('liquidity_quarter','missing_one_percent','market_outage_7d')}
    freeze_path=root/'revised_protocol.json'
    if freeze_path.exists():
        if json.loads(freeze_path.read_text(encoding='utf-8')) != json.loads(canonical_json(frozen)):
            raise ValueError('Frozen source/config/data/protocol changed; do not reuse this batch')
    else:
        save(freeze_path,frozen)
    if args.freeze_only:
        return
    checks=json.loads((root/'engineering_gate.json').read_text())
    assert checks['passed'] and checks['source_hashes']==frozen['source_hashes']
    if args.publish_only:
        revised=json.loads((root/'revised_summaries.json').read_text())
        assert len(revised)==64
    else:
        revised=[]
        for spec in specs:
            if source_hashes()!=frozen['source_hashes']:
                raise ValueError('Source mutated during frozen experiment')
            kwargs=dict(spec); transform=kwargs.pop('transform',None); ablation=kwargs.pop('ablation',None)
            _,summary=run_one(root=root,all_frames=transform_frames(frames,transform),
                frozen=ablated_protocol(frozen,ablation),**kwargs)
            revised.append(summary)
            save(root/'revised_summaries.json',revised)
            save(root/'batch_progress.json',{'completed':15+len(revised),'expected':79,'last':spec['name']})
    assert source_hashes()==frozen['source_hashes']
    publish(root,frames,revised)
    save(root/'batch_completion.json',{'completed_engine_runs':79,'baseline_runs':15,'revised_runs':64,
        'source_hashes_unchanged':True,'admission':'paused_revalidation',
        'research_interpretation':'Retrospective fixed protocol; inspect separate statistical gates'})
    print('COMPLETE: 79 engine runs; source/config/data freeze verified',flush=True)


if __name__=='__main__':
    main()
