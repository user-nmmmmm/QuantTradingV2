"""B2 (trading cluster): modules live in core/trading/."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('orders', 'order_lifecycle', 'order_store', 'order_latency', 'execution_port', 'protective_orders', 'protective_stops', 'lots', 'accounts', 'account_source', 'account_reconciliation', 'account_cost_contract', 'margin_rebalance', 'position_management', 'entry_audit', 'entry_risk', 'state', 'state_store_v2', 'sqlite_utils', 'sqlite_backup', 'portfolio', 'valuation')


def test_modules_live_in_core_trading_and_not_at_the_core_root():
    for name in MODULES:
        assert (ROOT / "core" / "trading" / f"{name}.py").is_file(), name
        assert not (ROOT / "core" / f"{name}.py").exists(), f"core/{name}.py should have moved"
