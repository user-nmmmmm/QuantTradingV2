"""B2 (analytics cluster): modules live in core/analytics/."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('accounting_check', 'backtest_audit', 'benchmarks', 'diagnostics', 'metric_result', 'strategy_health', 'indicators')


def test_modules_live_in_core_analytics_and_not_at_the_core_root():
    for name in MODULES:
        assert (ROOT / "core" / "analytics" / f"{name}.py").is_file(), name
        assert not (ROOT / "core" / f"{name}.py").exists(), f"core/{name}.py should have moved"
