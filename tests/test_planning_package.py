"""B2 (planning cluster): modules live in core/planning/."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('selection', 'selection_v2', 'candidate_scoring', 'allocation', 'cost_aware_allocation', 'target_position', 'portfolio_target_controller')


def test_modules_live_in_core_planning_and_not_at_the_core_root():
    for name in MODULES:
        assert (ROOT / "core" / "planning" / f"{name}.py").is_file(), name
        assert not (ROOT / "core" / f"{name}.py").exists(), f"core/{name}.py should have moved"
