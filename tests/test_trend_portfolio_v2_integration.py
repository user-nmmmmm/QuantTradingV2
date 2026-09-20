"""V2 changes strategy inputs while sharing execution, risk and governance."""

from copy import deepcopy
from dataclasses import asdict, replace

import pandas as pd
import pytest

from composition.factory import build_router, build_strategy_registry
from config.config import config
from core.broker import Broker
from core.portfolio import Portfolio
from core.protective_stops import ProtectiveStopPolicy
from core.risk import RiskManager
from core.risk.circuit_breaker import BreakerAction
from core.risk.portfolio_governor import CorrelationClusterPolicy, PortfolioRiskGovernor
from core.state import MarketState
from core.strategy_governance import GovernanceError, assert_live_admission
from core.strategy_health import HealthStatus, StrategyHealthPolicy
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.trend_portfolio_v2 import TrendPortfolioV2Strategy


SYMBOL = "BTC/USDT"
REGIMES = [
    (MarketState.TREND_UP, 1.0),
    (MarketState.SIDEWAYS, 0.25),
    (MarketState.VOLATILE, 0.5),
    (MarketState.TREND_DOWN, 0.0),
]


class _Configuration:
    def __init__(self, sections):
        self.sections = sections

    def get(self, section, key=None):
        value = self.sections.get(section)
        return value if key is None else (value or {}).get(key)

    def require(self, section, key=None):
        value = self.sections[section]
        return value if key is None else value[key]


def _configuration():
    sections = deepcopy(config._config)
    sections["routing"] = {state.name: "TrendPortfolioV2" for state, _ in REGIMES}
    sections["routing"]["NO_TRADE"] = "Cash"
    return _Configuration(sections)


def _frame(length=1):
    return pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "volume": 100000.0, "ATR_14": 5.0},
        index=pd.date_range("2024-01-01", periods=length, tz="UTC"),
    )


def _controlled_setup(monkeypatch, strategy):
    # Signal mechanics have their own tests. Isolate sizing and keep the real
    # health gate, risk manager, reservation projection and broker pipeline.
    monkeypatch.setattr(
        strategy, "_raw_entry_signal",
        lambda symbol, i, df, portfolio: {
            "action": "buy", "price": 100.0, "stop_loss": 90.0, "score": 1.0,
            "target_weight": .25, "breakout_atr": 1.0,
        },
    )


def _submit(path, strategy, state, portfolio, broker, risk, *, symbol=SYMBOL,
            governor=None):
    frame = _frame()
    if path == "on_bar":
        strategy.on_bar(symbol, 0, frame, state, portfolio, broker, risk,
                        current_prices={symbol: 100.0})
        return broker.pending_orders[-1] if broker.pending_orders else None
    candidate = strategy.build_entry_candidate(symbol, 0, frame, state, portfolio)
    if candidate is None:
        return None
    return strategy.submit_entry_candidate(
        candidate, portfolio=portfolio, broker=broker, risk_manager=risk,
        current_prices={symbol: 100.0}, risk_governor=governor,
    )


@pytest.mark.parametrize("path", ["on_bar", "candidate"])
@pytest.mark.parametrize("state,market_multiplier", REGIMES)
@pytest.mark.parametrize("health_multiplier", [1.0, 0.25])
def test_actual_pending_orders_multiply_regime_and_health_separately(
    monkeypatch, path, state, market_multiplier, health_multiplier,
):
    strategy = TrendPortfolioV2Strategy()
    _controlled_setup(monkeypatch, strategy)
    if health_multiplier < 1:
        strategy.health._transition(
            HealthStatus.PROBATION, _frame().index[0].to_pydatetime(),
            reason="integration_fixture",
        )
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    risk = RiskManager(risk_per_trade=0.02, max_pos_size_pct=1.0, min_entry_notional_pct=0.0)

    result = _submit(path, strategy, state, portfolio, broker, risk)

    if market_multiplier == 0:
        assert result is None
        assert not broker.pending_orders
        return
    expected_qty = 10.0 * market_multiplier * health_multiplier
    assert result.accepted
    assert len(broker.pending_orders) == 1
    assert broker.pending_orders[0].qty == pytest.approx(expected_qty)
    assert result.intent.approved_risk_amount == pytest.approx(expected_qty * 10.0)
    assert broker.pending_open_notional({SYMBOL: 100.0})[SYMBOL] == pytest.approx(
        expected_qty * 100.0,
    )
    assert portfolio.get_position(SYMBOL)["qty"] == 0  # Next-bar execution remains intact.
    assert strategy.health_snapshot()["risk_multiplier"] == health_multiplier
    assert strategy.health_snapshot()["status"] == (
        "probation" if health_multiplier < 1 else "active"
    )


@pytest.mark.parametrize("path", ["on_bar", "candidate"])
def test_baseline_default_multiplier_preserves_order_size(monkeypatch, path):
    strategy = TrendBreakoutStrategy()
    _controlled_setup(monkeypatch, strategy)
    assert all(strategy.entry_risk_multiplier(state) == 1.0 for state in MarketState)
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    result = _submit(path, strategy, MarketState.TREND_UP, portfolio, broker,
                     RiskManager(risk_per_trade=0.02, max_pos_size_pct=1.0))
    assert result.accepted
    assert broker.pending_orders[0].qty == pytest.approx(20.0)


@pytest.mark.parametrize("path", ["on_bar", "candidate"])
def test_existing_notional_cap_applies_after_market_scaling(monkeypatch, path):
    strategy = TrendPortfolioV2Strategy()
    _controlled_setup(monkeypatch, strategy)
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    risk = RiskManager(risk_per_trade=0.02, max_pos_size_pct=0.05)

    result = _submit(path, strategy, MarketState.TREND_UP, portfolio, broker, risk)

    # The 1% initial risk cap permits 10; existing $500 symbol cap permits 5.
    assert result.accepted
    assert broker.pending_orders[0].qty == pytest.approx(5.0)
    assert result.intent.approved_risk_amount == pytest.approx(50.0)


@pytest.mark.parametrize("path", ["on_bar", "candidate"])
def test_existing_portfolio_breaker_still_blocks_v2(monkeypatch, path):
    strategy = TrendPortfolioV2Strategy()
    _controlled_setup(monkeypatch, strategy)
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    risk = RiskManager(risk_per_trade=0.02)
    risk.portfolio_breaker_action = BreakerAction.BLOCK_NEW

    _submit(path, strategy, MarketState.SIDEWAYS, portfolio, broker, risk)

    assert not broker.pending_orders
    assert not strategy.get_context(SYMBOL).get("entry_pending")


def test_shared_session_governor_scales_then_exhausts_v2_budget(monkeypatch):
    strategy = TrendPortfolioV2Strategy()
    _controlled_setup(monkeypatch, strategy)
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    risk = RiskManager(risk_per_trade=0.02, max_pos_size_pct=1.0)
    governor = PortfolioRiskGovernor(
        CorrelationClusterPolicy(max_same_session_entry_risk=0.004),
    )
    governor.begin_session(0)

    first = _submit("candidate", strategy, MarketState.TREND_UP, portfolio, broker,
                    risk, governor=governor)
    second = _submit("candidate", strategy, MarketState.TREND_UP, portfolio, broker,
                     risk, symbol="ETH/USDT", governor=governor)

    assert first.accepted
    assert first.qty == pytest.approx(4.0)
    assert first.intent.approved_risk_amount == pytest.approx(40.0)
    assert second is None
    assert len(broker.pending_orders) == 1


@pytest.mark.parametrize("path", ["on_bar", "candidate"])
def test_inherited_health_lock_blocks_v2_raw_setups(monkeypatch, path):
    strategy = TrendPortfolioV2Strategy()
    _controlled_setup(monkeypatch, strategy)
    strategy.health._transition(
        HealthStatus.MANUAL_LOCK, _frame().index[0].to_pydatetime(),
        reason="integration_fixture",
    )
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)

    _submit(path, strategy, MarketState.TREND_UP, portfolio, broker, RiskManager())

    assert not broker.pending_orders
    assert strategy.health_snapshot()["status"] == "manual_lock"
    assert strategy.health_snapshot()["suppressed_raw_setups"] == 1


def test_flat_normal_regime_transitions_keep_v2_without_router_cooldowns(monkeypatch):
    configuration = _configuration()
    registry = build_strategy_registry(configuration)
    strategy = registry["TrendPortfolioV2"]
    _controlled_setup(monkeypatch, strategy)
    router = build_router(registry, configuration, allow_short=False)
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    frame = _frame(4)

    for i, (state, _) in enumerate(REGIMES):
        candidate = router.collect_candidate(
            SYMBOL, i, frame, state, portfolio, broker, RiskManager(),
        )
        if state == MarketState.TREND_DOWN:
            assert candidate is None
        else:
            assert candidate is not None
            assert candidate.strategy is strategy
            assert candidate.signal["action"] == "buy"
        assert not router.cooldowns
    assert router.regime_map["TREND_DOWN"] == "TrendPortfolioV2"
    assert not broker.pending_orders


def test_held_v2_long_survives_normal_regime_transitions():
    configuration = _configuration()
    registry = build_strategy_registry(configuration)
    router = build_router(registry, configuration, allow_short=False)
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    frame = _frame(130)
    order = broker.submit_order(
        SYMBOL, "buy", 5.0, price=100.0, stop_loss=90.0,
        approved_risk_amount=50.0, timestamp=frame.index[0],
        strategy_id="TrendPortfolioV2",
    )
    assert order.accepted
    broker.process_orders({SYMBOL: frame.iloc[1]})
    assert portfolio.get_position(SYMBOL)["qty"] == 5.0

    for i, (state, _) in enumerate(REGIMES, start=122):
        assert router.collect_candidate(
            SYMBOL, i, frame, state, portfolio, broker, RiskManager(),
        ) is None
        assert not broker.pending_orders
        assert portfolio.get_position(SYMBOL)["qty"] == 5.0
        assert not registry["TrendPortfolioV2"].get_context(SYMBOL).get("exit_pending")


def test_factory_opt_in_preserves_default_registry_and_baseline_routing():
    original = deepcopy(config._config)
    default_registry = build_strategy_registry(config)
    assert set(build_strategy_registry()) == set(default_registry) == {
        "TrendBreakout", "TrendBreakdown", "RangeMeanReversion", "VolatilityReversion",
    }
    configuration = _configuration()
    registry = build_strategy_registry(configuration)
    assert set(registry) == set(default_registry) | {"TrendPortfolioV2"}
    assert build_router(default_registry, config, allow_short=False).regime_map[
        "TREND_DOWN"
    ] == "Cash"
    assert config._config == original


def test_factory_injects_same_health_and_retains_all_other_stop_safeguards():
    configuration = _configuration()
    configuration.sections["strategy_health"] = {
        "cooldown_days": 9.0, "probation_risk_multiplier": 0.25,
    }
    configuration.sections["stops"] = {
        "initial_stop_mode": "hybrid", "use_trailing_stop": False,
        "atr_period": 21, "initial_atr_multiple": 3.0, "trailing_atr_multiple": 4.0,
        "min_stop_distance_pct": 0.007, "max_stop_distance_pct": 0.25,
        "breakeven_after_r": 2.0, "breakeven_cost_buffer": 0.2,
    }
    configuration.sections["research"] = {
        "experiment_id": "trend-v2/integration",
        "trend_breakout_parameters": {"entry_window": 30, "exit_window": 15},
    }
    registry = build_strategy_registry(configuration)
    strategy, baseline = registry["TrendPortfolioV2"], registry["TrendBreakout"]
    original_stop_policy = ProtectiveStopPolicy.from_mapping(configuration.get("stops"))

    assert strategy.health.policy == baseline.health.policy == StrategyHealthPolicy.from_mapping(
        configuration.get("strategy_health"),
    )
    assert strategy.health.strategy_name == "TrendPortfolioV2"
    assert strategy.health_state_key != baseline.health_state_key
    assert strategy.score_policy == baseline.score_policy
    assert (strategy.entry_window, strategy.exit_window) == (30, 15)
    assert strategy.use_obv == baseline.use_obv is True
    assert baseline.stop_policy == original_stop_policy
    assert asdict(strategy.stop_policy) == asdict(replace(
        original_stop_policy, initial_stop_mode="atr", use_atr_initial_stop=True,
        use_trailing_stop=True, initial_atr_multiple=2.0, trailing_atr_multiple=2.5,
    ))


def test_research_opt_in_does_not_admit_v2_for_live_routing():
    configuration = _configuration()
    assert "TrendPortfolioV2" in build_strategy_registry(configuration)
    with pytest.raises(GovernanceError, match="TrendPortfolioV2=unregistered"):
        assert_live_admission(configuration, ["TrendPortfolioV2"])


@pytest.mark.parametrize("path", ["on_bar", "candidate"])
def test_small_sideways_probation_order_still_obeys_shared_minimum(monkeypatch, path):
    strategy = TrendPortfolioV2Strategy()
    _controlled_setup(monkeypatch, strategy)
    strategy.health._transition(
        HealthStatus.PROBATION, _frame().index[0].to_pydatetime(), reason="integration_fixture",
    )
    portfolio = Portfolio(10000.0)
    broker = Broker(portfolio, commission_rate=0)
    result = _submit(path, strategy, MarketState.SIDEWAYS, portfolio, broker,
                     RiskManager(risk_per_trade=.02, max_pos_size_pct=1.0))
    assert result is None
    assert not broker.pending_orders  # $62.50 is below the unchanged $100 minimum.


def test_factory_accepts_preregistered_v2_modes_without_changing_v1():
    configuration = _configuration()
    configuration.sections["research"] = {
        "experiment_id": "v2/ablation",
        "trend_portfolio_v2": {"market_state_mode": "hard_gate", "exit_mode": "baseline",
                               "asset_base_weight": 1 / 6, "periods_per_year": 2190},
    }
    registry = build_strategy_registry(configuration)
    strategy = registry["TrendPortfolioV2"]
    assert strategy.market_state_mode == "hard_gate"
    assert strategy.stop_policy.resolved_initial_stop_mode == "structural_donchian"
    assert not strategy.stop_policy.use_trailing_stop
    assert strategy.periods_per_year == 2190
    assert strategy.asset_base_weight == 1 / 6
    assert registry["TrendBreakout"].allowed_states == {MarketState.TREND_UP}
