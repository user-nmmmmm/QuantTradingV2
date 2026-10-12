"""One account mode, one cost model - validated, never mixed.

SR3-4 of ``docs/current_strategy_remediation_roadmap.md`` (STR-P1-05/06).

The frozen baseline ran a contradiction: ``account.mode: spot_margin``
(collateral semantics, quote borrowed to fund longs) combined with a
commission rate whose own comment said "Binance Futures standard taker", and a
financing ledger that never charged the long side a cent. Any one of those
three is defensible on its own; together they describe a venue that does not
exist, and no re-admission evidence may be produced on them.

This module makes the pairing explicit and checkable:

* the configured fee schedule must name the same market type as the account;
* a ``spot_margin`` account must model borrow (otherwise leveraged longs are
  free money);
* a ``perpetual`` account must require historical funding.

The check runs at both entry points (backtest and live), so the mismatch can
never be discovered afterwards, from a report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


class AccountCostContractError(ValueError):
    """The account mode and the configured cost model disagree."""


#: Account mode -> fee-schedule market types that are consistent with it.
_COMPATIBLE_MARKET_TYPES = {
    "spot": {"spot"},
    "spot_margin": {"spot", "spot_margin", "margin"},
    "perpetual": {"perpetual", "futures", "swap"},
}

_RUNTIME_ACCOUNT_MODES = {
    "spot": "spot",
    "spot_margin": "spot_margin",
    "margin": "spot_margin",
    "perpetual": "perpetual",
    "future": "perpetual",
    "futures": "perpetual",
    "swap": "perpetual",
}

_DEFAULT_RUNTIME_MARKET_TYPES = {
    "spot": "spot",
    "spot_margin": "margin",
    "perpetual": "swap",
}


@dataclass(frozen=True)
class AccountCostContract:
    """The validated (account mode, fee schedule, financing) triple."""

    account_mode: str
    venue: str
    market_type: str
    commission_rate_taker: float
    commission_rate_maker: float
    fee_source: str
    borrow_modeled: bool
    funding_modeled: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_mode": self.account_mode,
            "venue": self.venue,
            "market_type": self.market_type,
            "commission_rate_taker": self.commission_rate_taker,
            "commission_rate_maker": self.commission_rate_maker,
            "fee_source": self.fee_source,
            "borrow_modeled": self.borrow_modeled,
            "funding_modeled": self.funding_modeled,
        }


def validate_account_cost_contract(
    configuration: Any, *, account_mode: Optional[str] = None,
) -> AccountCostContract:
    """Fail closed when the account mode and the cost model disagree.

    ``account_mode`` overrides the configured mode for runs that select one on
    the command line (``main.py --market-type``).
    """
    execution = dict(configuration.require("execution") or {})
    account = dict(configuration.require("account") or {})
    mode = str(account_mode or account.get("mode") or "spot").lower()
    if mode not in _COMPATIBLE_MARKET_TYPES:
        raise AccountCostContractError(
            f"unknown account mode {mode!r}; expected one of "
            f"{sorted(_COMPATIBLE_MARKET_TYPES)}"
        )

    schedule = execution.get("fee_schedule")
    if not isinstance(schedule, dict):
        raise AccountCostContractError(
            "execution.fee_schedule is required: the commission rates must "
            "name the venue and market type they were taken from, so an "
            "account mode can never silently borrow another venue's fees"
        )
    market_type = str(schedule.get("market_type", "")).lower()
    if market_type not in _COMPATIBLE_MARKET_TYPES[mode]:
        raise AccountCostContractError(
            f"account.mode={mode!r} is incompatible with "
            f"execution.fee_schedule.market_type={market_type!r}; pick one "
            "venue semantics and configure its fees, borrow and funding "
            "together (SR3-4)"
        )

    borrow_rate = float(account.get("default_borrow_rate_annual", 0.0) or 0.0)
    funding_required = bool(account.get("funding_rate_required", False))
    if mode == "spot_margin" and borrow_rate <= 0:
        raise AccountCostContractError(
            "account.mode=spot_margin requires a positive "
            "default_borrow_rate_annual: leveraged longs borrow the quote "
            "currency and that borrow is not free (STR-P1-05)"
        )
    if mode == "perpetual" and not funding_required:
        raise AccountCostContractError(
            "account.mode=perpetual requires funding_rate_required=true so a "
            "missing historical funding rate fails instead of being silently "
            "treated as zero"
        )

    return AccountCostContract(
        account_mode=mode,
        venue=str(schedule.get("venue", "unknown")),
        market_type=market_type,
        commission_rate_taker=float(execution["commission_rate_taker"]),
        commission_rate_maker=float(execution["commission_rate_maker"]),
        fee_source=str(schedule.get("source", "unspecified")),
        borrow_modeled=mode in {"spot_margin", "perpetual"} and borrow_rate > 0,
        funding_modeled=mode == "perpetual" and funding_required,
    )


def canonical_runtime_account_mode(market_type: str) -> str:
    """Translate an exchange/CLI market type into the cost-contract mode."""

    normalized = str(market_type or "").strip().lower()
    try:
        return _RUNTIME_ACCOUNT_MODES[normalized]
    except KeyError as exc:
        raise AccountCostContractError(
            f"unknown runtime market type {normalized!r}; expected one of "
            f"{sorted(_RUNTIME_ACCOUNT_MODES)}"
        ) from exc


def default_runtime_market_type(account_mode: str) -> str:
    """Return the exchange-facing default for a configured account mode."""

    normalized = str(account_mode or "").strip().lower()
    try:
        return _DEFAULT_RUNTIME_MARKET_TYPES[normalized]
    except KeyError as exc:
        raise AccountCostContractError(
            f"unknown configured account mode {normalized!r}; expected one of "
            f"{sorted(_DEFAULT_RUNTIME_MARKET_TYPES)}"
        ) from exc


def validate_runtime_account_cost_contract(
    configuration: Any, *, market_type: str,
) -> AccountCostContract:
    """Validate the account that will actually be opened by the live broker.

    The configured account mode is the research/backtest contract.  A live
    command-line override may select the exchange endpoint, but it may not
    silently select a different economic account.  Exchange-facing aliases
    (``margin``, ``swap`` and ``futures``) are normalized before comparison.
    """

    configured = validate_account_cost_contract(configuration)
    runtime_mode = canonical_runtime_account_mode(market_type)
    if runtime_mode != configured.account_mode:
        raise AccountCostContractError(
            f"runtime market_type={market_type!r} resolves to "
            f"account.mode={runtime_mode!r}, but configuration requires "
            f"account.mode={configured.account_mode!r}; the live broker and "
            "its fee/financing contract must use the same account mode"
        )
    return validate_account_cost_contract(
        configuration, account_mode=runtime_mode,
    )
