"""Pre-registered retrospective controls study. Never changes live admission."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import pandas as pd

from analysis.overlay_study import run_diagnostics, health_reference_attribution
from composition.factory import build_strategy_registry
from config.config import config
from core.reproducibility import sha256_file, sha256_frame
from core.strategy_registration import register_health_policies
from scripts.run_revalidation60 import load_inputs, run_one, save, source_hashes


ARMS = ('baseline', 'regime_entry_all', 'regime_exit_off', 'regime_entry_all_exit_off',
        'health_off', 'health_single_probation', 'budget_no_reduction',
        'budget_confirmed_stop', 'budget_review_5bars')


def arm_parameters(base, arm):
    if arm not in ARMS:
        raise ValueError('Unknown registered study arm')
    params = deepcopy(base)
    params.setdefault('research', {}).update(
        entry_audit=True, experiment_id=f'strategy-p0-p1-v1:{arm}',
        overlay_study='strategy-p0-p1-v1')
    research = params['research']
    if arm.startswith('regime_'):
        controls = {}
        if 'entry_all' in arm:
            states = ['TREND_UP','TREND_DOWN','SIDEWAYS','VOLATILE']
            controls['entry_states'] = states
            controls['exit_states'] = ['TREND_UP']
            params['routing'] = {state:'TrendBreakout' for state in states}
        if 'exit_off' in arm:
            controls['exit_on_disallowed_state'] = False
        research['regime_controls'] = {'TrendBreakout': controls}
    if arm == 'health_off':
        research['strategy_health_overrides'] = {'TrendBreakout': {'enabled': False}}
    if arm == 'health_single_probation':
        research['strategy_health_overrides'] = {'TrendBreakout': {
            'unified_recovery': False, 'recovery_stages': [],
            'repeated_failure_action': 'manual_lock'}}
    if arm == 'budget_no_reduction':
        params['drawdown_budget']['reduction_enabled'] = False
    if arm == 'budget_confirmed_stop':
        params['drawdown_budget']['stop_basis'] = 'confirmed_protective'
    if arm == 'budget_review_5bars':
        params['drawdown_budget']['review_interval_bars'] = 5
    return params


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'reports/strategy_p0_p1_20261002')
    parser.add_argument('--inputs', type=Path, default=ROOT/'reports/strategy_review_20260919/frozen_inputs')
    parser.add_argument('--windows', nargs='+', choices=['validation20','final20'], default=['validation20','final20'])
    parser.add_argument('--arms', nargs='+', choices=ARMS, default=list(ARMS))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    frames, inventory, _ = load_inputs(args.inputs)
    reference = json.loads((ROOT/'reports/strategy_review_20260919/reference_protocol.json').read_text(encoding='utf-8'))
    base = deepcopy(config._config)
    jobs = []
    prior = config._config
    try:
        for window in args.windows:
            for arm in args.arms:
                params = arm_parameters(base, arm)
                config._config = params
                registry = build_strategy_registry(config)
                start, end = [pd.Timestamp(value).tz_localize(None) for value in reference['segments'][window]]
                eligible = {s for s,f in frames.items() if not f.loc[start:end].empty}
                registration = register_health_policies(registry, eligible, params['routing'], research=params['research'])
                jobs.append({'name':f'{arm}_{window}', 'window':window, 'arm':arm,
                             'start':reference['segments'][window][0], 'end':reference['segments'][window][1],
                             'parameters':params, 'health_registration':registration})
    finally:
        config._config = prior
    identity = {'schema':'strategy-p0-p1-study/v1', 'source_hashes':source_hashes(),
                'config_sha256':sha256_file(ROOT/'config/params.yaml'),
                'data_manifest_sha256':sha256_file(args.inputs/'data_manifest.json'),
                'engine_input_hashes':{s:sha256_frame(f) for s,f in frames.items()},
                'jobs':jobs, 'forced_end_liquidation':True,
                'interpretation':'Retrospective, already observed history. No independent holdout or live admission.',
                'health_comparison':'Current fixed staged recovery, disabled health, registered single-probation reference; no parameter sweep.',
                'budget_comparison':'Admission budget and all hard account ceilings remain enforced; only reductions or confirmed stop measurement vary.',
                'scope':'P0 defect verification and isolated P1 control attribution, not alpha selection.'}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output/'registration.json'
    if manifest.exists():
        if json.loads(manifest.read_text(encoding='utf-8')) != identity:
            raise ValueError('Source/config/data/job identity changed; use a new study output directory')
    else:
        save(manifest, identity)
    for job in jobs:
        _, summary = run_one(job['name'], args.output, frames,
                             {**reference, 'parameters':job['parameters']},
                             start=job['start'], end=job['end'], forced=True)
        snapshots = json.loads((args.output/'runs'/job['name']/'strategy_health.json').read_text(encoding='utf-8'))
        for strategy, registered in job['health_registration'].items():
            if snapshots[strategy]['policy_registration'] != registered:
                raise ValueError('Runtime health policy/universe differs from pre-registration')
        save(args.output/'runs'/job['name']/'health_registration.json', job['health_registration'])
    if source_hashes() != identity['source_hashes'] or sha256_file(ROOT/'config/params.yaml') != identity['config_sha256']:
        raise ValueError('Source/config changed during study; results cannot be compared')
    diagnostics = {job['name']:run_diagnostics(args.output/'runs'/job['name']) for job in jobs}
    rows = []
    for job in jobs:
        values = diagnostics[job['name']]
        baseline = diagnostics.get(f"baseline_{job['window']}")
        rows.append({'name':job['name'], 'window':job['window'], 'arm':job['arm'],
                     **{key:value for key,value in values.items() if not isinstance(value,(dict,list))},
                     'net_equity_delta_vs_baseline':values['final_equity']-baseline['final_equity'] if baseline else None})
    pd.DataFrame(rows).to_csv(args.output/'comparison.csv',index=False)
    attribution = {}
    for window in args.windows:
        ref = args.output/'runs'/f'health_off_{window}'
        if not (ref/'summary.json').exists():
            continue
        for arm in ('baseline','health_single_probation'):
            folder = args.output/'runs'/f'{arm}_{window}'
            if (folder/'summary.json').exists():
                attribution[f'{arm}_{window}'] = health_reference_attribution(folder,ref)
    save(args.output/'results.json',{'diagnostics':diagnostics,'health_reference_attribution':attribution,
                                   'completed_runs':len(jobs),'accounting_ok':all(v['accounting_ok'] for v in diagnostics.values())})
    print(pd.DataFrame(rows)[['name','return_pct','max_drawdown_pct','budget_reduce_actions']].to_string(index=False),flush=True)


if __name__ == '__main__':
    main()
