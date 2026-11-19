"""Replaying history after a restart restores cooldown progress without any order side effects (C5)."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from core.trading.state import MarketState
from live_trading.catchup import CatchupPlan, plan_catchup, replay_state_only, runtime_checkpoint
from router.router import Router


def _frame(rows=12):
    return pd.DataFrame({"close": range(rows)}, index=pd.date_range("2026-10-01", periods=rows, freq="min"))


def _engine(state=MarketState.TREND_UP, *, cooldown_bars=2, strategies=None):
    class Machine:
        def get_state(self, frame, location):
            return state
    router = Router({}, regime_map={"SIDEWAYS": "Cash", "TREND_UP": "Trend"}, cooldown_bars=cooldown_bars)
    return SimpleNamespace(state_machine=Machine(), router=router, strategies=strategies or {},
                           event_processor=SimpleNamespace(_last_market_states={}))


# ---- plan_catchup input checks ---------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, 2.5, True, None])
def test_catchup_limit_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError, match="positive integer"):
        plan_catchup(_frame(), None, timeframe="1m", max_bars=bad)


def test_empty_history_yields_an_empty_plan_and_unsorted_history_is_rejected():
    assert plan_catchup(_frame().iloc[0:0], None, timeframe="1m") == CatchupPlan((), None, 0, False)
    frame = _frame()
    with pytest.raises(ValueError, match="sorted and unique"):
        plan_catchup(frame.iloc[::-1], frame.index[0], timeframe="1m")
    duplicated = pd.concat([frame.iloc[:3], frame.iloc[2:4]])
    with pytest.raises(ValueError, match="sorted and unique"):
        plan_catchup(duplicated, frame.index[0], timeframe="1m")


# ---- replay_state_only: router cooldown ------------------------------------------------------------------------

def test_a_regime_change_starts_a_cooldown_and_later_bars_advance_it_until_it_ends():
    frame = _frame()
    engine = _engine(cooldown_bars=2)
    engine.router.symbol_states["A"] = MarketState.SIDEWAYS
    replay_state_only(engine, "A", frame, frame.index[3])
    assert engine.router._cooldown_progress["A"] == (frame.index[3], 0)

    replay_state_only(engine, "A", frame, frame.index[5])                  # two observed bars later: still cooling
    assert engine.router._cooldown_progress["A"] == (frame.index[5], 2)
    assert "A" in engine.router.cooldowns
    assert engine.router.symbol_states["A"] is MarketState.TREND_UP         # the new regime is recorded at once; the cooldown only delays acting on it

    replay_state_only(engine, "A", frame, frame.index[6])                  # third bar: the cooldown has run its course
    assert "A" not in engine.router.cooldowns and "A" not in engine.router._cooldown_progress
    assert "A" not in engine.router._cooldown_started_at
    assert engine.router.symbol_states["A"] is MarketState.TREND_UP
    assert engine.event_processor._last_market_states["A"] is MarketState.TREND_UP


def test_replaying_the_same_bar_twice_does_not_advance_the_cooldown_twice():
    frame = _frame()
    engine = _engine(cooldown_bars=5)
    engine.router.symbol_states["A"] = MarketState.SIDEWAYS
    replay_state_only(engine, "A", frame, frame.index[3])
    replay_state_only(engine, "A", frame, frame.index[4])
    replay_state_only(engine, "A", frame, frame.index[4])
    assert engine.router._cooldown_progress["A"] == (frame.index[4], 1)


def test_a_legacy_cooldown_without_a_start_time_ends_at_its_bar_index_and_then_reacts_to_the_regime_change():
    frame = _frame()
    engine = _engine(cooldown_bars=2)
    engine.router.symbol_states["A"] = MarketState.SIDEWAYS
    engine.router.cooldowns["A"] = 6
    replay_state_only(engine, "A", frame, frame.index[4])
    assert engine.router.cooldowns["A"] == 6 and engine.router.symbol_states["A"] is MarketState.SIDEWAYS
    replay_state_only(engine, "A", frame, frame.index[7])
    # The old cooldown is over; the regime changed while it ran, so a fresh one starts at this bar.
    assert engine.router.cooldowns["A"] == 9 and engine.router._cooldown_started_at["A"] == frame.index[7]
    assert engine.router.symbol_states["A"] is MarketState.TREND_UP


# ---- replay_state_only: strategy cooldown ----------------------------------------------------------------------

class _Strategy:
    name = "trend"

    def __init__(self, context=None, trade_state=None):
        self.context = context or {}
        self.trade_state = trade_state if trade_state is not None else {}
        self._consumed_close_event_ids = {"b", "a"}
        self.entered = []

    def _just_entered(self, location, frame, context):
        self.entered.append(location)


def test_strategy_cooldown_counts_observed_bars_and_expires_after_24():
    frame = _frame(40)
    strategy = _Strategy(context={"A": {"entry_price": 1.0}},
                         trade_state={"A": {"cooldown_started_at": frame.index[2].isoformat()}})
    engine = _engine(strategies={"trend": strategy})
    replay_state_only(engine, "A", frame, frame.index[10])
    state = strategy.trade_state["A"]
    assert state["cooldown_bar_count"] == 8 and state["cooldown_last_timestamp"] == frame.index[10].isoformat()
    assert strategy.entered == [10] and "cooldown_until" not in state

    replay_state_only(engine, "A", frame, frame.index[10])                 # same bar again: nothing is added
    assert strategy.trade_state["A"]["cooldown_bar_count"] == 8

    replay_state_only(engine, "A", frame, frame.index[30])
    state = strategy.trade_state["A"]
    assert state["cooldown_bar_count"] == 28
    assert "cooldown_started_at" not in state and state["cooldown_until"] == -1


# ---- runtime_checkpoint ----------------------------------------------------------------------------------------

def test_runtime_checkpoint_collects_router_and_strategy_progress_in_a_stable_shape():
    strategy = _Strategy(context={"A": {"k": 1}}, trade_state={"A": {"cooldown_bar_count": 3}})
    engine = _engine(strategies={"trend": strategy})
    engine.router.symbol_states["A"] = MarketState.TREND_UP
    values = runtime_checkpoint(engine)
    assert set(values) == {"router_runtime", "strategy_runtime:trend"}
    assert values["router_runtime"]["schema"] == "router-runtime/v1"
    assert values["router_runtime"]["states"] == {"A": "TREND_UP"}
    assert values["strategy_runtime:trend"] == {"context": {"A": {"k": 1}}, "consumed_close_event_ids": ["a", "b"],
                                                "trade_state": {"A": {"cooldown_bar_count": 3}}}
