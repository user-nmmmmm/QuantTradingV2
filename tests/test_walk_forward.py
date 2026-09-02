"""Walk-forward selection that re-runs the engine, and the FDR it feeds.

The project had the statistics (``walk_forward_splits``, ``benjamini_hochberg``,
``deflated_sharpe_ratio``) but nothing bound them to a backtest: ``optimize.py
--oos`` ranked candidates on the full sample and split the winner's equity
curve afterwards, so the "out-of-sample" segment had already been seen by the
selection step. It also handed the FDR correction an empty p-value list while
testing sixteen parameter combinations.

These pin the protocol rather than any particular number: what the selection
runs are allowed to see, where routing starts, which half decides the winner,
and that the correction receives one hypothesis per candidate.
"""
from __future__ import annotations

from contextlib import contextmanager

import pandas as pd
import pytest

from analysis.walk_forward import (
    WalkForwardConfig,
    common_timeline,
    run_walk_forward,
)
from core.metrics import one_sided_bootstrap_p_value
from core.state import MarketState
from strategies.base import Strategy

SYMBOL = "BTC/USDT"
WARMUP = 10


class _SpyStrategy(Strategy):
    """Records the bar timestamp of every entry decision it is asked to make."""

    def __init__(self, name: str, seen: list, *, enter_every: int = 0) -> None:
        super().__init__(name, set(MarketState))
        self._seen = seen
        self._enter_every = enter_every

    def should_enter(self, symbol, i, df, state, portfolio):
        self._seen.append(pd.Timestamp(df.index[i]))
        if self._enter_every and i % self._enter_every == 0:
            return {"action": "buy", "order_type": "market", "stop_loss": None}
        return None

    def should_exit(self, symbol, i, df, state, portfolio):
        if self._enter_every and i % self._enter_every == self._enter_every // 2:
            return {"action": "sell", "reason": "signal", "order_type": "market"}
        return None


def _prices(bars: int = 200) -> pd.DataFrame:
    """A gentle uptrend with enough movement to give returns a variance."""
    index = pd.date_range("2024-01-01", periods=bars, freq="D", tz="UTC")
    closes = [100.0 + i * 0.5 + (3.0 if i % 7 == 0 else 0.0) for i in range(bars)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [price + 1.5 for price in closes],
            "low": [price - 1.5 for price in closes],
            "close": closes,
            "volume": 1_000_000.0,
        },
        index=index,
    )


def _candidates(seen_by_name: dict, cadences: dict) -> dict:
    def _factory(name: str):
        def build():
            return {
                name: _SpyStrategy(
                    name, seen_by_name.setdefault(name, []),
                    enter_every=cadences[name],
                )
            }
        return build

    return {name: _factory(name) for name in cadences}


@contextmanager
def _routed_to_the_only_strategy():
    """Send every regime to whichever single strategy a candidate builds."""
    import backtest.engine as engine_module
    from router.router import Router

    def _router_factory(strategies, _configuration, log_path=None):
        only = next(iter(strategies))
        return Router(
            strategies,
            regime_map={state.name: only for state in MarketState},
            log_path=log_path,
        )

    original = engine_module.build_router
    engine_module.build_router = _router_factory
    try:
        yield
    finally:
        engine_module.build_router = original


def _config(**overrides) -> WalkForwardConfig:
    return WalkForwardConfig(**{
        "train_size": 40, "validation_size": 30, "test_size": 30,
        "purge_size": 5, "warmup_period": WARMUP, "initial_capital": 10_000.0,
        "bootstrap_samples": 200, **overrides,
    })


def _run(seen_by_name: dict, cadences: dict, **overrides):
    with _routed_to_the_only_strategy():
        return run_walk_forward(
            {SYMBOL: _prices()},
            _candidates(seen_by_name, cadences),
            _config(**overrides),
        )


@pytest.fixture(scope="module")
def outcome():
    seen: dict = {}
    result = _run(seen, {"fast": 9, "slow": 21})
    return result, seen


class TestSelectionNeverSeesTheTestWindow:
    """The regression the whole module exists for."""

    def test_windows_were_actually_produced(self, outcome):
        result, _ = outcome
        assert result["windows"], f"no windows ran: {result['skipped_windows']}"

    def test_a_run_decides_on_no_bar_outside_its_own_window(self):
        """``_run_window`` is where a leak would happen; assert on it directly.

        Bars before ``start`` are fed only so indicators are warm - the
        strategy must never be asked for a decision on them - and no bar after
        ``end`` may reach the run at all.
        """
        from analysis.walk_forward import _run_window

        prices = _prices()
        timeline = common_timeline({SYMBOL: prices})
        start, end = timeline[50], timeline[79]
        seen: list = []

        with _routed_to_the_only_strategy():
            outcome = _run_window(
                {SYMBOL: prices},
                lambda: {"spy": _SpyStrategy("spy", seen, enter_every=9)},
                start=start, end=end,
                warmup_start=timeline[50 - WARMUP], config=_config(),
            )

        assert seen, "the strategy was never consulted"
        # The prefix is fed so indicators are warm, but the first decision is
        # taken on the window's own first bar.
        assert min(seen) == start
        # Not == end: entry decisions are skipped on bars where a position is
        # already held. What matters is that nothing past the window is seen.
        assert max(seen) <= end
        # ... while the run itself did cover the window to its last bar.
        assert outcome["returns"].index.max() >= end

    def test_a_purge_gap_separates_selection_from_test(self, outcome):
        result, _ = outcome
        for window in result["windows"]:
            assert window["purged_bars"] == 5
            assert pd.Timestamp(window["validation_start"]) < pd.Timestamp(
                window["test_start"]
            )

    def test_test_windows_do_not_overlap_each_other(self, outcome):
        result, _ = outcome
        spans = [
            (pd.Timestamp(window["test_start"]), pd.Timestamp(window["test_end"]))
            for window in result["windows"]
        ]
        for (_, previous_end), (next_start, _) in zip(spans, spans[1:]):
            assert previous_end < next_start


class TestWarmupHandling:
    def test_a_window_without_room_for_warmup_is_skipped_not_shortened(self):
        """Shortening it would give that window different indicators."""
        seen: dict = {}
        result = _run(seen, {"fast": 9}, warmup_period=60)

        reasons = [row["reason"] for row in result["skipped_windows"]]
        assert "insufficient_warmup_history" in reasons
        first = next(
            row for row in result["skipped_windows"]
            if row["reason"] == "insufficient_warmup_history"
        )
        assert first["required_bars"] == 60
        assert first["available_bars"] < 60

    def test_returns_start_at_the_window_not_at_the_warmup_prefix(self):
        """The prefix is flat capital; counting it would dilute the window."""
        from analysis.walk_forward import _run_window

        prices = _prices()
        timeline = common_timeline({SYMBOL: prices})
        start, end = timeline[50], timeline[79]

        with _routed_to_the_only_strategy():
            outcome = _run_window(
                {SYMBOL: prices},
                lambda: {"spy": _SpyStrategy("spy", [], enter_every=9)},
                start=start, end=end,
                warmup_start=timeline[50 - WARMUP], config=_config(),
            )

        returns = outcome["returns"]
        assert not returns.empty
        assert returns.index.min() > start   # first return needs two points
        assert returns.index.max() >= end


class TestSelectionRule:
    def test_the_winner_comes_from_the_validation_half(self, outcome):
        result, _ = outcome
        for window in result["windows"]:
            scored = {
                name: entry["validation_score"]
                for name, entry in window["scores"].items()
                if entry["validation_score"] is not None
            }
            assert window["selected"] == max(scored, key=scored.get)

    def test_disagreement_between_halves_is_reported_not_hidden(self, outcome):
        result, _ = outcome
        for window in result["windows"]:
            assert window["selection_agrees"] == (
                window["train_best"] == window["selected"]
            )
        stability = result["procedure"]["selection_stability"]
        assert 0.0 <= stability["train_validation_agreement"] <= 1.0
        assert stability["sample_size"] == len(result["windows"])

    def test_an_unknown_selection_metric_is_refused(self):
        with pytest.raises(ValueError, match="selection_metric"):
            WalkForwardConfig(
                train_size=10, validation_size=10, test_size=10,
                selection_metric="ProfitFactor",
            )


class TestMultipleTestingIsActuallyFed:
    """``optimize.py`` passed ``p_values=[]`` while trying sixteen combinations."""

    def test_every_candidate_contributes_one_hypothesis(self, outcome):
        result, _ = outcome
        correction = result["multiple_testing"]

        assert correction["status"] == "ok"
        assert correction["sample_size"] == len(result["candidates"])

    def test_each_candidate_carries_its_own_adjusted_p_value(self, outcome):
        result, _ = outcome
        for row in result["candidates"].values():
            assert row["p_value"] is not None
            assert row["adjusted_p_value"] is not None
            assert row["adjusted_p_value"] >= row["p_value"]
            assert isinstance(row["survives_fdr"], bool)

    def test_candidates_are_pooled_across_every_test_window(self, outcome):
        result, _ = outcome
        for row in result["candidates"].values():
            assert row["test_windows"] == len(result["windows"])

    def test_the_trial_count_deflating_sharpe_is_the_candidate_count(self, outcome):
        result, _ = outcome
        deflated = result["procedure"]["deflated_sharpe"]

        if deflated["status"] == "ok":
            assert deflated["trials"] == len(result["candidates"])


class TestBootstrapPValue:
    def test_a_bootstrap_never_justifies_p_equals_zero(self):
        """A literal zero would make a candidate survive any FDR threshold."""
        result = one_sided_bootstrap_p_value([1.0] * 50, n_samples=100)

        assert result["p_value"] == pytest.approx(1 / 101)
        assert result["p_value"] > 0.0

    def test_losing_returns_are_not_significant(self):
        result = one_sided_bootstrap_p_value([-0.01] * 50, n_samples=100)

        assert result["p_value"] == pytest.approx(1.0)

    def test_too_few_observations_is_insufficient_not_one(self):
        result = one_sided_bootstrap_p_value([0.01])

        assert result["status"] == "insufficient"
        assert result["p_value"] is None


class TestTimeline:
    def test_positions_count_against_the_union_of_all_symbols(self):
        first = _prices(50)
        second = _prices(50).iloc[10:]

        timeline = common_timeline({"A": first, "B": second})

        assert len(timeline) == 50
        assert timeline.is_monotonic_increasing


class TestParallelGridIsOrderStable:
    """``--jobs`` must not change what a run reports, only how fast it gets there."""

    def test_results_follow_the_grid_order_not_completion_order(self, monkeypatch):
        import analysis.optimize as optimize

        # Stand in for the engine so this stays a test of the scheduling glue.
        completion_order = []

        def _fake(task):
            _, entry, exit_, _capital = task
            completion_order.append((entry, exit_))
            return {"name": f"entry={entry},exit={exit_}", "Entry_Window": entry,
                    "Exit_Window": exit_, "Total_Ret%": float(entry), "Max_DD%": 0.0,
                    "Sharpe": 0.0, "Trades": 0, "Win_Rate%": 0.0,
                    "returns": pd.Series(dtype=float)}

        monkeypatch.setattr(optimize, "evaluate_one_candidate", _fake)
        grid = [(20, 5), (30, 10), (50, 15)]

        rows = optimize._evaluate_grid({}, grid, 10_000.0, jobs=1)

        assert [(row["Entry_Window"], row["Exit_Window"]) for row in rows] == grid

    def test_a_single_job_takes_the_serial_path(self, monkeypatch):
        """Spawning a pool for one worker would cost more than it saves."""
        import analysis.optimize as optimize

        def _explode(*args, **kwargs):
            raise AssertionError("ProcessPoolExecutor must not be used for jobs=1")

        monkeypatch.setattr(optimize, "ProcessPoolExecutor", _explode)
        monkeypatch.setattr(
            optimize, "evaluate_one_candidate",
            lambda task: {"Entry_Window": task[1], "Exit_Window": task[2]},
        )

        assert len(optimize._evaluate_grid({}, [(20, 5)], 10_000.0, jobs=1)) == 1
