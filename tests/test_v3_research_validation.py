import pytest

from analysis.trend_portfolio_v3_validation import health_reachability, performance_summary


def test_drawdown_uses_initial_capital_and_cost_net_equity():
    result = performance_summary([9800, 11000, 9000], 10000)
    assert result["return_pct"] == pytest.approx(-10)
    assert result["max_drawdown_pct"] == pytest.approx(100 * 2000 / 11000)


def test_double_coin_health_contract_is_explicit_not_silently_changed():
    settings = {"strategy_health": {"probation_min_distinct_symbols": 3}}
    assert health_reachability(settings, ["BTC-USDT", "ETH-USDT"])["status"] == "unreachable"
    assert settings["strategy_health"]["probation_min_distinct_symbols"] == 3
    settings["strategy_health"]["probation_min_distinct_symbols"] = 2
    assert health_reachability(settings, ["BTC-USDT", "ETH-USDT"])["status"] == "pass"
