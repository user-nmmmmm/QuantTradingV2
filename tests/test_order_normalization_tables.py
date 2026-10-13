"""Exchange status normalization and order-error classification, as tables (C5).

These two functions decide whether an uncertain submission is treated as rejected (safe to retry) or as unknown
(must be reconciled first), so the tables below state the intended outcome of each case explicitly.
"""
from __future__ import annotations

import pytest

from core.foundation.domain import OrderErrorCode as Code, OrderStatus as Status
from core.trading.orders import (
    TERMINAL_STATUSES, classify_order_exception, is_ambiguous_error, normalize_exchange_status, validate_transition,
)


@pytest.mark.parametrize("payload, expected", [
    ({"status": "closed"}, Status.FILLED),
    ({"status": "FILLED"}, Status.FILLED),
    ({"status": "open", "amount": 2, "filled": 2}, Status.FILLED),            # fully filled even if the status lags
    ({"status": "expired", "amount": 2, "filled": 1}, Status.EXPIRED),        # expiry is terminal despite a partial fill
    ({"status": "canceled"}, Status.CANCELED),
    ({"status": "Cancelled"}, Status.CANCELED),
    ({"status": "rejected"}, Status.REJECTED),
    ({"status": "failed"}, Status.REJECTED),
    ({"status": "open", "amount": 2, "filled": 0.5}, Status.PARTIALLY_FILLED),
    ({"status": "canceled_pending_fill_unknown", "amount": 2, "filled": 0.5}, Status.PARTIALLY_FILLED),
    ({"status": "open"}, Status.ACCEPTED),
    ({"status": "NEW"}, Status.ACCEPTED),
    ({"status": "pending"}, Status.ACCEPTED),
    ({"status": "", "amount": 3, "remaining": 0}, Status.FILLED),             # nothing remaining
    ({"status": None, "amount": 0, "remaining": 0}, Status.UNKNOWN),          # no size: cannot conclude it filled
    ({}, Status.UNKNOWN),
    ({"status": "weird"}, Status.UNKNOWN),
    ({"status": "open", "amount": "oops", "filled": "oops"}, Status.ACCEPTED),  # unparseable numbers count as zero
])
def test_exchange_status_normalization(payload, expected):
    assert normalize_exchange_status(payload) is expected


@pytest.mark.parametrize("exc, expected", [
    (TimeoutError("read timed out"), Code.TIMEOUT),
    (RuntimeError("request Timeout"), Code.TIMEOUT),
    (RuntimeError("429 Too Many Requests"), Code.RATE_LIMIT),
    (type("RateLimitExceeded", (Exception,), {})("slow down"), Code.RATE_LIMIT),
    (type("AuthenticationError", (Exception,), {})("nope"), Code.AUTHENTICATION),
    (RuntimeError("Signature for this request is not valid"), Code.AUTHENTICATION),
    (RuntimeError("Account has insufficient balance"), Code.INSUFFICIENT_FUNDS),
    (type("InsufficientFunds", (Exception,), {})(""), Code.INSUFFICIENT_FUNDS),
    (ConnectionError("Connection reset by peer"), Code.NETWORK),
    (type("NetworkError", (Exception,), {})("x"), Code.NETWORK),
    (RuntimeError("exchange under maintenance"), Code.EXCHANGE_UNAVAILABLE),
    (type("ExchangeNotAvailableUnavailable", (Exception,), {})("x"), Code.EXCHANGE_UNAVAILABLE),
    (type("InvalidOrder", (Exception,), {})("x"), Code.TRADING_RULE),
    (RuntimeError("Order below minimum notional"), Code.TRADING_RULE),
    (RuntimeError("Precision is over the maximum defined"), Code.TRADING_RULE),
    (RuntimeError("something else entirely"), Code.UNKNOWN),
])
def test_order_exception_classification(exc, expected):
    assert classify_order_exception(exc) is expected


def test_only_errors_that_may_have_reached_the_exchange_are_ambiguous():
    ambiguous = {code for code in Code if is_ambiguous_error(code)}
    assert ambiguous == {Code.NETWORK, Code.TIMEOUT, Code.RATE_LIMIT, Code.EXCHANGE_UNAVAILABLE, Code.UNKNOWN}
    for definite in (Code.AUTHENTICATION, Code.INSUFFICIENT_FUNDS, Code.TRADING_RULE):
        assert not is_ambiguous_error(definite)


def test_transitions_allow_late_exchange_truth_but_not_resurrection():
    validate_transition(Status.ACCEPTED.value, Status.ACCEPTED)       # same state is a no-op
    validate_transition(Status.CANCELED.value, Status.FILLED)                  # a fill can race with the cancel ack
    validate_transition(Status.EXPIRED_UNSUBMITTED.value, Status.PARTIALLY_FILLED)
    validate_transition(Status.UNKNOWN.value, Status.CANCELED)
    for source, target in ((Status.FILLED, Status.ACCEPTED), (Status.REJECTED, Status.FILLED), (Status.CANCELED, Status.ACCEPTED)):
        with pytest.raises(ValueError, match="Invalid order transition"):
            validate_transition(source.value, target)
    assert Status.FILLED in TERMINAL_STATUSES and Status.UNKNOWN not in TERMINAL_STATUSES
