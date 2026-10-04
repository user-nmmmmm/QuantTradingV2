import numpy as np
import pandas as pd

from analysis.paper_portfolio import PaperSpec, run_paper_replay
from analysis.return_followup import trend_targets, annual_selection_targets
from tests.test_paper_portfolio import frames


def test_targets_are_fractional_cash_allocations_and_future_changes_do_not_rewrite_history():
    data = frames(n=500)
    first = trend_targets(data)
    changed = {s: f.copy() for s, f in data.items()}
    cut = first.index[300]
    for frame in changed.values():
        frame.loc[cut:, 'close'] *= .1
    later = trend_targets(changed)
    pd.testing.assert_frame_equal(first.loc[:cut-pd.Timedelta(days=1)], later.loc[:cut-pd.Timedelta(days=1)])
    assert first.iloc[119].sum() == 0
    assert first.iloc[120].sum() == .9
    assert set(np.round(later.to_numpy().ravel(), 8)) <= {0., .15, .3, .45}


def test_annual_selection_uses_mature_prior_returns_and_is_invariant_to_future_changes():
    data = frames(n=1100)
    index = data['BTC/USDT'].index
    rng = np.random.default_rng(42)
    panel = pd.DataFrame(rng.normal(.001, .005, (len(index), 3)), index=index,
        columns=['trend_20', 'trend_60', 'trend_120'])
    target, log = annual_selection_targets(data, panel, start='2024-01-01', end='2026-09-01')
    revised = panel.copy()
    revised.loc['2025-01-01':, 'trend_120'] = 100.
    new_target, new_log = annual_selection_targets(data, revised, start='2024-01-01', end='2026-09-01')
    assert log[:2] == new_log[:2]
    pd.testing.assert_frame_equal(target.loc[:'2025-12-30'], new_target.loc[:'2025-12-30'])
    for row in log[1:]:
        assert pd.Timestamp(row['last_training_return_available_at']) < pd.Timestamp(row['selection_cutoff'])
        assert pd.Timestamp(row['first_scheduled_execution']).year == row['year']


def test_selected_targets_are_executed_by_one_cash_account_with_fees():
    data = frames(n=800)
    index = data['BTC/USDT'].index
    panel = pd.DataFrame({'trend_20': np.resize([.002, .001], len(index)),
        'trend_60': np.resize([-.001, -.002], len(index)),
        'trend_120': np.resize([-.001, -.002], len(index))}, index=index)
    targets, log = annual_selection_targets(data, panel, start='2024-06-01', end='2026-01-31')
    spec = PaperSpec(name='annual_selected', lookback=120)
    result = run_paper_replay(data, spec, start='2024-06-01', end='2026-01-31', target_overrides=targets)
    assert result['accounting']['ok'] and result['accounting']['commission'] > 0
    assert result['accounting']['minimum_cash'] >= 0
    assert log[1]['selected'] == 'trend_20'
    assert result['fills']['fill_time'].min() > result['fills']['signal_time'].min()


def test_single_horizon_external_targets_match_native_broker_account():
    data = frames(n=210)
    for frame in data.values():
        frame.loc[frame.index[155]:, ['open', 'high', 'low', 'close']] *= .8
    spec = PaperSpec(lookback=60)
    native = run_paper_replay(data, spec)
    external = run_paper_replay(data, spec, target_overrides=trend_targets(data, horizons=(60,)))
    pd.testing.assert_series_equal(native['returns'], external['returns'])
    assert native['accounting'] == external['accounting']


def test_annual_cash_exit_and_reentry_pay_real_switch_costs_on_common_schedule():
    data = frames(n=800)
    index = data['BTC/USDT'].index
    panel = pd.DataFrame({name: np.resize([-.001, -.002], len(index))
                         for name in ('trend_20', 'trend_60', 'trend_120')}, index=index)
    panel.loc['2025-01-01':, 'trend_20'] *= -1
    targets, log = annual_selection_targets(data, panel, start='2024-01-01', end='2026-01-31')
    assert [row['selected'] for row in log] == ['trend_60', 'cash', 'trend_20']
    result = run_paper_replay(data, PaperSpec(name='annual_selected', lookback=120),
        start='2024-01-01', end='2026-01-31', target_overrides=targets)
    for row, side in ((log[1], 'sell'), (log[2], 'buy')):
        at = pd.Timestamp(row['first_scheduled_execution'])
        fills = result['fills']
        matched = fills[(fills.fill_time == at) & (fills.side == side)]
        assert len(matched) == 2 and (matched.commission > 0).all()
    assert result['accounting']['ok'] and result['accounting']['minimum_cash'] >= 0
