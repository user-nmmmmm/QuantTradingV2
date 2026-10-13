"""B4 (ADR 0018): the four host classes hold collaborators instead of inheriting mixins."""
from __future__ import annotations

import copy

import pytest

from core.broker import Broker
from core.live_broker import LiveBroker
from core.risk import RiskManager
from live_trading.engine import LiveTradingEngine

HOSTS = {
    Broker: ("_matching", "_fill_service", "_financing", "_liquidation"),
    LiveBroker: ("_submission", "_reconciler", "_account_sync"),
    RiskManager: ("_circuit_breaker", "_position_sizing", "_entry_policy"),
    LiveTradingEngine: ("_tick_orchestrator", "_risk_actions", "_recovery", "_state_export"),
}


@pytest.mark.parametrize("host_class", HOSTS)
def test_hosts_do_not_inherit_mixins(host_class):
    names = [base.__name__ for base in host_class.__mro__]
    assert not [name for name in names if name.endswith("Mixin")], names


@pytest.mark.parametrize("host_class, attributes", HOSTS.items())
def test_collaborators_are_created_lazily_and_point_back_to_the_host(host_class, attributes):
    host = object.__new__(host_class)          # __init__ is skipped on purpose (several tests build hosts this way)
    for attribute in attributes:
        assert attribute not in vars(host)
        collaborator = getattr(host, attribute)
        assert collaborator._host is host
        assert getattr(host, attribute) is collaborator      # cached


def test_deepcopy_redirects_the_collaborators_to_the_copy():
    manager = RiskManager()
    original_breaker = manager._circuit_breaker
    clone = copy.deepcopy(manager)
    assert clone._circuit_breaker is not original_breaker
    assert clone._circuit_breaker._host is clone
    assert original_breaker._host is manager


def test_forwarding_methods_keep_the_public_signature():
    import inspect
    parameters = inspect.signature(RiskManager.check_entry_risk).parameters
    assert list(parameters)[:5] == ["self", "portfolio", "symbol", "qty", "price"]
    assert parameters["action"].default == "buy"
    assert isinstance(inspect.getattr_static(RiskManager, "breaker_action"), property)
    assert isinstance(inspect.getattr_static(Broker, "_event_time"), staticmethod)
    assert isinstance(inspect.getattr_static(Broker, "_iso_time"), classmethod)
