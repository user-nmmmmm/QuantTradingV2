import pytest

from scripts.run_strategy_branch_example import run_example


def test_selection_volatility_execution_uses_real_partial_fills_and_reconciles():
    result = run_example()
    assert result["selection"]["targets"]["AAA-USDT"] == .3
    assert result["selection"]["targets"]["BBB-USDT"] == 0.
    assert 0 < result["volatility_sizing"]["AAA-USDT"]["weight"] < .3
    assert len(result["orders"]) == 1
    assert result["orders"][0]["filled_qty"] == 2.
    assert result["orders"][0]["qty"] > 2.
    assert [fill["qty"] for fill in result["fills"]] == [1., 1.]
    assert result["cash"] == pytest.approx(9799.8)
    assert result["reconciliation"] == {"cash_error": 0., "position_error": 0.}
    assert result["research_status"] == "not_evaluated"
    assert result["formal_routing_enabled"] is False
