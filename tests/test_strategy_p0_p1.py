"""Regression coverage for order truth and isolated overlay research controls."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.drawdown_budget import BacktestDrawdownReducer
from composition.factory import build_strategy_registry, build_router, build_risk_manager
from config.config import config
from core.broker import Broker
from core.domain import OrderStatus, OrderIntent, OrderSubmissionResult
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.protective_orders import ProtectiveOrder, authoritative_position_ids
from core.state import MarketState
from core.state_store_v2 import StateStore
from core.strategy_registration import register_health_policies
from strategies.base import Strategy
from tests.test_strategy_remediation import setup_budget, machine, probation, cohorts


class ExitStrategy(Strategy):
    def __init__(self):
        super().__init__('ExitTest', {MarketState.TREND_UP})

    def should_enter(self, *args):
        return None

    def should_exit(self, *args):
        return {'action': 'sell', 'reason': 'Regime VOLATILE Not Allowed'}


def exit_case():
    p = Portfolio(10000)
    p.update_position('A', 10, 100, stop_price=90, strategy_id='ExitTest')
    b = Broker(p, commission_rate=0, slippage=0, max_participation_rate=.1)
    df = pd.DataFrame(dict(open=[100.]*5, high=[100.]*5, low=[100.]*5,
                           close=[100.]*5, volume=[50.]*5),
                      index=pd.date_range('2024-01-01', periods=5))
    return ExitStrategy(), p, b, df


def submit(s, p, b, df, i):
    return s.process_exit_only('A', i, df, MarketState.VOLATILE, p, b)


def test_budget_cancel_releases_exit_latch_and_resizes_remaining_inventory():
    s, p, b, df = exit_case()
    first = submit(s, p, b, df, 0)
    b.force_liquidate({'A': df.iloc[1]}, timestamp=df.index[1],
                      reason='DrawdownBudgetReduce', remaining_fraction=.5,
                      risk_action_id='budget-cancel')
    assert first.status is OrderStatus.CANCELED
    second = submit(s, p, b, df, 2)
    assert second.id != first.id
    assert second.qty == abs(p.get_position('A')['qty'])
    assert s.get_context('A')['exit_order_id'] == second.id
    assert any(row['order_id'] == first.id and row['latch_released']
               for row in s.exit_lifecycle_audit)


def test_partial_exit_waits_then_canceled_remainder_retries_only_remaining_qty():
    s, p, b, df = exit_case()
    first = submit(s, p, b, df, 0)
    b.process_orders({'A': df.iloc[1]})
    assert first.status is OrderStatus.PARTIALLY_FILLED
    assert submit(s, p, b, df, 1) is None
    assert s.get_context('A')['exit_order_remaining_qty'] == 5
    b.cancel_symbol_orders('A')
    second = submit(s, p, b, df, 2)
    assert second.qty == 5


@pytest.mark.parametrize('status', [OrderStatus.CANCELED, OrderStatus.REJECTED,
                                   OrderStatus.EXPIRED, OrderStatus.FILLED])
def test_confirmed_terminal_exit_allows_reevaluation(status):
    s, p, b, df = exit_case()
    first = submit(s, p, b, df, 0)
    b.pending_orders.remove(first)
    b._set_status(first, status, df.index[1])
    assert submit(s, p, b, df, 1).qty == 10


def test_unknown_missing_and_cancel_pending_never_create_duplicate_exits():
    s, p, b, df = exit_case()
    first = submit(s, p, b, df, 0)
    for status in (OrderStatus.UNKNOWN, OrderStatus.CANCEL_PENDING):
        b._set_status(first, status, df.index[1])
        assert submit(s, p, b, df, 1) is None
    b.orders_by_id.pop(first.id)
    assert submit(s, p, b, df, 2) is None
    assert len(b.pending_orders) == 1


def test_restart_restores_exit_identity_and_uses_durable_live_terminal_fact(tmp_path):
    s, p, _, df = exit_case()
    ledger = OrderStore(str(tmp_path / 'orders.db'))
    state = StateStore(str(tmp_path / 'state.db'))
    intent = OrderIntent(exchange='offline', account='test', symbol='A', timeframe='1d',
                         bar_time=df.index[0].isoformat(), strategy_id=s.name,
                         action='sell', sequence=1, requested_qty=10, price=100)
    ledger.create_intent(intent, df.index[0].isoformat())
    ledger.transition(intent.client_order_id, OrderStatus.SUBMITTING, df.index[0].isoformat())
    ledger.transition(intent.client_order_id, OrderStatus.UNKNOWN, df.index[0].isoformat())
    s.bind_state_store(state)
    result = OrderSubmissionResult(intent.client_order_id, OrderStatus.UNKNOWN, 10, remaining_qty=10)
    s._track_exit('A', result, p)
    restored = ExitStrategy()
    restored.bind_state_store(state)
    venue = SimpleNamespace(order_store=ledger, close_events=[])
    assert submit(restored, p, venue, df, 1) is None
    ledger.transition(intent.client_order_id, OrderStatus.CANCELED, df.index[1].isoformat())
    restored._reconcile_exit('A', p, venue)
    assert not restored.get_context('A').get('exit_pending')
    ledger.close()
    state.close()


def test_regime_entry_widening_does_not_disable_original_regime_exit(monkeypatch):
    params = deepcopy(config._config)
    states = [s.name for s in MarketState]
    params['research'] = {'experiment_id': 'test-controls', 'regime_controls': {
        'TrendBreakout': {'entry_states': states}}}
    params['routing'] = {s: 'TrendBreakout' for s in states}
    monkeypatch.setattr(config, '_config', params)
    strategy = build_strategy_registry(config)['TrendBreakout']
    build_router({'TrendBreakout': strategy}, config)
    assert strategy.allowed_states == set(MarketState)
    assert strategy.regime_requires_exit(MarketState.VOLATILE)
    strategy.exit_on_disallowed_state = False
    assert not strategy.regime_requires_exit(MarketState.VOLATILE)
    assert strategy.allowed_states == set(MarketState)


def test_unreachable_health_is_rejected_and_explicit_policy_is_frozen(monkeypatch):
    registry = build_strategy_registry(config)
    with pytest.raises(ValueError, match='Unreachable health recovery'):
        register_health_policies(registry, ['BTC', 'ETH'], config.get('routing'))
    params = deepcopy(config._config)
    params['research'] = {'experiment_id': 'two-symbol-policy', 'strategy_health_overrides': {
        'TrendBreakout': {'probation_min_distinct_symbols': 2}}}
    monkeypatch.setattr(config, '_config', params)
    registry = build_strategy_registry(config)
    registration = register_health_policies(registry, ['BTC', 'ETH'], params['routing'],
                                           research=params['research'])
    h = registry['TrendBreakout'].health
    assert h.policy.probation_min_distinct_symbols == 2
    assert h.policy.probation_required_cohorts == 5
    assert h.policy.recovery_stage_min_days == 30
    assert h.policy.probation_require_positive_without_best
    assert h.to_dict()['policy_registration'] == registration['TrendBreakout']
    saved = h.to_dict()
    h.load(saved)
    h.registration = {**h.registration, 'sha256': 'changed'}
    with pytest.raises(ValueError, match='identity mismatch'):
        h.load(saved)


def test_two_symbol_stages_still_require_fresh_cohorts_time_and_unconcentrated_gains():
    h = machine()
    h.policy = replace(h.policy, probation_min_distinct_symbols=2)
    probation(h)
    cohorts(h, '2024-01-31', [10]*5, ('A','B'))
    assert h.risk_multiplier == .1
    h.evaluate('2024-03-01')
    assert h.risk_multiplier == .25
    assert h.probation_closed_cohorts == 0
    h.evaluate('2024-09-01')
    assert h.risk_multiplier == .25


@pytest.mark.parametrize('status,qty,epoch,reduce_only', [
    ('unknown', 10, True, True), ('cancel_pending', 10, True, True),
    ('accepted', 9, True, True), ('accepted', 10, False, True),
    ('accepted', 10, True, False)])
def test_unconfirmed_stop_never_releases_original_budget(status, qty, epoch, reduce_only):
    p,b,r,e = setup_budget()
    p.update_position('A',10,100,stop_price=90,time=e.timestamp)
    budget = r.drawdown_budget
    budget.policy = replace(budget.policy, stop_basis='confirmed_protective')
    ids = authoritative_position_ids(p,'A') if epoch else ('stale-position',)
    budget.confirmed_stop_provider = lambda: [ProtectiveOrder('stop','A','sell',qty,99,status,
                                                             reduce_only,ids)]
    assert budget.snapshot().open_risk == 100


def test_confirmed_stop_budget_preserves_original_lot_risk_and_pending_approval():
    p,b,r,e = setup_budget()
    p.update_position('A',10,100,stop_price=90,time=e.timestamp)
    budget = r.drawdown_budget
    budget.policy = replace(budget.policy, stop_basis='confirmed_protective')
    budget.confirmed_stop_provider = lambda: [ProtectiveOrder('stop','A','sell',10,99,'accepted',
        True,authoritative_position_ids(p,'A'))]
    assert budget.snapshot().open_risk == 10
    assert p.open_lots('A')[0].stop_price == 90
    assert budget.snapshot().stop_basis_by_symbol == {'A':'confirmed_protective'}


def test_reduction_ablation_keeps_admission_budget_and_hard_liquidation():
    p,b,r,e = setup_budget()
    p.update_position('A',20,100,stop_price=1,time=e.timestamp)
    budget = r.drawdown_budget
    budget.policy = replace(budget.policy, reduction_enabled=False)
    assert budget.snapshot().over_budget
    assert BacktestDrawdownReducer(budget).step(e) == []
    assert budget.clamp('A', 1, 100, 90) == 0
    assert r.portfolio_drawdown_liquidate == .2


def test_review_frequency_defers_only_reductions():
    p,b,r,e = setup_budget()
    budget = r.drawdown_budget
    budget.policy = replace(budget.policy, review_interval_bars=5)
    reducer = BacktestDrawdownReducer(budget)
    reducer.step(e)
    p.update_position('A',20,100,stop_price=1,time=e.timestamp)
    assert reducer.step(e) == []
    assert budget.audit[-1]['action'] == 'research_review_deferred'
    assert budget.snapshot().over_budget


def test_research_overlay_requires_registered_identity(monkeypatch):
    params = deepcopy(config._config)
    params['drawdown_budget']['stop_basis'] = 'confirmed_protective'
    monkeypatch.setattr(config, '_config', params)
    with pytest.raises(ValueError, match='registered research study'):
        build_risk_manager(config)


def test_old_position_pending_exit_cannot_be_reused_for_new_position():
    s,p,b,df = exit_case()
    first = submit(s,p,b,df,0)
    old_ids = s.get_context('A')['exit_position_ids']
    p.update_position('A',-10,100)
    p.update_position('A',3,100,stop_price=90,strategy_id=s.name)
    b._set_status(first,OrderStatus.UNKNOWN,df.index[1])
    assert submit(s,p,b,df,1) is None
    assert s.get_context('A')['exit_position_changed']
    assert s.get_context('A')['exit_position_ids'] == old_ids
    b.pending_orders.remove(first)
    b._set_status(first,OrderStatus.CANCELED,df.index[2])
    second = submit(s,p,b,df,2)
    assert second.qty == 3
    assert s.get_context('A')['exit_position_ids'] != old_ids


def test_registered_overlay_arms_preserve_all_hard_limits_and_input_config():
    from scripts.run_strategy_p0_p1 import ARMS, arm_parameters
    base = deepcopy(config._config)
    original = deepcopy(base)
    for arm in ARMS:
        params = arm_parameters(base,arm)
        for section in ('risk','drawdown','account','execution','portfolio_risk','state','stops'):
            assert params[section] == original[section]
        assert params['strategy_governance'] == original['strategy_governance']
    assert base == original


def test_health_proxy_links_only_reference_fills_to_observed_health_blocks(tmp_path):
    from analysis.overlay_study import health_reference_attribution
    control, reference = tmp_path/'control', tmp_path/'reference'
    control.mkdir(); reference.mkdir()
    pd.DataFrame([{'timestamp':'2024-01-01','symbol':'A','reason':'strategy_health_block'},
                  {'timestamp':'2024-01-01','symbol':'B','reason':'portfolio_block'}]).to_csv(control/'entry_observations.csv',index=False)
    pd.DataFrame(columns=['side','signal_time','symbol']).to_csv(control/'trades.csv',index=False)
    pd.DataFrame([{'timestamp':'2024-01-01','symbol':s,'reason':'order_accepted','order_id':s}
                  for s in ('A','B')]).to_csv(reference/'entry_observations.csv',index=False)
    pd.DataFrame([{'order_id':s,'symbol':s,'side':'buy','fill_time':'2024-01-02'}
                  for s in ('A','B')]).to_csv(reference/'trades.csv',index=False)
    pd.DataFrame([{'symbol':s,'entry_time':'2024-01-02','position_id':s,'net_pnl':pnl}
                  for s,pnl in (('A',10),('B',1000))]).to_csv(reference/'closed_trades.csv',index=False)
    result = health_reference_attribution(control,reference)
    assert result['reference_missed_gain_proxy'] == 10
    assert result['reference_avoided_loss_proxy'] == 0
    assert len(result['rows']) == 1
