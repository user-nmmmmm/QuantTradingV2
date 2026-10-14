"""A read/write attribute that lives on a collaborator while the host keeps its old name (B6, ADR 0027).

``RiskManager`` used to hold the circuit breaker's state itself. The state now belongs to ``CircuitBreaker``, but
state export, recovery, the engines and the tests still read and write ``risk.high_water_equity`` and friends.
``Delegated`` forwards those names to the collaborator, so no caller changes and the collaborator is the single
place where the value is stored.
"""
from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar, overload

T = TypeVar("T")


class Delegated(Generic[T]):
    """``host.<name>`` reads and writes ``getattr(host, holder).<name>``; ``holder`` is a lazily created attribute."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        self.name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    @overload
    def __get__(self, obj: None, owner: Optional[type] = None) -> "Delegated[T]": ...

    @overload
    def __get__(self, obj: object, owner: Optional[type] = None) -> T: ...

    def __get__(self, obj: Optional[object], owner: Optional[type] = None) -> Any:
        if obj is None:
            return self
        return getattr(getattr(obj, self.holder), self.name)

    def __set__(self, obj: object, value: T) -> None:
        setattr(getattr(obj, self.holder), self.name, value)
