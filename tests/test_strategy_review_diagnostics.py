"""Post-run diagnostics must not invent signal continuity or counterfactual PnL."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from analysis.strategy_review_diagnostics import write_review_diagnostics
from core.state import MarketState, MarketStateMachine


def _frame(count=30):
    index = pd.date_range("2024-01-01", periods=count)
    return pd.DataFrame({"open": 100., "high": 100., "low": 90., "close": 100.,
                         "volume": 1_000.}, index=index)


def _summary(folder, start, end):
    (folder / "summary.json").write_text(json.dumps({"start": str(start), "end": str(end)}), encoding="utf-8")


def test_raw_delay_and_later_breakout_are_not_the_same_signal(tmp_path, monkeypatch):
    frame = _frame()
    frame.loc[frame.index[20:24], "close"] = [101, 102, 103, 104]
    frame.loc[frame.index[20:24], "high"] = [101, 102, 103, 104]
    frame["market_state"] = MarketState.TREND_DOWN  # Must not reuse cached states.
    original = frame.copy(deep=True)
    calls = []

    def classify(self, source):
        assert "market_state" not in source
        calls.append(self.stability_period)
        raw = source.close.map(lambda price: MarketState.TREND_UP if price > 100 else MarketState.SIDEWAYS)
        return self._apply_stability_filter(raw)

    monkeypatch.setattr(MarketStateMachine, "calculate_states", classify)
    first, accepted, filled = frame.index[20], frame.index[22], frame.index[23]
    _summary(tmp_path, first, filled)
    result = {
        # Deliberately wider than summary.json: authoritative run bounds win.
        "equity_curve": pd.DataFrame({"equity": 10_000.}, index=frame.index),
        "entry_observations": [
            {"symbol": "BTC/USDT", "timestamp": first, "reason": "market_state_cash"},
            {"symbol": "BTC/USDT", "timestamp": accepted, "reason": "order_accepted",
             "strategy": "TrendBreakout", "rank": 1, "score": 1.2, "order_id": "entry-1"},
            {"symbol": "BTC/USDT", "timestamp": frame.index[-1], "rank": 1, "score": 9},
        ],
        "trades": [{"symbol": "BTC/USDT", "side": "buy", "qty": 1,
                    "fill_price": 104, "fill_time": filled, "order_id": "entry-1"}],
    }
    frozen = deepcopy(result)
    summary = write_review_diagnostics(tmp_path, {"BTC/USDT": frame}, result,
                                       {"state": {"stability_period": 3}})
    rows = pd.read_csv(tmp_path / "review_setup_timing.csv")
    early = rows.loc[pd.to_datetime(rows.signal_time) == first].iloc[0]
    later = rows.loc[pd.to_datetime(rows.signal_time) == accepted].iloc[0]
    assert early.outcome == "filtered_original_setup"
    assert pd.isna(early.first_fill_time)
    assert later.outcome == "same_signal_filled" and later.signal_to_fill_bars == 1
    assert early.signal_id != later.signal_id
    assert early.confirmation_delay_bars == 2
    assert calls == [1, 3]
    assert rows.counterfactual_pnl.isna().all()
    assert summary["entry_observation_count"] == 2
    assert summary["period_source"] == "explicit_run_period"
    pd.testing.assert_frame_equal(frame, original)
    pd.testing.assert_frame_equal(result["equity_curve"], frozen["equity_curve"])
    assert result["entry_observations"] == frozen["entry_observations"]


def test_known_mfe_excludes_exit_bar_high_and_scores_match_exact_order(tmp_path):
    frame = _frame(5)
    frame.high = [105, 120, 130, 1_000, 118]
    index = frame.index
    _summary(tmp_path, index[0], index[-1])
    event = {"close_event_id": "c1", "lot_id": "L1", "position_id": "P1",
             "symbol": "BTC/USDT", "opening_strategy_id": "TrendBreakout",
             "qty": 2, "timestamp": index[3], "realized_pnl": 18,
             "initial_risk": 20, "exit_reason": "protective_stop", "is_position_fully_closed": True}
    result = {"close_event_records": [event], "entry_observations": [
        {"observation_id": "winner", "symbol": "BTC/USDT", "strategy": "TrendBreakout",
         "timestamp": index[0], "rank": 1, "score": 1.2, "order_id": "entry-1", "stop_loss": 90,
         "sized_qty": 2, "clamped_qty": 2, "reason": "order_accepted"},
        {"observation_id": "rejected", "symbol": "ETH/USDT", "strategy": "TrendBreakout",
         "timestamp": index[0], "rank": 2, "score": 0.4, "sized_qty": 2, "clamped_qty": 0,
         "reason": "correlated_budget"},
    ], "trades": [
        {"order_id": "entry-1", "symbol": "BTC/USDT", "side": "buy", "qty": 2,
         "fill_price": 100, "theoretical_price": 100, "commission": 0,
         "fill_time": index[1], "strategy_id": "TrendBreakout"},
        {"order_id": "stop-1", "symbol": "BTC/USDT", "side": "sell", "qty": 2,
         "fill_price": 110, "theoretical_price": 111, "commission": 2,
         "fill_time": index[3], "exit_reason": "protective_stop", "close_event_ids": ["c1"]},
        {"order_id": "entry-2", "symbol": "BTC/USDT", "side": "buy", "qty": 1,
         "fill_price": 115, "theoretical_price": 114, "commission": 1,
         "fill_time": index[4], "strategy_id": "TrendBreakout"},
    ]}
    summary = write_review_diagnostics(tmp_path, {"BTC/USDT": frame}, result, {})
    close = pd.read_csv(tmp_path / "review_exit_quality.csv").iloc[0]
    assert close.mfe_per_unit == 30
    assert close.peak_gross_profit == 60
    assert close.gross_profit_giveback == 40 and close.net_profit_giveback == 42
    assert close.observed_trend_capture == pytest.approx(1 / 3)
    assert close.initial_stop == 90 and close.initial_risk == 20
    reentry = pd.read_csv(tmp_path / "review_reentry_costs.csv").iloc[0]
    assert reentry.stop_exit_execution_cost == 4 and reentry.next_entry_execution_cost == 2
    assert reentry.exit_and_reentry_execution_cost == 6
    candidates = pd.read_csv(tmp_path / "review_candidate_allocation.csv")
    assert candidates.iloc[0].observed_closed_net_pnl == 18
    assert pd.isna(candidates.iloc[1].observed_closed_net_pnl)
    assert candidates.iloc[1].budget_reduced_observed
    assert summary["competing_batch_count"] == 1


def test_same_bar_stop_does_not_use_high_reached_after_stop(tmp_path):
    frame = _frame(2)
    frame.high = [101, 900]
    result = {"review_period": {"start": frame.index[0], "end": frame.index[-1]},
              "trades": [
                  {"symbol": "BTC/USDT", "side": "buy", "qty": 1, "fill_price": 100,
                   "fill_time": frame.index[1], "order_id": "entry"},
                  {"symbol": "BTC/USDT", "side": "sell", "qty": 1, "fill_price": 90,
                   "fill_time": frame.index[1], "order_id": "stop"},
              ]}
    write_review_diagnostics(tmp_path, {"BTC/USDT": frame}, result, {})
    row = pd.read_csv(tmp_path / "review_exit_quality.csv").iloc[0]
    assert row.mfe_per_unit == 0
    assert pd.isna(row.realized_net_pnl)
    assert row.evidence_status == "close_event_facts_missing"


def test_no_period_does_not_treat_full_history_as_run_or_missing_profit_as_zero(tmp_path):
    summary = write_review_diagnostics(tmp_path, {"BTC/USDT": _frame()}, {}, {})
    assert summary["status"] == "insufficient_period_or_market_evidence"
    assert summary["symbol_count"] == 0 and summary["gate_fact_coverage"] == "unknown_no_entry_observations"
    assert pd.read_csv(tmp_path / "review_setup_timing.csv").empty


def test_missing_observations_and_untimestamped_allocator_rows_not_falsely_linked(tmp_path):
    frame = _frame(22)
    frame.loc[frame.index[-1], "close"] = 120
    _summary(tmp_path, frame.index[-2], frame.index[-1])
    result = {"allocation_audit": [{"symbol": "BTC/USDT", "score": 1, "reason": "accepted"}]}
    summary = write_review_diagnostics(tmp_path, {"BTC/USDT": frame}, result, {})
    row = pd.read_csv(tmp_path / "review_setup_timing.csv").iloc[0]
    assert row.outcome == "unobserved_gate_facts"
    assert pd.isna(row.order_id) and pd.isna(row.counterfactual_pnl)
    assert summary["allocation_audit_count_unlinked"] == 1
    assert summary["candidate_count"] == 0
