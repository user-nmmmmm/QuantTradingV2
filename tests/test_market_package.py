"""B2 (market cluster): modules live in core/market/ and path-relative code still finds the repository."""
from __future__ import annotations

from pathlib import Path

from core.market import request_budget

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "data", "data_fetcher", "market_data", "market_data_errors", "market_read_batch", "websocket_market",
    "derivatives_data", "public_data_clients", "quote_observations", "quote_borrow", "binance_depth_observer",
    "temporal_data", "data_versions", "trend_portfolio_data", "universe", "timeframes", "request_budget",
)


def test_modules_live_in_core_market_and_not_at_the_core_root():
    for name in MODULES:
        assert (ROOT / "core" / "market" / f"{name}.py").is_file(), name
        assert not (ROOT / "core" / f"{name}.py").exists(), f"core/{name}.py should have moved"


def test_deployment_budget_default_path_is_still_under_the_repository_reports_dir(monkeypatch):
    seen = []
    monkeypatch.delenv("QUANT_REQUEST_BUDGET_DB", raising=False)
    monkeypatch.setattr(request_budget, "SharedRequestBudget", lambda path: seen.append(Path(path)))
    request_budget.deployment_budget()
    assert seen == [ROOT / "reports" / "request_budget.sqlite3"]
