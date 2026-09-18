"""Behavioral contracts for the approved recovery and drawdown remediation."""
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.drawdown_budget import BacktestDrawdownReducer
from backtest.reporting.operating_periods import requested_period_curve, split_execution_records
from config.config import config
from core.broker import Broker
from core.domain import OrderStatus
from core.risk.reservation import ensure_opening_reservation
from tests.test_p1_unified_risk_reservation import opening_intent
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.risk.circuit_breaker import BreakerAction
from core.strategy_health import HealthStatus, StrategyHealthMachine, StrategyHealthPolicy, classify_exit_controller
from core.state_store_v2 import StateStore
from tests.test_revalidation_execution import Harness, NOW, SYMBOL, broker


def machine():
    return StrategyHealthMachine('TrendBreakout', StrategyHealthPolicy.from_mapping(deepcopy(config.get('strategy_health'))))


def probation(m):
    m._enter_cooldown(pd.Timestamp('2024-01-01', tz='UTC').to_pydatetime(), reason='test_losses')
    m.evaluate('2024-01-31')
    assert m.risk_multiplier == .1


def cohorts(m, start, values, symbols=('A', 'B', 'C')):
    for i, value in enumerate(values):
        at = pd.Timestamp(start) + pd.Timedelta(days=i+1)
        m.ingest_close(close_event_id=f'{start}:{i}', symbol=symbols[i % len(symbols)],
            realized_pnl=value, initial_risk=10, timestamp=at, exit_reason='signal')
        m.evaluate(at)


def test_all_recoveries_advance_one_stage_with_fresh_evidence_and_clock():
    m = machine(); probation(m)
    start = pd.Timestamp('2024-01-31')
    for old, new in ((.1, .25), (.25, .5), (.5, 1.0)):
        cohorts(m, start, [10]*5)
        assert m.risk_multiplier == old
        at = start + pd.Timedelta(days=30)
        m.evaluate(at)
        assert m.risk_multiplier == new
        assert m.probation_closed_cohorts == 0
        restored = machine(); restored.load(m.to_dict()); m = restored
        assert m.risk_multiplier == new
        m.evaluate(at + pd.Timedelta(days=90))
        assert m.risk_multiplier == new  # time alone cannot advance
        start = at + pd.Timedelta(days=90)
        if new < 1:
            # No synthetic reset: the original stage clock remains earlier.
            m.probation_started_at = start.tz_localize('UTC').to_pydatetime()
    assert m.status is HealthStatus.ACTIVE


@pytest.mark.parametrize('values,symbols', [([-10,-5,-5,1,100], ('A','B','C')), ([10]*5, ('A',))])
def test_concentrated_or_single_symbol_evidence_cannot_restore_risk(values, symbols):
    m = machine(); probation(m); cohorts(m, '2024-01-31', values, symbols)
    m.evaluate('2024-06-01')
    assert m.status is HealthStatus.PROBATION and m.risk_multiplier == .1


def test_negative_full_sample_fails_without_waiting_30_days():
    m = machine(); probation(m); cohorts(m, '2024-01-31', [-1]*5)
    assert m.status is HealthStatus.COOLDOWN and m.recovery_stage == 0
    assert m.cooldown_until == pd.Timestamp('2024-03-06', tz='UTC')


def test_old_probation_migration_does_not_increase_risk_or_reuse_evidence():
    old = StrategyHealthMachine('TrendBreakout', StrategyHealthPolicy())
    old._transition(HealthStatus.PROBATION, pd.Timestamp('2024-01-01', tz='UTC').to_pydatetime(), reason='legacy')
    old.ingest_close(close_event_id='old', symbol='A', realized_pnl=100, initial_risk=10, timestamp='2024-01-02')
    restored = machine(); restored.load(old.to_dict())
    assert restored.risk_multiplier == .25
    assert restored.probation_closed_cohorts == 0
    restored.evaluate('2024-07-01')
    assert restored.probation_started_at == pd.Timestamp('2024-07-01', tz='UTC')
    old.manual_lock('operator', at='2024-01-03')
    restored.load(old.to_dict())
    assert not restored.allows_new_entries('2030-01-01')


def setup_budget(*, volume=10000, symbol='A', cost_rate=0):
    p = Portfolio(10000)
    b = Broker(p, commission_rate=cost_rate, slippage=0, max_participation_rate=.05)
    r = RiskManager(risk_per_trade=.02, drawdown_budget_policy={'enabled': True},
        minimum_entry_policy={'scale_with_risk': True, 'research_notional_floor': 10},
        execution_costs={'commission_rate_taker': cost_rate})
    r.high_water_equity = 10000
    r.drawdown_budget.bind(b, {})
    event = SimpleNamespace(timestamp=pd.Timestamp('2024-01-02'), bars={symbol: pd.Series(
        dict(open=100., high=100., low=100., close=100., volume=volume), name=pd.Timestamp('2024-01-02'))})
    r.drawdown_budget.update({symbol:100.}, event.bars, event.timestamp)
    return p, b, r, event


def test_scaled_minimum_accepts_50_but_never_raises_size_to_floor():
    p,b,r,e = setup_budget()
    r.portfolio_breaker_action = BreakerAction.REDUCE
    r.probation_equity = 10000
    # The configured account probation multiplier is .25.
    qty = r.calculate_position_size(10000, 100, 90) * .1
    assert qty == .5
    assert r.clamp_entry_qty(p,'A',qty,100,current_prices={'A':100},health_multiplier=.1) == .5
    assert r.clamp_entry_qty(p,'A',.05,100,current_prices={'A':100},health_multiplier=.1) == 0


def test_open_risk_counts_unrealized_profit_to_original_stop():
    p,b,r,e = setup_budget()
    p.update_position('A',10,100,stop_price=90,time=e.timestamp)
    r.drawdown_budget.update({'A':150}, e.bars, e.timestamp)
    snap = r.drawdown_budget.snapshot()
    assert snap.equity == 10500
    assert snap.open_risk == 600  # not the original 100 risk
    assert snap.budget == 1250


def test_partial_fill_transfers_reserved_risk_and_cancel_releases_only_remainder():
    p,b,r,e = setup_budget(volume=100)
    order = b.submit_order('A','buy',50,100,stop_loss=90,approved_risk_amount=500,timestamp=pd.Timestamp('2024-01-01'))
    assert order.accepted
    assert r.drawdown_budget.snapshot().pending_risk == 500
    b.process_orders(e.bars)
    snap = r.drawdown_budget.snapshot()
    assert order.filled_qty == 5
    assert snap.open_risk == 50 and snap.pending_risk == 450
    b.cancel_opening_orders(timestamp=e.timestamp)
    assert r.drawdown_budget.snapshot().pending_risk == 0
    assert r.drawdown_budget.snapshot().open_risk == 50
    b.reservation_projection.rebuild(b.event_pipeline.events)
    assert r.drawdown_budget.snapshot().pending_risk == 0


def test_final_approval_counts_other_pending_orders_and_preserves_identity():
    p,b,r,e = setup_budget()
    r.drawdown_budget.bars['B'] = e.bars['A']
    r.drawdown_budget.prices['B'] = 100
    a = b.submit_order('A','buy',90,100,stop_loss=90,approved_risk_amount=900,timestamp='2024-01-01')
    rejected = b.submit_order('B','buy',20,100,stop_loss=90,approved_risk_amount=200,timestamp='2024-01-01')
    assert a.accepted and rejected.status is OrderStatus.REJECTED
    assert r.drawdown_budget.snapshot().pending_risk == 900
    assert b.submit_intent(a.intent) is a


def test_preapproved_command_does_not_compete_with_its_own_reservation():
    from dataclasses import replace
    p,b,r,e = setup_budget(symbol='BTC/USDT')
    intent = replace(opening_intent(qty=90), initial_stop=90, approved_risk_amount=900)
    enriched,_ = ensure_opening_reservation(b.event_pipeline,intent,reference_price=100,
        occurred_at=e.timestamp.tz_localize('UTC'),source='test')
    assert r.drawdown_budget.snapshot().pending_risk == 900
    assert r.drawdown_budget.check_intent(enriched) is None
    assert b.submit_intent(enriched).accepted
    assert r.drawdown_budget.snapshot().pending_risk == 900


def test_missing_original_stop_or_current_mark_blocks_new_risk():
    p,b,r,e = setup_budget()
    p.update_position('A',1,100,time=e.timestamp)
    assert r.drawdown_budget.snapshot().issues
    assert r.drawdown_budget.clamp('A',1,100,90) == 0
    p.lot_books['A'].open_lots[0].stop_price = 90
    r.drawdown_budget.update({'A':100}, {}, e.timestamp)
    assert r.drawdown_budget.snapshot().issues


def test_reducer_reserves_cost_feedback_and_uses_real_fills():
    p,b,r,e = setup_budget(cost_rate=.001)
    p.update_position('A',20,100,stop_price=90,time=e.timestamp)
    r.high_water_equity = 12200
    before = r.drawdown_budget.snapshot()
    reducer = BacktestDrawdownReducer(r.drawdown_budget)
    fills = reducer.step(e)
    after = r.drawdown_budget.snapshot()
    assert fills and all(t['exit_reason']=='DrawdownBudgetReduce' for t in fills)
    assert after.open_risk <= after.budget + 1e-6
    assert p.get_position('A')['qty'] < 20
    assert not reducer.step(e)
    assert classify_exit_controller('DrawdownBudgetReduce') == 'account_risk'
    assert before.exit_cost > 0


def test_partial_reduction_is_not_resubmitted_or_released_early():
    p,b,r,e = setup_budget(volume=10)
    p.update_position('A',20,100,stop_price=90,time=e.timestamp)
    r.high_water_equity = 12200
    reducer = BacktestDrawdownReducer(r.drawdown_budget)
    reducer.step(e)
    ids = [o.id for o in b.active_orders+b.pending_orders if o.exit_reason=='DrawdownBudgetReduce']
    assert ids and r.drawdown_budget.snapshot().over_budget
    assert not reducer.step(e)
    assert ids == [o.id for o in b.active_orders+b.pending_orders if o.exit_reason=='DrawdownBudgetReduce']


def live_budget_engine(broker, path):
    h = Harness(broker)
    h.state_store = StateStore(str(path))
    h.risk_manager = RiskManager(drawdown_budget_policy={'enabled':True})
    h.risk_manager.high_water_equity = 12400
    h.risk_manager.drawdown_budget.bind(broker,h.strategies)
    h.risk_manager.drawdown_budget.update({SYMBOL:100},{SYMBOL:{'close':100}},NOW)
    return h


def test_live_cancel_timeout_keeps_pending_risk_and_never_sends_reduction(broker,tmp_path):
    broker.exchange.entry_fraction = .5
    order = broker.submit_order(SYMBOL,'buy',10,reference_price=100,stop_loss=90,
        approved_risk_amount=100,strategy_id='TrendBreakout')
    h = live_budget_engine(broker,tmp_path/'budget.db')
    broker.exchange.cancel_timeout = True
    requests = len(broker.exchange.requests)
    assert not h._reconcile_drawdown_budget()
    assert len(broker.exchange.requests) == requests
    assert h.risk_manager.drawdown_budget.snapshot().pending_risk > 0
    h.state_store.close()


def test_live_partial_reduction_checkpoint_survives_restart_without_duplicates(broker,tmp_path):
    broker.submit_order(SYMBOL,'buy',10,reference_price=100,stop_loss=90,
        approved_risk_amount=100,strategy_id='TrendBreakout')
    broker.exchange.exit_fraction = .5
    path = tmp_path/'budget.db'
    h = live_budget_engine(broker,path)
    assert not h._reconcile_drawdown_budget()
    checkpoint = h.state_store.get('drawdown_budget_action')
    assert checkpoint and h.risk_manager.drawdown_budget.snapshot().open_risk > 0
    h.state_store.close()
    restarted = live_budget_engine(broker,path)
    requests = len(broker.exchange.requests)
    assert not restarted._reconcile_drawdown_budget()
    assert len(broker.exchange.requests) == requests
    assert restarted.state_store.get('drawdown_budget_action')['action_id']==checkpoint['action_id']
    pending = broker.order_store.list_non_terminal()[0]
    payload = broker.exchange.orders[pending['exchange_order_id']]
    remaining = payload['remaining']
    payload.update(status='closed',filled=payload['amount'],remaining=0)
    payload['trades'].append({'id':'budget-final-fill','amount':remaining,'price':100,
        'datetime':NOW.isoformat(),'fee':{'cost':0,'currency':'USDT'}})
    broker.exchange.qty -= remaining; broker.exchange.cash += remaining*100
    assert not restarted._reconcile_drawdown_budget()
    assert restarted.state_store.get('drawdown_budget_action') is None
    assert len(broker.exchange.requests) == requests
    restarted._reconcile_protective_orders()
    assert broker.exchange.requests[-1]['params']['stopLossPrice'] == 90
    assert broker.exchange.requests[-1]['amount'] == pytest.approx(4)
    restarted.state_store.close()


def test_calendar_outage_and_tail_are_not_fresh_market_observations():
    source = pd.DataFrame({'equity':[10000.,10100.]}, index=pd.to_datetime(['2024-01-01','2024-01-09']))
    frame = requested_period_curve(source,'2023-12-31','2024-01-10',capital=10000,
        lifecycle={'termination_timestamp':'2024-01-09'},activity=[{'timestamp':'2024-01-01',
        'strategy_states':{'TrendBreakout':'cooldown'},'all_routed_strategies_blocked':True,'account_action':'reduce'}])
    missing = frame.loc['2024-01-02':'2024-01-08']
    assert len(missing)==7 and missing.valuation_stale.all() and not missing.market_observed.any()
    assert set(missing.phase)=={'market_data_missing'}
    assert frame.loc['2024-01-01','phase']=='strategy_health_pause'
    assert frame.loc['2024-01-10','phase']=='risk_halted_cash'
    assert frame.loc['2023-12-31','phase']=='pre_market_cash'


def test_valuation_transfers_separate_from_actual_forced_exit():
    rows = [{'exit_reason':'signal'},{'exit_reason':'EndOfBacktest'}]
    actual, marks = split_execution_records(rows,mark_to_market=True)
    assert len(actual)==len(marks)==1
    assert split_execution_records(rows,mark_to_market=False)==(rows,[])
