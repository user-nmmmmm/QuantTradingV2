from unittest.mock import MagicMock, patch

import pandas as pd

from backtest.engine import BacktestEngine
from core.portfolio import Portfolio
from core.risk import BreakerAction, RiskControlDecision, RiskManager
from core.runtime import EventProcessor, MarketDataSlice
from core.state import MarketState


def _frame(values=(100.0,)):
    index = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.DataFrame(
        {
            "open": values,
            "high": [value + 1 for value in values],
            "low": [value - 1 for value in values],
            "close": values,
            "volume": [1000.0] * len(values),
        },
        index=index,
    )


class _SplitRouter:
    def __init__(self):
        self.position_management_calls = MagicMock(return_value=False)
        self.entry_candidate_calls = MagicMock(return_value=object())

    def process_position_management(self, *args, **kwargs):
        return self.position_management_calls(*args, **kwargs)

    def collect_entry_candidate(self, *args, **kwargs):
        return self.entry_candidate_calls(*args, **kwargs)


def test_block_new_runs_position_management_but_never_collects_entry():
    portfolio = Portfolio(8400.0)
    risk = RiskManager(
        daily_loss_limit=0.50,
        portfolio_drawdown_reduce=0.10,
        portfolio_drawdown_block=0.15,
        portfolio_drawdown_liquidate=0.20,
        portfolio_drawdown_lock=0.25,
    )
    risk.high_water_equity = 10000.0
    router = _SplitRouter()
    execution = MagicMock()
    execution.portfolio = portfolio
    state = MagicMock()
    state.get_state.return_value = MarketState.SIDEWAYS
    allocator = MagicMock()
    processor = EventProcessor(
        portfolio=portfolio,
        execution=execution,
        risk_manager=risk,
        state_machine=state,
        router=router,
        allocator=allocator,
        warmup_period=0,
        initial_equity=10000.0,
    )
    frame = _frame()
    event = MarketDataSlice(
        timestamp=frame.index[0],
        bars={"TEST": frame.iloc[0]},
        histories={"TEST": frame},
        positions={"TEST": 0},
    )

    result = processor.process(event, execute_market_event=False)

    assert result.breaker_action == "block_new"
    router.position_management_calls.assert_called_once()
    router.entry_candidate_calls.assert_not_called()
    allocator.allocate.assert_not_called()


def test_daily_loss_uses_previous_session_close_as_baseline():
    portfolio = Portfolio(100.0)
    risk = MagicMock()
    risk.breaker_action = BreakerAction.NORMAL
    risk.check_circuit_breaker.return_value = False
    execution = MagicMock()
    execution.portfolio = portfolio
    processor = EventProcessor(
        portfolio=portfolio,
        execution=execution,
        risk_manager=risk,
        state_machine=MagicMock(),
        router=MagicMock(),
        allocator=MagicMock(),
        warmup_period=10,
        initial_equity=100.0,
    )
    frame = _frame((100.0, 100.0))
    for index, timestamp in enumerate(frame.index):
        if index == 1:
            portfolio.cash = 90.0
        processor.process(
            MarketDataSlice(
                timestamp=timestamp,
                bars={"TEST": frame.iloc[index]},
                histories={"TEST": frame.iloc[: index + 1]},
                positions={"TEST": index},
            ),
            execute_market_event=False,
        )

    second_call = risk.check_circuit_breaker.call_args_list[1]
    assert second_call.args == (90.0, 100.0)


def test_manual_resume_creates_new_epoch_and_new_reduce_transition():
    risk = RiskManager(
        daily_loss_limit=0.90,
        portfolio_drawdown_reduce=0.10,
        portfolio_drawdown_block=0.20,
        portfolio_drawdown_liquidate=0.30,
        portfolio_drawdown_lock=0.40,
    )
    risk.check_circuit_breaker(100.0, 100.0)
    first = risk.check_circuit_breaker(
        89.0, 100.0, occurred_at=pd.Timestamp("2024-01-01"), bar_index=0
    )
    risk.manual_resume(
        approved_by="test-protocol", current_equity=89.0, rebase_high_water=True
    )
    second = risk.check_circuit_breaker(
        79.0, 89.0, occurred_at=pd.Timestamp("2024-02-01"), bar_index=31
    )

    assert first.action is BreakerAction.REDUCE
    assert second.action is BreakerAction.REDUCE
    assert first.transition_id != second.transition_id
    assert second.breaker_epoch == 1


class _LiquidatingRisk:
    def __init__(self):
        self.breaker_action = BreakerAction.LIQUIDATE
        self.high_water_equity = 100.0
        self.last_drawdown = 0.25
        self.daily_loss_triggered = False
        self.breaker_epoch = 0
        self.breaker_audit = []
        self.reduced_risk_multiplier = 0.5

    def reset_daily_breaker(self):
        return None

    def check_circuit_breaker(self, *_args, **_kwargs):
        if self.breaker_action is BreakerAction.NORMAL:
            return RiskControlDecision(
                action=BreakerAction.NORMAL,
                allow_position_management=True,
                allow_new_entries=True,
                breaker_epoch=self.breaker_epoch,
            )
        return RiskControlDecision(
            action=BreakerAction.LIQUIDATE,
            allow_position_management=False,
            allow_new_entries=False,
            force_liquidate=True,
            terminal=True,
            reason_codes=("portfolio_drawdown_liquidate",),
            transition_id="epoch-0-transition-1",
        )

    def record_breaker_action_result(self, *_args, **_kwargs):
        return None

    def _blocks_new_risk(self):
        return self.breaker_action is not BreakerAction.NORMAL

    def manual_resume(self, **_kwargs):
        self.breaker_action = BreakerAction.NORMAL
        self.breaker_epoch += 1


def test_liquidate_terminates_active_period_and_preserves_flat_capital_tail():
    data = _frame((100.0, 99.0, 98.0))
    engine = BacktestEngine(
        initial_capital=100.0,
        slippage=0.0,
        warmup_period=0,
        breaker_policy={"on_liquidate": "terminate"},
    )
    with patch("backtest.engine.build_risk_manager", return_value=_LiquidatingRisk()):
        result = engine.run({"TEST": data}, strategies={}, routing_log_enabled=False)

    assert result["lifecycle"]["status"] == "terminated_by_risk"
    assert result["lifecycle"]["active_end"] == data.index[0]
    assert result["lifecycle"]["inactive_bars"] == 2
    assert list(result["equity_curve"].index) == list(data.index)
    assert result["equity_curve"]["equity"].nunique() == 1
    assert result["breaker_state"]["daily_loss_triggered"] is False


def test_timed_rebase_counts_flat_bars_once_and_resumes_in_a_new_epoch():
    data = _frame((100.0, 99.0, 101.0))
    engine = BacktestEngine(
        initial_capital=100.0,
        slippage=0.0,
        warmup_period=0,
        breaker_policy={
            "on_liquidate": "cooldown",
            "shadow_diagnostics": False,
            "recovery": {
                "mode": "timed_rebase",
                "flat_bars_required": 1,
                "health_bars_required": 1,
                "rebase_high_water": True,
                "max_resumes": 1,
                "approved_by": "test-protocol",
            },
        },
    )
    risk = _LiquidatingRisk()
    with patch("backtest.engine.build_risk_manager", return_value=risk):
        result = engine.run({"TEST": data}, strategies={}, routing_log_enabled=False)

    assert result["lifecycle"]["status"] == "completed"
    assert result["lifecycle"]["resume_count"] == 1
    assert result["lifecycle"]["breaker_epochs"] == 2
    assert risk.breaker_epoch == 1
