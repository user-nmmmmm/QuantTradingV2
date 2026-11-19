"""Real Strategy -> selector -> allocator -> Broker hooks on synthetic intake."""
from copy import deepcopy
from pathlib import Path
import json
from types import SimpleNamespace
import sqlite3

import numpy as np
import pandas as pd
import pytest

from core.planning.allocation import EntryCandidate, PortfolioSignalAllocator
from core.broker import Broker
from core.events import TradingEventPipeline
from core.trading.portfolio import Portfolio
from core.risk import RiskManager
from core.foundation.runtime import EventProcessor, MarketDataSlice
from core.trading.state import MarketState
from research.ml_selection import forward_account_trajectory as account
from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.forward_execution_binding import verify_forward_execution_binding
from research.ml_selection.forward_policy_capture import (
    ForwardPolicyCapture, REGISTRY, binding_from_capture, read_capture_journal,
)
from research.ml_selection.models import FeatureScaler, RidgeModel
from research.ml_selection.selector import ResearchSelector
from strategies.base import Strategy
from tests.test_forward_account_trajectory import signed
from tests.test_ml_selection_next_forward import public_receipt


class CaptureStrategy(Strategy):
    def __init__(self):
        super().__init__('CapturedNative', {MarketState.TREND_UP})
        self.params = {'fixture': 'fixed-native-risk-sizing'}

    def should_enter(self, *args):
        return {'action': 'buy', 'price': 100., 'score': 1.}

    def should_exit(self, *args):
        return None


class CaptureStateMachine:
    def get_state(self, *args):
        return MarketState.TREND_UP


class CaptureRouter:
    def __init__(self, strategy):
        self.strategies = {strategy.name: strategy}

    def collect_candidate(self, symbol, i, frame, state, portfolio, broker, *args):
        strategy = next(iter(self.strategies.values()))
        if portfolio.get_position(symbol)['qty'] or broker.has_active_open_order(symbol):
            return None
        return EntryCandidate(symbol, strategy, i, frame, state,
                              strategy.should_enter(symbol, i, frame, state, portfolio), 1.)


class Execution:
    def __init__(self, broker):
        self.broker = broker

    def __getattr__(self, name):
        return getattr(self.broker, name)

    def on_market_data(self, event):
        self.broker.process_orders(event.bars)


def capture_fixture(tmp_path, *, rejected=False, partial=False, feature_delta=0., omit_features=False, original_files=False):
    result = public_receipt(tmp_path/'intake')
    collection = Path(result['folder'])
    manifest = result['manifest']
    frame = pd.read_csv(collection/'BTC_USDT.csv', index_col='timestamp', parse_dates=True).tail(4)
    frame.index = pd.to_datetime(frame.index, utc=True)
    tick = [frame.index[0]+pd.Timedelta(hours=12)]
    broker = Broker(Portfolio(10000.), commission_rate=.001, commission_rate_maker=.001,
        slippage=0, max_participation_rate=.000005 if partial else 1., account_id='captured-session',
        event_pipeline=TradingEventPipeline(run_id='captured-run', clock=lambda: tick[0].to_pydatetime()))
    scaler = FeatureScaler.fit(pd.DataFrame(0., index=[0, 1], columns=FEATURE_COLUMNS), FEATURE_COLUMNS)
    model = RidgeModel(scaler, np.zeros(len(FEATURE_COLUMNS)), -1. if rejected else 1., {})
    dataset = pd.DataFrame([{**dict.fromkeys(FEATURE_COLUMNS, 0.), 'symbol': 'BTC/USDT',
        'as_of': stamp+pd.Timedelta(days=1), 'eligible': True, 'exclusion_reason': ''} for stamp in frame.index])
    selector = ResearchSelector(dataset, mode='model', model=model)
    strategy = CaptureStrategy()
    processor = EventProcessor(portfolio=broker.portfolio, execution=Execution(broker),
        risk_manager=RiskManager(), state_machine=CaptureStateMachine(),
        router=CaptureRouter(strategy), allocator=PortfolioSignalAllocator(), candidate_selector=selector)
    observations, evidence, runtime_events = {}, {}, []
    for stamp, bar in frame.iterrows():
        observation = signed({'observed_at': stamp.isoformat(), 'information_cutoff': stamp.isoformat(),
            'protocol_id': 'protocol', 'frozen_candidate_id': 'candidate', 'model_id': model.model_id,
            'source_kind': 'synthetic_fixture', 'retrospective_observation': True,
            'observations': [{'symbol': 'BTC/USDT', 'eligible': True,
                **({} if omit_features else {'as_of': (stamp+pd.Timedelta(days=1)).isoformat(),
                    'features': {name: feature_delta if name == FEATURE_COLUMNS[0] else 0. for name in FEATURE_COLUMNS}})}]}, 'observation_id')
        bars = {'BTC/USDT': {key: float(bar[key]) for key in ('open', 'high', 'low', 'close', 'volume')}}
        raw = {'received_at': manifest['market_files']['BTC/USDT']['received_at'], 'bars': bars}
        market = {'event_id': stamp.isoformat(), 'timestamp': stamp.isoformat(),
            'received_at': raw['received_at'], 'bars': bars, 'raw_receipt': raw, 'raw_receipt_sha256': account._digest(raw)}
        evidence[stamp.isoformat()] = {'observation': observation, 'market_event': market,
            'market_collections': [{'path': collection.relative_to(tmp_path).as_posix(), 'collection_id': manifest['collection_id']}]}
        observations[observation['observation_id']] = observation
        runtime_events.append(MarketDataSlice(stamp, {'BTC/USDT': bar.copy()}, {'BTC/USDT': frame.loc[:stamp]},
                                              timeframe='1d', source='synthetic_fixture'))
    if original_files:
        (tmp_path/"protocol.json").write_text(json.dumps({"protocol_id": "protocol"}), encoding="utf-8")
        (tmp_path/"candidate.json").write_text(json.dumps({"model_id": model.model_id}), encoding="utf-8")
    wrapped = ForwardPolicyCapture(tmp_path, enabled=True).wrap(processor, broker=broker,
        protocol_id='protocol', frozen_candidate_id='candidate', model_id=model.model_id,
        frozen_protocol={'protocol_id': 'protocol'}, frozen_candidate={'model_id': model.model_id},
        evidence_resolver=lambda event: deepcopy(evidence[event.timestamp.isoformat()]))
    return SimpleNamespace(wrapped=wrapped, broker=broker, events=runtime_events,
        observations=observations, evidence=evidence, tick=tick, selector=selector, root=tmp_path)


def run_fixture(fixture):
    for event in fixture.events:
        fixture.tick[0] = event.timestamp + pd.Timedelta(hours=12)
        fixture.wrapped.process(event)
    return fixture.wrapped.export_binding()


def account_from_binding(fixture, binding):
    return account.build_forward_account_receipt(protocol_id='protocol', frozen_candidate_id='candidate',
        session_id=fixture.broker.account_id, session_started_at=(fixture.events[0].timestamp-pd.Timedelta(days=1)).isoformat(),
        observations=list(fixture.observations.values()), market_events=binding['market_events'],
        decisions=binding['decisions'], intents=binding['intents'], execution_binding_id=binding['binding_id'],
        **fixture.wrapped.registration['account_config'])


def proof(fixture, binding, receipt):
    return account.verify_forward_account_receipt(receipt, protocol_id='protocol', frozen_candidate_id='candidate',
        qualifying_observation_ids=fixture.observations, observation_receipts=fixture.observations,
        execution_bindings={binding['binding_id']: binding}, binding_store_root=fixture.root)


def test_default_off_is_inert_and_does_not_create_capture_files(tmp_path):
    processor = object()
    assert ForwardPolicyCapture(tmp_path/'absent').wrap(processor, broker=None, protocol_id=None,
        frozen_candidate_id=None, model_id=None, frozen_protocol=None, frozen_candidate=None,
        evidence_resolver=None) is processor
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('partial', [False, True])
def test_actual_native_strategy_selector_and_broker_capture_proves_local_origin_without_upgrading_synthetic(tmp_path, partial):
    fixture = capture_fixture(tmp_path, partial=partial)
    binding = run_fixture(fixture)
    assert binding['schema'].endswith('/v2')
    assert binding['intents'][0]['qty'] == pytest.approx(10.)
    command = next(iter(binding['native_commands'].values()))
    assert command['signal_id'] == command['causation_id'] == binding['decisions'][0]['decision_id']
    assert command['signal_id'] != binding['decisions'][0]['observation_id']
    receipt = account_from_binding(fixture, binding)
    result = proof(fixture, binding, receipt)
    capture = result['original_execution_binding']['policy_capture']
    assert capture['original_policy_command_origin_linked']
    assert not capture['contemporaneous_input_eligible']
    assert result['portfolio_return'] is None and result['engineering'] == 'pass'
    assert 'captured_policy_uses_synthetic_or_retrospective_inputs' in result['pending_reasons']
    assert len(receipt.payload['fills']) == (3 if partial else 1)
    assert receipt.payload['account_path'][-1]['cash'] == pytest.approx(fixture.broker.portfolio.cash)
    fixture.wrapped.close()


def test_no_trade_policy_calls_are_captured_but_do_not_prove_portfolio_performance(tmp_path):
    fixture = capture_fixture(tmp_path, rejected=True)
    binding = run_fixture(fixture)
    records = read_capture_journal(fixture.wrapped.folder)
    assert any(row['kind'] == 'selection' and not row['payload']['selected'] for row in records)
    assert binding['intents'] == []
    result = proof(fixture, binding, account_from_binding(fixture, binding))
    assert not result['original_execution_binding']['original_policy_command_origin_linked']
    assert result['portfolio_return'] is None
    fixture.wrapped.close()


@pytest.mark.parametrize('field,value', [('qty', 5.), ('side', 'sell')])
def test_resigned_binding_cannot_change_actual_captured_command(field, value, tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    changed = deepcopy(binding)
    changed['intents'][0][field] = value
    native_key = 'requested_qty' if field == 'qty' else 'action'
    changed['native_commands'][changed['intents'][0]['intent_id']][native_key] = value
    changed = signed(changed, 'binding_id')
    receipt = account_from_binding(fixture, binding).to_dict()
    receipt['intents'] = changed['intents']
    receipt['execution_binding_id'] = changed['binding_id']
    with pytest.raises(ValueError, match='append-only policy capture|canonical command'):
        verify_forward_execution_binding(changed, account=receipt,
            observation_receipts=fixture.observations, store_root=tmp_path, as_of=pd.Timestamp.now(tz='UTC'))
    fixture.wrapped.close()


def test_duplicate_runtime_step_is_rejected_before_resubmission(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.process(fixture.events[0])
    before = len(fixture.broker.orders_by_id)
    with pytest.raises(ValueError, match='already processed'):
        fixture.wrapped.process(fixture.events[0])
    assert len(fixture.broker.orders_by_id) == before
    fixture.wrapped.close()


def test_sqlite_registry_and_journal_are_append_only_and_checkpoint_is_independent(tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    for path, table in ((tmp_path/REGISTRY, 'sessions'), (fixture.wrapped.folder/'journal.sqlite3', 'records')):
        connection = sqlite3.connect(path)
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            connection.execute(f'DELETE FROM {table}')
        connection.close()
    changed = deepcopy(binding['policy_capture']['checkpoint'])
    changed['record_hash'] = 'a'*64
    with pytest.raises(ValueError, match='not retained'):
        binding_from_capture(tmp_path, fixture.wrapped.registration['registration_id'], changed)
    fixture.wrapped.close()


def test_missing_registry_or_self_signed_new_journal_cannot_upgrade_local_metadata(tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    fixture.wrapped.close()
    (tmp_path/REGISTRY).rename(tmp_path/'original-registry.sqlite3')
    receipt = account_from_binding(fixture, binding)
    with pytest.raises((sqlite3.OperationalError, ValueError)):
        proof(fixture, binding, receipt)


def test_original_native_account_must_equal_replay_cash_fees_inventory_and_marks(tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    receipt = account_from_binding(fixture, binding).to_dict()
    receipt['account_path'][-1]['cash'] += 1.
    with pytest.raises(ValueError, match='economics/clocks differ'):
        verify_forward_execution_binding(binding, account=receipt, observation_receipts=fixture.observations,
            store_root=tmp_path, as_of=pd.Timestamp.now(tz='UTC'))
    fixture.wrapped.close()


def test_model_mutation_and_wrong_observation_bytes_fail_before_policy_or_order(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.selector.model.intercept = 99.
    with pytest.raises(ValueError, match='frozen capture source/model'):
        fixture.wrapped.process(fixture.events[0])
    assert not fixture.broker.orders_by_id
    fixture.wrapped.close()


def test_runtime_failure_records_interruption_and_does_not_retry_orders(tmp_path):
    fixture = capture_fixture(tmp_path)
    def fail_after_native_order(event):
        if event.event_type == 'order':
            raise RuntimeError('interrupted after order')
    fixture.broker.event_pipeline.subscribe(fail_after_native_order)
    with pytest.raises(RuntimeError, match='interrupted'):
        fixture.wrapped.process(fixture.events[0])
    count = len(fixture.broker.orders_by_id)
    with pytest.raises(ValueError, match='reconciliation'):
        fixture.wrapped.process(fixture.events[1])
    assert len(fixture.broker.orders_by_id) == count
    fixture.wrapped.close()


def test_input_market_and_original_observation_must_match_actual_hook(tmp_path):
    fixture = capture_fixture(tmp_path)
    evidence = fixture.evidence[fixture.events[0].timestamp.isoformat()]
    evidence['market_event']['bars']['BTC/USDT']['close'] = 102.
    with pytest.raises(ValueError, match='runtime event differs'):
        fixture.wrapped.process(fixture.events[0])
    assert not fixture.broker.orders_by_id
    fixture.wrapped.close()


def test_nonflat_initial_inventory_is_rejected_before_registering_session(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.close()
    fixture.broker.portfolio.positions['BTC/USDT'] = {'qty': 1., 'avg_price': 100.}
    with pytest.raises(ValueError, match='flat initial inventory'):
        ForwardPolicyCapture(tmp_path/'other', enabled=True).wrap(fixture.wrapped.processor,
            broker=fixture.broker, protocol_id='p', frozen_candidate_id='c', model_id=fixture.selector.model.model_id,
            frozen_protocol={'protocol_id': 'p'}, frozen_candidate={'model_id': fixture.selector.model.model_id},
            evidence_resolver=lambda event: None)


@pytest.mark.parametrize('operation', ['rewrite', 'truncate'])
def test_journal_tamper_or_truncation_cannot_be_repaired_by_resigning_binding(operation, tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    receipt = account_from_binding(fixture, binding)
    fixture.wrapped.close()
    connection = sqlite3.connect(fixture.wrapped.folder/'journal.sqlite3')
    if operation == 'rewrite':
        connection.execute('DROP TRIGGER records_no_update')
        connection.execute("UPDATE records SET payload_json='{}' WHERE sequence=2")
    else:
        connection.execute('DROP TRIGGER records_no_delete')
        connection.execute('DELETE FROM records WHERE sequence=(SELECT MAX(sequence) FROM records)')
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match='journal changed|checkpoint mismatch|account origin incomplete'):
        proof(fixture, binding, receipt)


def test_original_broker_activity_outside_process_blocks_later_binding_export(tmp_path):
    fixture = capture_fixture(tmp_path)
    run_fixture(fixture)
    fixture.broker.submit_order('BTC/USDT', 'buy', 1., price=100., timestamp=fixture.events[-1].timestamp,
                                strategy_id='uncaptured_manual', sequence=99)
    with pytest.raises(ValueError, match='outside captured runtime'):
        fixture.wrapped.export_binding()
    fixture.wrapped.close()


def test_same_session_cannot_restart_with_new_registration_or_resubmit_original_orders(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.close()
    with pytest.raises(ValueError, match='already registered'):
        ForwardPolicyCapture(tmp_path, enabled=True).wrap(fixture.wrapped.processor,
            broker=fixture.broker, protocol_id='protocol', frozen_candidate_id='candidate', model_id=fixture.selector.model.model_id,
            frozen_protocol={'protocol_id': 'protocol'}, frozen_candidate={'model_id': fixture.selector.model.model_id},
            evidence_resolver=lambda event: fixture.evidence[event.timestamp.isoformat()])
    assert not fixture.broker.orders_by_id


def test_runtime_policy_configuration_and_method_replacement_are_frozen(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.processor.risk_manager.risk_per_trade = .5
    with pytest.raises(ValueError, match='frozen capture source/model'):
        fixture.wrapped.process(fixture.events[0])
    fixture.wrapped.processor.risk_manager.risk_per_trade = .01
    fixture.selector.select = lambda *args, **kwargs: []
    with pytest.raises(ValueError, match='original ResearchSelector'):
        fixture.wrapped.process(fixture.events[0])
    assert not fixture.broker.orders_by_id
    fixture.wrapped.close()


def test_synthetic_registration_cannot_be_upgraded_by_changing_replay_provenance_labels(tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    receipt = account_from_binding(fixture, binding).to_dict()
    receipt['input_source_kind'] = 'recorded_forward'
    receipt = signed(receipt, 'receipt_id')
    result = proof(fixture, binding, receipt)
    assert result['portfolio_return'] is None
    assert 'captured_policy_uses_synthetic_or_retrospective_inputs' in result['pending_reasons']
    fixture.wrapped.close()


def test_input_available_only_after_actual_capture_clock_is_rejected_without_order(tmp_path):
    fixture = capture_fixture(tmp_path)
    key = fixture.events[0].timestamp.isoformat()
    changed = fixture.evidence[key]['observation']
    changed['observed_at'] = (pd.Timestamp.now(tz='UTC')+pd.Timedelta(days=1)).isoformat()
    fixture.evidence[key]['observation'] = signed(changed, 'observation_id')
    with pytest.raises(ValueError, match='unavailable at actual runtime clock'):
        fixture.wrapped.process(fixture.events[0])
    assert not fixture.broker.orders_by_id
    fixture.wrapped.close()


def test_v1_caller_declared_native_causal_metadata_cannot_prove_original_policy(tmp_path):
    from tests.test_forward_execution_binding import bound_fixture, verify
    inputs, _, binding, observations, _ = bound_fixture(tmp_path)
    identifier = binding['intents'][0]['intent_id']
    binding['native_commands'][identifier]['signal_id'] = binding['decisions'][0]['decision_id']
    binding['native_commands'][identifier]['causation_id'] = binding['decisions'][0]['observation_id']
    binding = signed(binding, 'binding_id')
    inputs['execution_binding_id'] = binding['binding_id']
    result = verify(account.build_forward_account_receipt(**inputs), binding, observations, tmp_path)
    assert not result['original_execution_binding']['original_policy_command_origin_linked']
    assert result['portfolio_return'] is None


def test_binding_export_is_automatically_persisted_for_existing_forward_store_loader(tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    path = tmp_path/'execution_bindings'/(binding['binding_id']+'.json')
    assert path.is_file()
    assert binding == json.loads(path.read_text(encoding='utf-8'))
    assert fixture.wrapped.export_binding() == binding
    records = read_capture_journal(fixture.wrapped.folder)
    selection = next(row['payload'] for row in records if row['kind'] == 'selection')
    assert selection['actual_feature_rows'][0]['symbol'] == 'BTC/USDT'
    fixture.wrapped.close()

@pytest.mark.parametrize('omit_features', [True, False])
def test_actual_policy_features_must_join_original_observation_or_remain_pending(tmp_path, omit_features):
    fixture = capture_fixture(tmp_path, omit_features=omit_features, feature_delta=0. if omit_features else 5.)
    binding = run_fixture(fixture)
    receipt = account_from_binding(fixture, binding)
    if omit_features:
        result = proof(fixture, binding, receipt)
        assert not result['original_execution_binding']['policy_capture']['actual_policy_inputs_joined']
        assert result['portfolio_return'] is None
        assert 'captured_policy_feature_inputs_not_joined_to_original_observation' in result['pending_reasons']
    else:
        with pytest.raises(ValueError, match='feature inputs differ'):
            proof(fixture, binding, receipt)
    fixture.wrapped.close()


def test_original_frozen_store_protocol_and_candidate_bytes_cannot_change_after_registration(tmp_path):
    fixture = capture_fixture(tmp_path, original_files=True)
    binding = run_fixture(fixture)
    receipt = account_from_binding(fixture, binding)
    path = tmp_path/'candidate.json'
    path.write_text(path.read_text(encoding='utf-8')+' ', encoding='utf-8')
    with pytest.raises(ValueError, match='protocol/candidate bytes changed'):
        proof(fixture, binding, receipt)
    fixture.wrapped.close()


def test_recorded_forward_capture_requires_original_frozen_store_files(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.close()
    with pytest.raises(ValueError, match='file missing from original forward store'):
        ForwardPolicyCapture(tmp_path/'without-original', enabled=True).wrap(fixture.wrapped.processor,
            broker=fixture.broker, protocol_id='protocol', frozen_candidate_id='candidate', model_id=fixture.selector.model.model_id,
            frozen_protocol={'protocol_id': 'protocol'}, frozen_candidate={'model_id': fixture.selector.model.model_id},
            evidence_resolver=lambda event: fixture.evidence[event.timestamp.isoformat()], input_source_kind='recorded_forward')


def test_failed_tail_cannot_be_hidden_by_exporting_last_successful_checkpoint(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.process(fixture.events[0])
    original_checkpoint = deepcopy(fixture.wrapped.checkpoint)
    def fail_after_account_fill(event):
        if event.event_type == 'fill':
            raise RuntimeError('interrupted after fill')
    fixture.broker.event_pipeline.subscribe(fail_after_account_fill)
    fixture.tick[0] = fixture.events[1].timestamp+pd.Timedelta(hours=12)
    with pytest.raises(RuntimeError, match='interrupted after fill'):
        fixture.wrapped.process(fixture.events[1])
    with pytest.raises(ValueError, match='interrupted or unknown'):
        binding_from_capture(tmp_path, fixture.wrapped.registration['registration_id'], original_checkpoint)
    fixture.wrapped.close()


def test_public_store_bytes_cannot_create_a_new_valid_capture_signature(tmp_path):
    fixture = capture_fixture(tmp_path)
    binding = run_fixture(fixture)
    receipt = account_from_binding(fixture, binding)
    records = read_capture_journal(fixture.wrapped.folder)
    fixture.wrapped.close()
    assert fixture.wrapped._signing_key is None
    last = records[-1]
    forged = {'sequence': last['sequence']+1, 'kind': 'input',
        'recorded_at': pd.Timestamp.now(tz='UTC').isoformat(), 'previous_hash': last['record_hash'],
        'payload': deepcopy(fixture.evidence[fixture.events[0].timestamp.isoformat()])}
    fingerprint = account._digest(forged)
    connection = sqlite3.connect(fixture.wrapped.folder/'journal.sqlite3')
    connection.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?)', (forged['sequence'], forged['kind'],
        forged['recorded_at'], forged['previous_hash'], json.dumps(forged['payload']), fingerprint, '0'*128))
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match='signature invalid'):
        proof(fixture, binding, receipt)



@pytest.mark.parametrize("mode", ["spot_margin", "perpetual"])
def test_flat_margin_accounts_cannot_masquerade_as_supported_original_spot_capture(tmp_path, mode):
    from core.trading.accounts import AccountMode
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.close()
    fixture.broker.portfolio.account_mode = AccountMode(mode)
    with pytest.raises(ValueError, match="margin/funding origin remains pending"):
        ForwardPolicyCapture(tmp_path/"margin", enabled=True).wrap(fixture.wrapped.processor,
            broker=fixture.broker, protocol_id="protocol", frozen_candidate_id="candidate", model_id=fixture.selector.model.model_id,
            frozen_protocol={"protocol_id": "protocol"}, frozen_candidate={"model_id": fixture.selector.model.model_id},
            evidence_resolver=lambda event: fixture.evidence[event.timestamp.isoformat()])

class NativeEngineCaptureStrategy(CaptureStrategy):
    def __init__(self):
        super().__init__()
        self.allowed_states = set(MarketState)

    def should_enter(self, *args):
        return {'action': 'buy', 'price': 100., 'score': 1., 'stop_loss': 95.}


def _capture_engine_settings(config, *, reject):
    settings = deepcopy(config._config)
    settings['account']['mode'] = 'spot'
    settings['execution']['fee_schedule']['market_type'] = 'spot'
    settings['execution'].update(commission_rate_taker=.001, commission_rate_maker=.001,
        use_impact_cost=False, spread_bps=0., volatility_slippage_factor=0., max_participation_rate=.000005)
    settings['routing'] = {state.name: 'CapturedNative' for state in MarketState}
    settings['protective_orders']['backtest_resident'] = False
    settings['allocation']['capital']['enabled'] = False
    if reject:
        settings['risk']['minimum_entry']['research_notional_floor'] = 100000.
    return settings


def _engine_capture_observer(fixture, root, *, enabled):
    def resolve(event):
        stamp = pd.Timestamp(event.timestamp)
        stamp = stamp.tz_localize('UTC') if stamp.tzinfo is None else stamp.tz_convert('UTC')
        evidence = deepcopy(fixture.evidence[stamp.isoformat()])
        for reference in evidence['market_collections']:
            reference['path'] = 'inputs/' + reference['path']
        return evidence
    return ForwardPolicyCapture(root, enabled=enabled).engine_observer(protocol_id='protocol',
        frozen_candidate_id='candidate', model_id=fixture.selector.model.model_id,
        frozen_protocol={'protocol_id': 'protocol'}, frozen_candidate={'model_id': fixture.selector.model.model_id},
        evidence_resolver=resolve)


def _native_engine_run(fixture, observer, *, terminal_policy="valuation_only"):
    from backtest.engine import BacktestEngine
    from core.events import EventCodec
    selector = ResearchSelector(fixture.selector.table.reset_index(), mode='model', model=fixture.selector.model)
    engine = BacktestEngine(initial_capital=10000., warmup_period=0, timeframe='1d',
        slippage=0., calculate_benchmarks=False, candidate_selector=selector,
        run_id='same-native-engine-capture', terminal_policy=terminal_policy, bar_capture=observer)
    frame = fixture.events[-1].histories['BTC/USDT'].copy()
    result = engine.run({'BTC/USDT': frame}, strategies={'CapturedNative': NativeEngineCaptureStrategy()},
                        routing_log_enabled=False)
    events = [EventCodec.encode(row) for row in engine.execution_adapter.broker.event_pipeline.events]
    return result, events, engine


@pytest.mark.parametrize('reject', [False, True])
def test_actual_engine_router_strategy_risk_full_bar_capture_matches_default_off_exactly(tmp_path, monkeypatch, reject):
    import backtest.engine as engine_module
    from config.config import config
    from router.router import Router
    fixture = capture_fixture(tmp_path/'inputs')
    fixture.wrapped.close()
    settings = _capture_engine_settings(config, reject=reject)
    monkeypatch.setattr(config, '_config', settings)
    synthetic_clock = (fixture.events[0].timestamp+pd.Timedelta(hours=12)).to_pydatetime()
    monkeypatch.setattr(engine_module, 'TradingEventPipeline',
                        lambda **kwargs: TradingEventPipeline(**kwargs, clock=lambda: synthetic_clock))
    baseline, baseline_events, _ = _native_engine_run(fixture, None)
    disabled = _engine_capture_observer(fixture, tmp_path/'disabled', enabled=False)
    off, off_events, _ = _native_engine_run(fixture, disabled)
    enabled = _engine_capture_observer(fixture, tmp_path, enabled=True)
    actual, actual_events, engine = _native_engine_run(fixture, enabled)
    pd.testing.assert_frame_equal(baseline['equity_curve'], off['equity_curve'])
    pd.testing.assert_frame_equal(baseline['equity_curve'], actual['equity_curve'])
    assert baseline_events == off_events == actual_events
    assert not (tmp_path/'disabled').exists()
    assert isinstance(engine.event_processor.router, Router)
    assert isinstance(engine.event_processor.risk_manager, RiskManager)
    assert actual['accounting_check']['ok']
    binding = enabled.export_binding()
    if reject:
        assert not binding['intents'] and not actual['trades']
    else:
        assert len(actual['trades']) == 3
        assert all(row['qty'] == pytest.approx(.5) for row in actual['trades'])
        assert binding['intents'][0]['qty'] > 1.5
    replay = account.build_forward_account_receipt(protocol_id='protocol', frozen_candidate_id='candidate',
        session_id=enabled.runtime.broker.account_id,
        session_started_at=(fixture.events[0].timestamp-pd.Timedelta(days=1)).isoformat(),
        observations=list(fixture.observations.values()), market_events=binding['market_events'],
        decisions=binding['decisions'], intents=binding['intents'], execution_binding_id=binding['binding_id'],
        **enabled.runtime.registration['account_config'])
    verified = account.verify_forward_account_receipt(replay, protocol_id='protocol', frozen_candidate_id='candidate',
        qualifying_observation_ids=fixture.observations, observation_receipts=fixture.observations,
        execution_bindings={binding['binding_id']: binding}, binding_store_root=tmp_path)
    assert verified['engineering'] == 'pass' and verified['portfolio_return'] is None
    assert verified['original_execution_binding']['original_policy_command_origin_linked'] is (not reject)
    enabled.close()


@pytest.mark.parametrize("field", ["capital_score_source", "contract"])
def test_main_selector_contract_cannot_change_after_capture_freeze(tmp_path, field):
    fixture = capture_fixture(tmp_path)
    if field == "capital_score_source":
        fixture.selector.capital_score_source = "original_score"
    else:
        fixture.selector.contract["return_gate"] = False
    with pytest.raises(ValueError, match="frozen capture source/model"):
        fixture.wrapped.process(fixture.events[0])
    assert not fixture.broker.orders_by_id
    fixture.wrapped.close()


def test_engine_final_tail_activity_stays_unproven_and_cannot_hide_behind_bar_checkpoint(tmp_path, monkeypatch):
    import backtest.engine as engine_module
    from config.config import config
    fixture = capture_fixture(tmp_path/"inputs")
    fixture.wrapped.close()
    monkeypatch.setattr(config, "_config", _capture_engine_settings(config, reject=False))
    synthetic_clock = (fixture.events[0].timestamp+pd.Timedelta(hours=12)).to_pydatetime()
    monkeypatch.setattr(engine_module, "TradingEventPipeline",
                        lambda **kwargs: TradingEventPipeline(**kwargs, clock=lambda: synthetic_clock))
    enabled = _engine_capture_observer(fixture, tmp_path, enabled=True)
    # Original native terminal execution cannot flatten 1.5 units with a
    # 0.5-unit cap. Capture must preserve that original failure and raw tail.
    with pytest.raises(ValueError, match="End-window exit cannot fill"):
        _native_engine_run(fixture, enabled, terminal_policy="forced_liquidation")
    with pytest.raises(ValueError, match="outside captured runtime"):
        enabled.export_binding()
    enabled.close()


def test_same_model_cannot_use_new_settings_under_original_frozen_protocol_identity(tmp_path):
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.close()
    with pytest.raises(ValueError, match="settings differ from frozen protocol/candidate"):
        ForwardPolicyCapture(tmp_path/"different-settings", enabled=True).wrap(fixture.wrapped.processor,
            broker=fixture.broker, protocol_id="protocol", frozen_candidate_id="candidate", model_id=fixture.selector.model.model_id,
            frozen_protocol={"protocol_id": "protocol", "settings": {"selection": {"score_cap": 9.}}},
            frozen_candidate={"model_id": fixture.selector.model.model_id},
            evidence_resolver=lambda event: fixture.evidence[event.timestamp.isoformat()])
    assert not fixture.broker.orders_by_id


@pytest.mark.parametrize("wrong_policy", [False, True])
def test_actual_policy_identity_is_checked_even_without_optional_rl_candidate_label(tmp_path, wrong_policy):
    from research.ml_selection.models import BernoulliPolicy
    fixture = capture_fixture(tmp_path)
    fixture.wrapped.close()
    model = fixture.selector.model
    policy = BernoulliPolicy(FEATURE_COLUMNS, scaler=model.scaler, bias=1.)
    fixture.wrapped.processor.candidate_selector = ResearchSelector(fixture.selector.table.reset_index(),
        mode="policy", model=model, policy=policy)
    options = dict(broker=fixture.broker, protocol_id="protocol", frozen_candidate_id="candidate", model_id=model.model_id,
        frozen_protocol={"protocol_id": "protocol"},
        frozen_candidate={"parent_model_id": model.model_id,
                          "model_id": "unrelated-policy" if wrong_policy else policy.model_id},
        evidence_resolver=lambda event: fixture.evidence[event.timestamp.isoformat()])
    if wrong_policy:
        with pytest.raises(ValueError, match="frozen policy does not identify"):
            ForwardPolicyCapture(tmp_path/"policy", enabled=True).wrap(fixture.wrapped.processor, **options)
    else:
        captured = ForwardPolicyCapture(tmp_path/"policy", enabled=True).wrap(fixture.wrapped.processor, **options)
        assert captured.registration["policy"]["artifacts"]["policy"]["model_id"] == policy.model_id
        captured.close()
    assert not fixture.broker.orders_by_id
