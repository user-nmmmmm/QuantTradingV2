"""Admission and independent ranking/sizing checks; no model training."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import main as entrypoint
from config.config import config
from core.allocation import EntryCandidate, PortfolioSignalAllocator
from core.position_management import CapitalAllocationPolicy, SmartCapitalPlanner
from dashboard.backtest_jobs import BacktestJobs


def test_cli_refuses_v3_selector_before_bundle_or_data(monkeypatch):
    monkeypatch.setitem(config._config, "portfolio_targets", {"enabled": True})
    load = MagicMock(side_effect=AssertionError("unsupported path must fail at admission"))
    monkeypatch.setattr(entrypoint, "_load_requested_data", load)
    monkeypatch.setattr("backtest.coin_selector.read_bundle", load)
    assert entrypoint.main(["--coin-selector", "on"]) == 2
    load.assert_not_called()


def test_direct_backtest_refuses_v3_before_constructing_selector(monkeypatch):
    monkeypatch.setitem(config._config, "portfolio_targets", {"enabled": True})
    load = MagicMock(side_effect=AssertionError("unsupported path must fail at admission"))
    monkeypatch.setattr("backtest.coin_selector.create_selector", load)
    args = entrypoint._build_parser().parse_args(["--coin-selector", "on"])
    with pytest.raises(ValueError, match="portfolio target controller"):
        entrypoint._execute_backtest(args, {})
    load.assert_not_called()


@pytest.mark.parametrize("market_type,expected", [("spot", "spot"), ("margin", "spot_margin"),
                                                  ("perpetual", "perpetual"), (None, "spot_margin")])
def test_selector_deployment_uses_effective_engine_account(monkeypatch, market_type, expected):
    monkeypatch.setitem(config._config, "account", {"mode": "spot_margin"})
    args = SimpleNamespace(coin_selector="off", market_type=market_type)
    assert entrypoint._validate_selector_execution_options(args) == expected


def test_dashboard_rejects_v3_selector_before_freezing_or_queuing(monkeypatch, tmp_path):
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    (root / "config" / "params.yaml").write_text(
        "portfolio_targets: {enabled: true}\naccount: {mode: spot}\n", encoding="utf-8")
    monkeypatch.setattr("dashboard.backtest_jobs.PROJECT_ROOT", root)
    manager = BacktestJobs(tmp_path / "data", tmp_path / "reports")
    monkeypatch.setattr(manager, "_required_symbols", lambda: 1)
    freeze = MagicMock(side_effect=AssertionError("must reject before model snapshot"))
    monkeypatch.setattr(manager, "_freeze_selector", freeze)
    try:
        with pytest.raises(ValueError, match="portfolio target controller"):
            manager.submit({"source": "synthetic", "symbols": ["BTC/USDT"],
                            "start": "2025-01-01", "end": "2025-07-01", "capital": 10000,
                            "slippage_bps": 5, "seed": 42, "use_selector": True})
        freeze.assert_not_called()
        assert manager.list() == []
        assert not (manager.reports_dir / ".dashboard" / "configs").exists()
    finally:
        manager.close()


def _candidate(symbol, score, submitted):
    def submit(candidate, **kwargs):
        submitted.append((candidate.symbol, candidate.score, candidate.ranking_score))
        return SimpleNamespace(accepted=True)
    strategy = SimpleNamespace(name="fixture", submit_entry_candidate=submit)
    frame = pd.DataFrame({"close": [100.]}, index=pd.date_range("2025-01-01", periods=1))
    return EntryCandidate(symbol, strategy, 0, frame, "TREND_UP", {"action": "buy"}, score)


def test_ml_ranking_override_does_not_replace_capital_score():
    submitted = []
    original = [_candidate("A", .9, submitted), _candidate("B", .1, submitted)]
    changed = [replace(original[0], selection_rank_score=.1),
               replace(original[1], selection_rank_score=.9)]
    seen_by_capital = []
    allocator = PortfolioSignalAllocator(capital_policy=CapitalAllocationPolicy(enabled=True))
    def plan(candidates, **kwargs):
        seen_by_capital.extend((item.symbol, item.score) for item in candidates)
        return {item.symbol: {"approved_qty": 1.} for item in candidates}, {"status": "complete"}
    allocator.capital_planner.plan = plan
    allocator.allocate(changed, portfolio=object(), broker=object(), risk_manager=object(),
                       current_prices={})
    assert seen_by_capital == [("B", .1), ("A", .9)]
    assert submitted == [("B", .1, .9), ("A", .9, .1)]
    assert [item.score for item in allocator.audit] == [.1, .9]
    assert [item.symbol for item in PortfolioSignalAllocator.rank(original)] == ["A", "B"]


def _smart_inputs():
    import numpy as np
    frame = pd.DataFrame({"close": 100. + np.sin(np.arange(31))},
                         index=pd.date_range("2025-01-01", periods=31, tz="UTC"))
    strategy = SimpleNamespace(name="fixture", health_risk_multiplier=lambda: 1.,
        entry_risk_multiplier=lambda state: 1., initial_entry_quantity=lambda **kwargs: 1000.)
    candidates = [EntryCandidate(symbol, strategy, 30, frame, "TREND_UP",
                                {"action": "buy", "stop_loss": 80.}, score)
                  for symbol, score in (("A", 4.), ("B", .1))]
    projection = SimpleNamespace(pending_notional=lambda prices: {}, pending_stop_risk=lambda: {},
                                 pending_cash=lambda prices, **kwargs: 0.)
    arguments = {"portfolio": SimpleNamespace(positions={}, cash=10000., get_equity=lambda prices: 10000.),
                 "broker": SimpleNamespace(reservation_projection=projection),
                 "risk_manager": SimpleNamespace(max_leverage=1., max_entry_notional=lambda *args, **kwargs: 10000.,
                                                   minimum_entry_notional=lambda *args, **kwargs: 1.),
                 "risk_governor": SimpleNamespace(policy=None),
                 "current_prices": {symbol: float(frame.close.iloc[-1]) for symbol in ("A", "B")}}
    return candidates, arguments


def _planner(max_positions):
    return SmartCapitalPlanner(CapitalAllocationPolicy(enabled=True, max_positions=max_positions,
                                                       max_position_pct=.9, cost_buffer_bps=0.))


def test_smart_slots_follow_ml_ranking_with_native_weights():
    original, arguments = _smart_inputs()
    ranked = [replace(original[0], selection_rank_score=.1),
              replace(original[1], selection_rank_score=.9)]
    baseline, _ = _planner(1).plan(original, **arguments)
    treatment, _ = _planner(1).plan(ranked, **arguments)
    assert baseline["A"]["approved_qty"] > 0 and baseline["B"]["reason"] == "position_slots_exhausted"
    assert treatment["B"]["approved_qty"] > 0 and treatment["A"]["reason"] == "position_slots_exhausted"
    assert treatment["B"]["capital_score"] == .1
    baseline, _ = _planner(2).plan(original, **arguments)
    treatment, _ = _planner(2).plan(ranked, **arguments)
    assert baseline["A"]["weight"] > baseline["B"]["weight"]
    for symbol in ("A", "B"):
        assert treatment[symbol]["approved_notional"] == pytest.approx(baseline[symbol]["approved_notional"])
        assert treatment[symbol]["weight"] == pytest.approx(baseline[symbol]["weight"])


def test_smart_planner_rejects_mixed_ranking_contract():
    original, arguments = _smart_inputs()
    mixed = [replace(original[0], selection_rank_score=.1), original[1]]
    decisions, summary = _planner(2).plan(mixed, **arguments)
    assert summary["reason"] == "mixed_selection_ranking_contract"
    assert all(item["approved_qty"] == 0 for item in decisions.values())


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True])
def test_smart_planner_cannot_use_invalid_ml_ranking(invalid):
    original, arguments = _smart_inputs()
    decisions, _ = _planner(2).plan([replace(original[0], selection_rank_score=invalid)], **arguments)
    assert decisions["A"]["reason"] == "invalid_selection_rank_score"
    assert decisions["A"]["approved_qty"] == 0
