"""Frozen historical return comparison with continuous-account annual selection."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from analysis.paper_portfolio import PaperSpec, run_paper_replay
from analysis.paper_study import WINDOWS, load_verified_inputs, write_json
from analysis.research_evidence import candidate_panel_evidence
from analysis.return_followup import followup_specs, trend_targets, annual_selection_targets, paired_excess_diagnostic
from core.data_versions import DataVersionStore
from core.reproducibility import sha256_file
from scripts.run_paper_roadmap import save_run


def source_identity():
    paths = list((ROOT / 'core').rglob('*.py'))
    names = ['analysis/paper_portfolio.py', 'analysis/return_followup.py', 'analysis/paper_study.py',
        'analysis/paper_validation.py', 'analysis/research_evidence.py', 'analysis/paper_risk.py',
        'scripts/run_return_followup.py', 'scripts/run_paper_roadmap.py', 'config/params.yaml', 'requirements.lock.txt']
    paths.extend(ROOT / name for name in names)
    return {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in sorted(set(paths)) if p.is_file()}


def frozen_frames(output):
    registration = json.loads((output / 'registration.json').read_text(encoding='utf-8'))
    store = DataVersionStore(output / 'inputs')
    return {symbol: pd.read_csv(BytesIO(store.read_file(registration['input_snapshot'], entry['file'])),
        index_col='timestamp', parse_dates=True, float_precision='round_trip')
        for symbol, entry in registration['input_identity']['symbols'].items()}


def worker(output_string, job):
    logging.disable(logging.CRITICAL)
    output = Path(output_string)
    data = frozen_frames(output)
    targets = trend_targets(data) if job['spec']['name'].startswith('ensemble_equal') else None
    result = run_paper_replay(data, job['spec'], start=job['start'], end=job['end'],
        cost_multiplier=job['cost_multiplier'], initial_capital=job['capital'], target_overrides=targets)
    return save_run(output, job, result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 4:
        raise ValueError('bounded worker count required')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    _, identity = load_verified_inputs(ROOT / 'data/binance/1d/_manifest.json')
    files = {entry['file']: entry['path'] for entry in identity['symbols'].values()}
    files['manifest.json'] = ROOT / 'data/binance/1d/_manifest.json'
    files['prior_comparison.csv'] = ROOT / 'reports/paper_roadmap_20261003_v3/comparison.csv'
    snapshot = DataVersionStore(output / 'inputs').freeze_files('return-followup-history', files,
        observed_at=datetime.now(timezone.utc).isoformat(), metadata={'retrospective_only': True})
    assert snapshot['files']['manifest.json']['sha256'] == identity['manifest_sha256']
    assert all(snapshot['files'][entry['file']]['sha256'] == entry['sha256'] for entry in identity['symbols'].values())
    windows = (*WINDOWS, ('continuous', '2021-01-01', '2026-09-19'))
    jobs = [{'id': f'{window}__{spec.name}__cost{cost:g}', 'window': window, 'start': start, 'end': end,
        'spec': asdict(spec), 'cost_multiplier': cost, 'capital': 10000., 'purpose': 'comparison'}
        for window, start, end in windows for spec in followup_specs() for cost in (1., 1.5)]
    source = source_identity()
    protocol = {'schema': 'return-followup/v1', 'registered_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'fixed two-asset retrospective method comparison, not production or unseen validation',
        'input_identity': identity, 'input_snapshot': snapshot['snapshot_id'], 'source_hashes': source,
        'prior_comparison_sha256': snapshot['files']['prior_comparison.csv']['sha256'],
        'jobs': jobs, 'annual_selection': {'family': ['trend_20', 'trend_60', 'trend_120'],
            'initial_year': 'trend_60', 'train': 'previous calendar year', 'cutoff_days_before_year': 35,
            'minimum_days': 250, 'criterion': 'maximum positive daily-return Sharpe, cash otherwise',
            'ties': 'registered family order', 'execution': 'one continuous spot account, same weekly schedule',
            'cost_stress': 'freeze choices at base cost then repeat same targets at 1.5x cost'},
        'ensemble': '0.9 / number of assets * mean(close > close.shift(h), h=20,60,120)',
        'statistical_family': 'all noncash accounts; cash retained as account control but excluded from undefined zero-volatility Sharpe trials',
        'global_search_history_complete': False, 'independent_holdout_passed': False,
        'historical_pit_certified': False, 'real_execution_calibrated': False}
    write_json(output / 'registration.json', protocol)
    rows, failures = [], []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(worker, str(output), job): job for job in jobs}
        for completed in as_completed(futures):
            job = futures[completed]
            try:
                rows.append(completed.result())
                print(f"completed {len(rows)}/{len(jobs)}: {job['id']}", flush=True)
            except Exception as exc:
                failures.append({'job_id': job['id'], 'error': repr(exc)})
    write_json(output / 'failures.json', failures)
    if failures:
        raise RuntimeError('registered jobs failed; all failures preserved')
    def returns(name, cost=1.):
        path = output / 'runs' / f'continuous__{name}__cost{cost:g}' / 'equity.csv'
        return pd.read_csv(path, index_col='timestamp', parse_dates=True)['return'].rename(name)
    training = pd.concat([returns(name) for name in ('trend_20', 'trend_60', 'trend_120')], axis=1)
    targets, selection_log = annual_selection_targets(frozen_frames(output), training)
    write_json(output / 'annual_selection.json', selection_log)
    targets.to_csv(output / 'annual_targets.csv', index_label='timestamp')
    for cost in (1., 1.5):
        spec = PaperSpec(name='annual_selected', lookback=120)
        job = {'id': f'continuous__annual_selected__cost{cost:g}', 'window': 'continuous',
            'start': '2021-01-01', 'end': '2026-09-19', 'spec': asdict(spec),
            'capital': 10000., 'cost_multiplier': cost, 'purpose': 'chronological_selection'}
        result = run_paper_replay(frozen_frames(output), spec, start=job['start'], end=job['end'],
            target_overrides=targets, cost_multiplier=cost)
        rows.append(save_run(output, job, result))
    flat = pd.DataFrame([{k: v for k, v in row.items() if k != 'risk'} for row in rows]).sort_values(['window','name','cost_multiplier'])
    flat.to_csv(output / 'comparison.csv', index=False)
    old = pd.read_csv(BytesIO(DataVersionStore(output / 'inputs').read_file(snapshot['snapshot_id'], 'prior_comparison.csv')))
    matches = []
    for row in rows:
        if row['name'] not in {'cash', 'equal_weight', 'trend_20', 'trend_60', 'trend_120', 'trend_60_no_trade'} or row['window'] == 'continuous':
            continue
        previous = old[(old.name == row['name']) & (old.window == row['window']) & (old.cost_multiplier == row['cost_multiplier']) & (old.initial_capital == 10000.)].iloc[0]
        fields = ('net_return', 'max_drawdown', 'commission', 'slippage', 'turnover_notional', 'fill_count')
        differences = {key: float(row[key]-previous[key]) for key in fields}
        matches.append({'job_id': row['job_id'], 'differences': differences,
            'unchanged': all(np.isclose(row[key], previous[key], rtol=1e-10, atol=1e-8) for key in fields)})
    write_json(output / 'prior_reproduction.json', matches)
    panel = pd.concat([returns(spec.name) for spec in followup_specs()], axis=1)
    panel['annual_selected'] = returns('annual_selected')
    panel.to_csv(output / 'continuous_returns.csv', index_label='timestamp')
    baseline = panel['trend_60']
    statistics = {'local_family': candidate_panel_evidence({c: panel[c] for c in panel if c != 'cash'},
        benchmark_returns=baseline, registered_before_results=True, bootstrap_iterations=1000),
        'paired_vs_trend60': {name: paired_excess_diagnostic(panel[name], baseline) for name in panel if name != 'trend_60'}}
    write_json(output / 'statistics.json', statistics)
    years = []
    for name in panel:
        for year, group in panel[name].groupby(panel.index.year):
            years.append({'name': name, 'year': int(year), 'net_return': float((1+group).prod()-1),
                'scope': 'calendar slice of the same continuous account; not independently reset returns'})
    pd.DataFrame(years).to_csv(output / 'continuous_years.csv', index=False)
    if source != source_identity():
        raise ValueError('experiment source changed while executing')
    summary = {'status': 'completed_retrospective_comparison', 'runs': len(rows),
        'all_accounting_passed': all(r['accounting_ok'] for r in rows),
        'prior_comparisons': len(matches), 'prior_results_unchanged': all(r['unchanged'] for r in matches),
        'independent_holdout_passed': False, 'real_execution_calibrated': False,
        'source_unchanged': True, 'production_config_changed': False, 'live_orders_submitted': 0}
    write_json(output / 'summary.json', summary)
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
