from types import SimpleNamespace

import pytest

from core.risk.portfolio_governor import CorrelationClusterPolicy, PortfolioRiskGovernor


def book(risk):
    return SimpleNamespace(open_lots=[SimpleNamespace(
        initial_risk=risk, qty_open=1., qty_original=1., entry_price=1000., stop_price=1000.-risk)])


def test_crypto_parent_budget_includes_major_altcoins_and_pending_orders():
    policy = CorrelationClusterPolicy(
        clusters={"BTC": "major", "ETH": "major"}, default_cluster="crypto_beta",
        max_correlated_stop_risk=.03, max_crypto_beta_stop_risk=.03,
        max_same_session_entry_risk=.02,
    )
    portfolio = SimpleNamespace(lot_books={"BTC-USDT": book(150.), "SOL-USDT": book(100.)})
    governor = PortfolioRiskGovernor(policy)
    governor.begin_session("2026-09-20")
    result = governor.evaluate(symbol="ETH-USDT", planned_risk=100., equity=10000.,
                               portfolio=portfolio, pending_stop_risk={"ADA-USDT": 30.})
    assert result.allowed_risk == pytest.approx(20.)
    assert result.reason == "crypto_beta_stop_risk_scaled"
    governor.commit(result, symbol="ETH-USDT")
    blocked = governor.evaluate(symbol="XRP-USDT", planned_risk=100., equity=10000.,
                                portfolio=portfolio,
                                pending_stop_risk={"ADA-USDT": 30., "ETH-USDT": 20.})
    assert not blocked.allowed


def test_parent_budget_fallback_reserves_same_session_and_default_is_unchanged():
    portfolio = SimpleNamespace(lot_books={"BTC-USDT": book(250.)})
    governor = PortfolioRiskGovernor(CorrelationClusterPolicy(max_crypto_beta_stop_risk=.03))
    governor.begin_session("day")
    first = governor.evaluate(symbol="SOL-USDT", planned_risk=40., equity=10000., portfolio=portfolio)
    governor.commit(first, symbol="SOL-USDT")
    second = governor.evaluate(symbol="ETH-USDT", planned_risk=40., equity=10000., portfolio=portfolio)
    assert second.allowed_risk == pytest.approx(10.)
    assert not CorrelationClusterPolicy().has_risk_caps


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_parent_budget_rejected(value):
    with pytest.raises(ValueError):
        CorrelationClusterPolicy(max_crypto_beta_stop_risk=value)


def test_gap_and_partial_close_do_not_undercount_filled_parent_stop_risk():
    lot = SimpleNamespace(initial_risk=100., qty_original=10., qty_open=20 / 3,
                          entry_price=105., stop_price=90.)
    portfolio = SimpleNamespace(lot_books={"BTC-USDT": SimpleNamespace(open_lots=[lot])})
    governor = PortfolioRiskGovernor(CorrelationClusterPolicy(max_crypto_beta_stop_risk=.03))
    result = governor.evaluate(symbol="ETH-USDT", planned_risk=250., equity=10000.,
                               portfolio=portfolio, pending_stop_risk={})
    assert result.allowed_risk == pytest.approx(200.)
    lot.stop_price = None
    assert not governor.evaluate(symbol="ETH-USDT", planned_risk=1., equity=10000.,
                                  portfolio=portfolio, pending_stop_risk={}).allowed


@pytest.mark.parametrize("risk", [float("nan"), float("inf"), -1.])
def test_parent_budget_fails_closed_on_invalid_pending_risk(risk):
    governor = PortfolioRiskGovernor(CorrelationClusterPolicy(max_crypto_beta_stop_risk=.03))
    assert not governor.evaluate(symbol="BTC-USDT", planned_risk=1., equity=10000.,
        portfolio=SimpleNamespace(lot_books={}), pending_stop_risk={"ETH-USDT": risk}).allowed
