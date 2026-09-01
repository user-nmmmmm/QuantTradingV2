"""Bounded retry policy for exchange operations."""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from core.logger import get_logger
from core.orders import classify_order_exception, is_ambiguous_error


logger = get_logger(__name__)
T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retryable: Callable[[Any], bool] = is_ambiguous_error,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` with bounded exponential backoff for ambiguous failures."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if base_delay < 0 or max_delay < 0:
        raise ValueError("retry delays cannot be negative")

    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            code = classify_order_exception(exc)
            final_attempt = attempt == max_attempts - 1
            if final_attempt or not retryable(code):
                raise
            delay = min(base_delay * (2 ** attempt), max_delay)
            logger.warning(
                "Retrying exchange operation attempt=%s/%s category=%s delay=%.3fs",
                attempt + 2,
                max_attempts,
                code.value,
                delay,
            )
            sleep_fn(delay)

    raise RuntimeError("retry loop exited unexpectedly")
