"""Each reason the final entry gate can refuse a trade, and what it passes through to the notional caps (C5)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.risk import entry_policy
from core.risk.entry_policy import EntryPolicy

BIG = 1e12


class _Host:
    def __init__(self, caps, *, blocked=False, healthy=True):
        self.caps, self.blocked, self.healthy = caps, blocked, healthy
        self.liquidity_limit_pct = 0.01
        self.max_leverage, self.max_pos_size_pct = 2.0, 0.25
        self._last_entry_context = {"equity": 1000.0, "reserved": 0.0, "exposure": 100.0, "position_value": 50.0}
        self.cap_calls = []

    def _blocks_new_risk(self):
        return self.blocked

    def _health_allows_new_risk(self):
        return self.healthy

    def _entry_notional_caps(self, portfolio, symbol, price, prices, reserved, action, projection):
        self.cap_calls.append((symbol, price, prices, reserved, action, projection))
        return self.caps


def _caps(**overrides):
    return {"leverage": BIG, "concentration": BIG, **overrides}


@pytest.fixture()
def notes(monkeypatch):
    seen = []
    monkeypatch.setattr(entry_policy, "note", seen.append)
    return seen


def _check(host, qty=1.0, price=10.0, **kwargs):
    return EntryPolicy(host).check_entry_risk(SimpleNamespace(cash=500.0), "BTC/USDT", qty, price, **kwargs)


def test_a_clean_trade_is_allowed_and_records_nothing(notes):
    assert _check(_Host(_caps())) is True and notes == []


@pytest.mark.parametrize("host, kwargs, reason", [
    (_Host(_caps(), blocked=True), {}, "portfolio_block"),
    (_Host(_caps(), healthy=False), {}, "account_health_block"),
    (_Host(_caps()), {"qty": 0.0}, "invalid_order_size_or_price"),
    (_Host(_caps()), {"qty": -1.0}, "invalid_order_size_or_price"),
    (_Host(_caps()), {"price": 0.0}, "invalid_order_size_or_price"),
    (_Host(None), {}, "unverifiable_exposure"),
    (_Host(_caps(cash=5.0)), {"qty": 1.0, "price": 10.0}, "cash_limit"),
    (_Host(_caps(account_mode=5.0)), {"qty": 1.0, "price": 10.0}, "account_mode"),
    (_Host(_caps(initial_margin=5.0)), {"qty": 1.0, "price": 10.0}, "initial_margin"),
    (_Host(_caps(leverage=5.0)), {"qty": 1.0, "price": 10.0}, "leverage_limit"),
    (_Host(_caps(concentration=5.0)), {"qty": 1.0, "price": 10.0}, "concentration_limit"),
    (_Host(_caps(cluster_exposure=5.0)), {"qty": 1.0, "price": 10.0}, "cluster_exposure"),
    (_Host(_caps(crypto_beta_exposure=5.0)), {"qty": 1.0, "price": 10.0}, "crypto_beta_exposure"),
])
def test_each_rejection_reports_its_reason(notes, host, kwargs, reason):
    assert _check(host, **kwargs) is False
    assert notes == [reason]


def test_liquidity_cap_applies_only_when_volume_is_known(notes):
    assert _check(_Host(_caps()), qty=2.0, current_volume=100.0) is False      # 1% of 100 is 1
    assert notes == ["liquidity_limit"]
    assert _check(_Host(_caps()), qty=2.0, current_volume=0.0) is True         # unknown volume is not a veto
    assert _check(_Host(_caps()), qty=1.0, current_volume=100.0) is True       # exactly at the limit


def test_a_trade_exactly_at_a_cap_is_allowed(notes):
    assert _check(_Host(_caps(cash=10.0, leverage=10.0, concentration=10.0)), qty=1.0, price=10.0) is True


def test_reserved_notional_comes_from_the_projection_when_there_is_one(notes):
    projection = SimpleNamespace(pending_notional=lambda prices: {"ETH/USDT": prices["ETH/USDT"] * 2})
    host = _Host(_caps())
    assert _check(host, current_prices={"ETH/USDT": 7.0}, reservation_projection=projection,
                  pending_open_notional={"ignored": 1.0}, action="short") is True
    symbol, price, prices, reserved, action, passed = host.cap_calls[0]
    assert reserved == {"ETH/USDT": 14.0} and action == "short" and passed is projection and price == 10.0

    fallback = _Host(_caps())
    _check(fallback, pending_open_notional={"SOL/USDT": 3.0})
    assert fallback.cap_calls[0][3] == {"SOL/USDT": 3.0}
    none_given = _Host(_caps())
    _check(none_given)
    assert none_given.cap_calls[0][3] == {}
