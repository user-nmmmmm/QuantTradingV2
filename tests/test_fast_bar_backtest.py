"""The faster historical bar path must preserve trading and audit facts."""

from dataclasses import fields

from backtest.engine import BacktestEngine
from scripts.benchmark_backtest import correctness_artifacts
from tests.engine_baseline_harness import build_synthetic_data_map, compare_artifacts


def _event_facts(events):
    return [
        tuple(
            (item.name, getattr(event, item.name))
            for item in fields(event)
            if item.name != "observed_at"
        )
        for event in events
    ]


def test_fast_bars_preserve_backtest_and_event_facts():
    data = build_synthetic_data_map(
        seed=20260812,
        symbols=[f"ASSET{i}/USDT" for i in range(10)],
        bars=240,
    )
    results = {}
    for fast_bars in (False, True):
        engine = BacktestEngine(
            run_id="fast-bar-parity",
            random_slip=False,
            fast_bars=fast_bars,
        )
        results[fast_bars] = engine.run(data, routing_log_enabled=False)

    baseline = results[False]
    optimized = results[True]
    assert compare_artifacts(
        correctness_artifacts(optimized), correctness_artifacts(baseline)
    ) == []
    for key in (
        "margin_ledger", "financing_ledger", "execution_audit", "breaker_audit",
        "risk_budget_reconciliation", "lifecycle", "valuation_quality",
    ):
        assert compare_artifacts(optimized[key], baseline[key], key) == []
    assert _event_facts(optimized["event_log"]) == _event_facts(baseline["event_log"])
