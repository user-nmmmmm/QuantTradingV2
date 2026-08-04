"""Fail-closed startup and order controls for exchange-connected trading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Optional


class SafetyConfigurationError(ValueError):
    """Raised when exchange trading cannot be started safely."""


def _csv(value: Optional[str]) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def _positive_float(value: Optional[str], name: str) -> float:
    if value in (None, ""):
        raise SafetyConfigurationError(f"{name} must be explicitly configured")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SafetyConfigurationError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise SafetyConfigurationError(f"{name} must be greater than zero")
    return parsed


def credentials_from_environment() -> Mapping[str, str]:
    """Load credentials without accepting secrets on the command line."""
    return {
        "apiKey": os.getenv("EXCHANGE_API_KEY", ""),
        "secret": os.getenv("EXCHANGE_SECRET", ""),
        "password": os.getenv("EXCHANGE_PASSWORD", ""),
    }


@dataclass(frozen=True)
class StartupSafetyPolicy:
    sandbox: bool
    exchange_id: str
    account_type: str
    symbols: tuple[str, ...]
    allowed_exchanges: tuple[str, ...]
    allowed_account_types: tuple[str, ...]
    allowed_symbols: tuple[str, ...]
    base_currency: str
    max_order_notional: float
    max_daily_new_risk: float
    kill_switch_env: str = "QUANT_KILL_SWITCH"

    @classmethod
    def from_environment(
        cls,
        *,
        sandbox: bool,
        exchange_id: str,
        account_type: str,
        symbols: Iterable[str],
        base_currency: str,
    ) -> "StartupSafetyPolicy":
        prefix = "QUANT_SANDBOX" if sandbox else "QUANT_LIVE"
        allowed_exchanges = _csv(os.getenv("QUANT_ALLOWED_EXCHANGES", "binance"))
        allowed_account_types = _csv(os.getenv("QUANT_ALLOWED_ACCOUNT_TYPES", "spot"))
        allowed_symbols = _csv(os.getenv("QUANT_ALLOWED_SYMBOLS", "BTC/USDT,ETH/USDT"))

        order_default = "1000" if sandbox else None
        daily_default = "5000" if sandbox else None
        max_order = _positive_float(
            os.getenv(f"{prefix}_MAX_ORDER_NOTIONAL", order_default),
            f"{prefix}_MAX_ORDER_NOTIONAL",
        )
        max_daily = _positive_float(
            os.getenv(f"{prefix}_MAX_DAILY_NEW_RISK", daily_default),
            f"{prefix}_MAX_DAILY_NEW_RISK",
        )
        policy = cls(
            sandbox=sandbox,
            exchange_id=exchange_id.lower(),
            account_type=account_type.lower(),
            symbols=tuple(symbols),
            allowed_exchanges=tuple(item.lower() for item in allowed_exchanges),
            allowed_account_types=tuple(item.lower() for item in allowed_account_types),
            allowed_symbols=allowed_symbols,
            base_currency=base_currency.upper(),
            max_order_notional=max_order,
            max_daily_new_risk=max_daily,
        )
        policy.validate()
        return policy

    def validate(self) -> None:
        if not self.allowed_exchanges or self.exchange_id not in self.allowed_exchanges:
            raise SafetyConfigurationError("exchange is not allowlisted")
        if not self.allowed_account_types or self.account_type not in self.allowed_account_types:
            raise SafetyConfigurationError("account type is not allowlisted")
        if not self.allowed_symbols:
            raise SafetyConfigurationError("symbol allowlist must not be empty")
        rejected = sorted(set(self.symbols) - set(self.allowed_symbols))
        if rejected:
            raise SafetyConfigurationError(f"symbols are not allowlisted: {', '.join(rejected)}")
        for symbol in self.symbols:
            quote = symbol.rsplit("/", 1)[-1].split(":", 1)[0].upper()
            if quote != self.base_currency:
                raise SafetyConfigurationError("symbol quote currency does not match base currency")

    def kill_switch_active(self) -> bool:
        return os.getenv(self.kill_switch_env, "").strip().lower() in {
            "1", "true", "yes", "on", "active",
        }


class OrderSafetyGuard:
    """Enforce startup policy at the final order-submission boundary."""

    def __init__(
        self,
        policy: StartupSafetyPolicy,
        *,
        clock: Callable[[], date] = date.today,
    ) -> None:
        self.policy = policy
        self._clock = clock
        self._risk_day = clock()
        self._daily_new_risk = 0.0

    def assert_order_allowed(self, symbol: str, side: str, qty: float, price: Optional[float]) -> None:
        if self.policy.kill_switch_active():
            raise SafetyConfigurationError("global kill switch is active")
        if symbol not in self.policy.allowed_symbols or symbol not in self.policy.symbols:
            raise SafetyConfigurationError("order symbol is not allowlisted for this run")
        if price is None or price <= 0:
            raise SafetyConfigurationError("a positive reference price is required for safety limits")
        notional = abs(qty * price)
        if notional > self.policy.max_order_notional:
            raise SafetyConfigurationError("order exceeds maximum notional")

        today = self._clock()
        if today != self._risk_day:
            self._risk_day = today
            self._daily_new_risk = 0.0
        if side.lower() in {"buy", "short"}:
            if self._daily_new_risk + notional > self.policy.max_daily_new_risk:
                raise SafetyConfigurationError("order exceeds daily new-risk limit")
            self._daily_new_risk += notional


def verify_live_permissions(exchange: Any, account_type: str) -> None:
    """Require an authoritative permission response before real-money startup."""
    fetcher = getattr(exchange, "fetch_api_permissions", None)
    if not callable(fetcher):
        fetcher = getattr(exchange, "sapi_get_account_api_restrictions", None)
    if not callable(fetcher):
        raise SafetyConfigurationError("exchange adapter cannot verify API permissions")

    permissions = fetcher() or {}
    withdrawal = permissions.get("enableWithdrawals")
    if withdrawal is None:
        withdrawal = permissions.get("withdraw")
    if withdrawal is not False:
        raise SafetyConfigurationError("API withdrawal permission must be explicitly disabled")

    if account_type in {"future", "futures", "swap"}:
        trading = permissions.get("enableFutures")
    else:
        trading = permissions.get("enableSpotAndMarginTrading")
        if trading is None:
            trading = permissions.get("trade")
    if trading is not True:
        raise SafetyConfigurationError("required API trading permission is not enabled")
