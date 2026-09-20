"""Causal account-level quote borrowing evidence for opt-in margin research.

The policy is deliberately independent of symbol iteration order. Evidence of
coin borrowing (used to short a coin) is not evidence of USDT borrowing.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd


def utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("missing borrow timestamp")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


@dataclass(frozen=True)
class QuoteBorrowFact:
    account: str
    quote_currency: str
    effective_at: str
    available_at: str
    annual_rate: float | None
    limit: float | None
    status: str = "verified"
    source_uri: str = ""
    expires_at: str | None = None

    def __post_init__(self):
        if not self.account or self.quote_currency != "USDT":
            raise ValueError("account and USDT quote currency are required")
        if self.status not in {"verified", "assumed", "unavailable"}:
            raise ValueError("invalid borrowing evidence status")
        utc(self.effective_at)
        utc(self.available_at)
        if self.expires_at is not None and utc(self.expires_at) <= utc(self.effective_at):
            raise ValueError("borrow expiry must follow effective time")
        if self.status != "unavailable":
            for name in ("annual_rate", "limit"):
                value = getattr(self, name)
                if value is None or not math.isfinite(float(value)) or value < 0:
                    raise ValueError(f"{name} must be finite and nonnegative")
        if self.status == "verified" and not self.source_uri:
            raise ValueError("verified borrowing requires a source URI")


@dataclass(frozen=True)
class QuoteBorrowDecision:
    annual_rate: float
    limit: float
    status: str
    source: str


@dataclass(frozen=True)
class MarginEligibilityDecision:
    allowed: bool
    status: str
    reason: str


class QuoteBorrowPolicy:
    """Select visible evidence, or the explicitly declared 8% model assumption.

    ``limit`` is total outstanding quote debt, not incremental capacity. Missing
    evidence in verified-only mode allows cash-funded buys but no new debt.
    """

    def __init__(self, facts=(), *, mode="assumed", assumed_annual_rate=.08,
                 assumed_limit=1e100, quote_currency="USDT"):
        if mode not in {"assumed", "verified_only"}:
            raise ValueError("borrow mode must be assumed or verified_only")
        if quote_currency != "USDT":
            raise ValueError("only USDT quote borrowing is supported")
        for value in (assumed_annual_rate, assumed_limit):
            if not math.isfinite(float(value)) or value < 0:
                raise ValueError("borrow assumptions must be finite and nonnegative")
        self.facts = tuple(facts)
        self.mode = mode
        self.assumed_annual_rate = float(assumed_annual_rate)
        self.assumed_limit = float(assumed_limit)
        self.quote_currency = quote_currency
        keys = [(f.account, f.quote_currency, utc(f.effective_at), utc(f.available_at))
                for f in self.facts]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate account borrowing revision")

    def resolve(self, *, account, as_of) -> QuoteBorrowDecision:
        point = utc(as_of)
        visible = [fact for fact in self.facts
                   if fact.account == account and fact.quote_currency == self.quote_currency
                   and utc(fact.effective_at) <= point and utc(fact.available_at) <= point]
        if visible:
            fact = max(visible, key=lambda f: (utc(f.effective_at), utc(f.available_at)))
            unavailable = (fact.status == "unavailable" or
                           (fact.expires_at is not None and point >= utc(fact.expires_at)) or
                           (self.mode == "verified_only" and fact.status != "verified"))
            if unavailable:
                return QuoteBorrowDecision(0., 0., "unavailable", fact.source_uri or "unavailable")
            return QuoteBorrowDecision(float(fact.annual_rate), float(fact.limit),
                                       fact.status, fact.source_uri or "explicit_assumption")
        if self.mode == "verified_only":
            return QuoteBorrowDecision(0., 0., "unavailable", "missing_verified_quote_borrow")
        return QuoteBorrowDecision(self.assumed_annual_rate, self.assumed_limit,
                                   "assumed", "explicit_quote_borrow_assumption")

    def market_eligibility(self, *, symbol, as_of, metadata=None) -> MarginEligibilityDecision:
        """Market permission and quote credit are independent causal facts.

        A visible margin-removal announcement stops NEW financing immediately,
        while a positive eligibility fact cannot grant it before effective time.
        Base-coin borrowing suspensions never stand in for USDT borrowing.
        """
        point, metadata = utc(as_of), metadata or {}
        visible = []
        quote_events = []
        for market, row in metadata.items():
            for fact in row.get("events", ()):
                if (fact.get("source_status") != "verified" or fact.get("available_at") is None
                        or fact.get("effective_at") is None or utc(fact["available_at"]) > point):
                    continue
                kind = fact.get("kind", fact.get("action"))
                effective, available = utc(fact["effective_at"]), utc(fact["available_at"])
                if market == symbol and kind in {"margin_eligible", "margin_delisted"}:
                    if kind == "margin_delisted" or effective <= point:
                        visible.append((effective, available, kind))
                asset = fact.get("borrow_asset", fact.get("borrow_currency",
                         fact.get("asset", fact.get("quote_currency"))))
                if asset == "USDT" and kind in {"borrow_suspended", "borrow_resumed"}:
                    if kind == "borrow_suspended" or effective <= point:
                        quote_events.append((effective, available, kind))
        if quote_events and max(quote_events)[2] == "borrow_suspended":
            return MarginEligibilityDecision(False, "verified", "usdt_borrow_suspended")
        if visible:
            latest = max(visible)
            if latest[2] == "margin_delisted":
                return MarginEligibilityDecision(False, "verified", "announced_margin_removal")
            return MarginEligibilityDecision(True, "verified", "verified_market_margin_eligibility")
        if self.mode == "assumed":
            return MarginEligibilityDecision(True, "assumed", "assumed_all_qualified_spot_markets_margin_eligible")
        return MarginEligibilityDecision(False, "unavailable", "missing_verified_market_margin_eligibility")

    def interest(self, *, account, start, end, principal):
        """Piecewise causal ACT/365 interest; later evidence never rewrites costs."""
        first, last = utc(start), utc(end)
        if last <= first or principal <= 0:
            return 0., []
        cuts = {first, last}
        for fact in self.facts:
            if fact.account != account:
                continue
            known = max(utc(fact.effective_at), utc(fact.available_at))
            if first < known < last:
                cuts.add(known)
            if fact.expires_at is not None and first < utc(fact.expires_at) < last:
                cuts.add(utc(fact.expires_at))
        points = sorted(cuts)
        amount, segments = 0., []
        for left, right in zip(points, points[1:]):
            decision = self.resolve(account=account, as_of=left)
            if decision.status == "unavailable":
                raise ValueError("outstanding quote debt lacks a visible financing rate")
            cost = float(principal) * decision.annual_rate * (right-left).total_seconds() / (365*86400)
            amount += cost
            segments.append({"start": left.isoformat(), "end": right.isoformat(),
                             "rate": decision.annual_rate, "status": decision.status,
                             "source": decision.source, "amount": cost})
        return amount, segments
