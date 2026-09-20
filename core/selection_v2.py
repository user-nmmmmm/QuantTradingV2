"""Causal, all-qualified portfolio selection and sizing (S2 v2).

This interface is deliberately separate from S2 v1's TopN/cash contract.  It
produces desired exposures, never orders or synthetic negative cash balances.
Daily candle indexes denote opening time; a candle becomes available at its
explicit close_time, or at index + one day when close_time is absent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from numbers import Integral
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from core.indicators import Indicators
from core.protective_stops import ProtectiveStopPolicy, plan_initial_stop
from core.universe import normalize_symbol


def utc_time(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("missing timestamp")
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


@dataclass(frozen=True)
class SelectionPolicyV2:
    selection_mode: str = "all_qualified"
    top_n: None = None
    buffer_ranks: None = None
    variant: str = "momentum"
    horizons: tuple[int, int] = (60, 120)
    min_listing_days: int = 180
    min_quote_volume: float = 5_000_000.0
    volume_window: int = 20
    sma_window: int = 120
    entry_window: int = 20
    exit_window: int = 60
    atr_period: int = 14
    initial_atr_multiple: float = 2.0
    min_stop_distance_pct: float = 0.005
    max_stop_distance_pct: float = 0.35
    use_obv: bool = True
    volatility_window: int = 60
    periods_per_year: float = 365.0
    covariance_diagonal_shrinkage: float = 0.20
    target_annual_volatility: float = 0.10
    max_symbol_weight: float = 0.30
    max_cluster_weight: float = 1.50
    max_crypto_weight: float = 2.0
    max_gross_weight: float = 3.0
    max_symbol_initial_risk: float = 0.01
    max_parent_initial_risk: float = 0.03

    def __post_init__(self):
        if self.selection_mode != "all_qualified" or self.top_n is not None or self.buffer_ranks is not None:
            raise ValueError("all_qualified forbids TopN and ranking buffers")
        if self.variant not in {"momentum", "breakout"}:
            raise ValueError("variant must be momentum or breakout")
        horizons = tuple(self.horizons)
        if len(horizons) != 2 or len(set(horizons)) != 2 or any(
            isinstance(value, bool) or not isinstance(value, Integral) or value <= 0 for value in horizons
        ):
            raise ValueError("horizons must contain two distinct positive integers")
        object.__setattr__(self, "horizons", horizons)
        for name in ("min_listing_days", "volume_window", "sma_window", "entry_window", "exit_window", "volatility_window", "atr_period"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.volatility_window < 2:
            raise ValueError("volatility_window must contain at least two returns")
        for name in ("min_quote_volume", "periods_per_year", "target_annual_volatility", "initial_atr_multiple", "max_symbol_weight",
                     "min_stop_distance_pct", "max_stop_distance_pct",
                     "max_cluster_weight", "max_crypto_weight", "max_gross_weight", "max_symbol_initial_risk", "max_parent_initial_risk"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 <= self.covariance_diagonal_shrinkage <= 1:
            raise ValueError("covariance_diagonal_shrinkage must be in [0, 1]")
        if not self.min_stop_distance_pct < self.max_stop_distance_pct < 1:
            raise ValueError("stop distance bounds must be ordered and below one")

    @property
    def required_closes(self) -> int:
        return max(121, max(self.horizons) + 1, self.sma_window, self.volatility_window + 1,
                   self.volume_window, self.entry_window + 1, self.exit_window + 1, self.atr_period)


def completed_history(history: pd.DataFrame, as_of: Any) -> pd.DataFrame:
    """Return an independent, chronologically sorted prefix of available bars."""
    if not isinstance(history.index, pd.DatetimeIndex):
        raise ValueError("daily history requires a DatetimeIndex")
    frame = history.copy(deep=False)
    frame.index = pd.to_datetime(frame.index, utc=True)
    now = utc_time(as_of)
    completed = (pd.to_datetime(frame["close_time"], utc=True, errors="coerce")
                 if "close_time" in frame else frame.index + pd.Timedelta(days=1))
    visible = np.asarray(completed <= now) & np.asarray(frame.index <= now)
    if "available_at" in frame:
        available = pd.to_datetime(frame["available_at"], utc=True, errors="coerce")
        visible &= np.asarray(available <= now)
    # Filter knowledge time before duplicate checks: appending a later
    # correction to an old candle cannot change its earlier historical use.
    frame = frame.loc[visible].copy()
    if frame.index.has_duplicates:
        if "available_at" not in frame:
            raise ValueError("duplicate candle opening time without revision provenance")
        frame["_revision_time_v2"] = pd.to_datetime(frame["available_at"], utc=True)
        records = frame.reset_index(names="_opening_time_v2")
        if records.duplicated(["_opening_time_v2", "_revision_time_v2"]).any():
            raise ValueError("ambiguous candle revisions at the same knowledge time")
        frame = frame.sort_values("_revision_time_v2", kind="stable")
        frame = frame.loc[~frame.index.duplicated(keep="last")].drop(columns="_revision_time_v2")
    return frame if frame.index.is_monotonic_increasing else frame.sort_index()


def signal_facts(history: pd.DataFrame, *, as_of: Any,
                 policy: SelectionPolicyV2 | None = None) -> dict[str, Any]:
    """Price/liquidity facts only; no metadata, health mutation or future data."""
    policy = policy or SelectionPolicyV2()
    return _signal_facts_prepared(completed_history(history, as_of), as_of=as_of, policy=policy)


def _signal_facts_prepared(frame: pd.DataFrame, *, as_of: Any, policy: SelectionPolicyV2,
                           price_only: bool = False) -> dict[str, Any]:
    """Evaluate one already-validated causal prefix without copying it again."""
    result: dict[str, Any] = {"qualified": False, "reasons": [], "add_allowed": False,
                              "score": None, "close": None, "stop_price": None,
                              "observed_at": None, "available_at": None,
                              "breakout": False, "obv_confirmed": False}
    if len(frame) < policy.required_closes:
        result["reasons"].append("insufficient_contiguous_history")
        return result
    tail = frame.iloc[-policy.required_closes:]
    latest_open = tail.index[-1]
    minimum_open = utc_time(as_of).normalize() - pd.Timedelta(days=1)
    expected = pd.date_range(end=latest_open,
                             periods=policy.required_closes, freq="D", tz="UTC")
    if latest_open < minimum_open or not tail.index.equals(expected):
        result["reasons"].append("missing_or_stale_daily_close")
        return result
    try:
        prices = tail[["high", "low", "close"]].to_numpy(dtype=float)
        if not np.isfinite(prices).all() or not (prices > 0).all():
            raise ValueError("invalid prices")
        closes = tail["close"].to_numpy(dtype=float)
    except (KeyError, TypeError, ValueError):
        result["reasons"].append("invalid_prices")
        return result
    close = float(closes[-1])
    score = float(math.fsum(close / closes[-1 - h] - 1 for h in policy.horizons) / 2)
    observed = tail.index[-1]
    available = (utc_time(tail["close_time"].iloc[-1]) if "close_time" in tail
                 else observed + pd.Timedelta(days=1))
    if "available_at" in tail:
        available = max(available, utc_time(tail["available_at"].iloc[-1]))
    result.update(close=close, score=score, observed_at=observed.isoformat(), available_at=available.isoformat(),
                  sma=float(np.mean(closes[-policy.sma_window:])),
                  daily_exit=score < 0 or close < float(tail["low"].iloc[-policy.exit_window - 1:-1].min()))
    if price_only:
        return result
    try:
        quote_volume = tail["quote_volume"].iloc[-policy.volume_window:].to_numpy(dtype=float)
        valid_quote = np.isfinite(quote_volume).all() and (quote_volume >= 0).all()
    except (KeyError, ValueError, TypeError):
        valid_quote = False
    if not valid_quote:
        result["reasons"].append("missing_actual_quote_volume")
    else:
        result["median_quote_volume"] = float(np.median(quote_volume))
        if result["median_quote_volume"] < policy.min_quote_volume:
            result["reasons"].append("insufficient_quote_volume")
    if score <= 0:
        result["reasons"].append("nonpositive_momentum")
    if close <= result["sma"]:
        result["reasons"].append("below_sma")
    high = float(tail["high"].iloc[-policy.entry_window - 1:-1].max())
    breakout = close > high
    # The existing OBV confirmation is its net accumulation over entry_window.
    # Recompute from raw bars: supplied indicator columns are not trusted facts.
    obv_confirmed = False
    if "volume" in tail:
        volumes = pd.to_numeric(tail["volume"], errors="coerce").to_numpy(dtype=float)
        changes = np.sign(np.diff(closes[-policy.entry_window - 1:]))
        active_volumes = volumes[-policy.entry_window:]
        obv_confirmed = bool(np.isfinite(active_volumes).all() and (active_volumes >= 0).all()
                             and np.sum(changes * active_volumes) > 0)
    result.update(breakout=breakout, obv_confirmed=obv_confirmed)
    # A positive usable initial stop is required to grant any new risk.
    numeric_prices = frame[["high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    atr = float(Indicators.ATR(numeric_prices, policy.atr_period).iloc[-1])
    stop_plan = plan_initial_stop(side="buy", reference_price=close, structural_stop=None, atr=atr,
        policy=ProtectiveStopPolicy(initial_stop_mode="atr", initial_atr_multiple=policy.initial_atr_multiple,
                                    min_stop_distance_pct=policy.min_stop_distance_pct,
                                    max_stop_distance_pct=policy.max_stop_distance_pct))
    if stop_plan.accepted:
        result["atr"] = atr
        result["stop_price"] = stop_plan.stop_price
    else:
        result["reasons"].append("invalid_atr_stop")
    result["qualified"] = not result["reasons"]
    result["add_allowed"] = result["qualified"] and (
        policy.variant == "momentum" or breakout and (obv_confirmed or not policy.use_obv))
    return result


@dataclass(frozen=True)
class SelectionRowV2:
    symbol: str
    qualified: bool
    reasons: tuple[str, ...]
    add_allowed: bool
    score: float | None = None
    close: float | None = None
    stop_price: float | None = None
    observed_at: str | None = None
    available_at: str | None = None
    cluster: str = "crypto_beta"
    held: bool = False
    force_exit: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SelectionResultV2:
    as_of: str
    rows: dict[str, SelectionRowV2]
    selected_symbols: tuple[str, ...]
    returns: pd.DataFrame = field(repr=False, compare=False)
    policy: SelectionPolicyV2 = field(default_factory=SelectionPolicyV2)

    def to_dict(self) -> dict[str, Any]:
        return {"version": 2, "as_of": self.as_of, "selected_symbols": list(self.selected_symbols),
                "rows": {symbol: row.to_dict() for symbol, row in self.rows.items()}, "policy": asdict(self.policy)}


def _metadata_reasons(meta: Mapping[str, Any], as_of: pd.Timestamp, policy: SelectionPolicyV2) -> list[str]:
    reasons = []
    status = meta.get("source_status", meta.get("status"))
    if status != "verified":
        reasons.append("unverified_market_metadata")
    classification_value = meta.get("classification", meta.get("asset_class", "unknown"))
    classification_time = meta.get("classification_available_at", meta.get("available_at"))
    known_classes = []
    for event in meta.get("classification_events", ()):
        if event.get("source_status") != "verified":
            continue
        try:
            available = utc_time(event["available_at"])
            if available <= as_of:
                known_classes.append((available, str(event["classification"])))
        except (KeyError, TypeError, ValueError):
            continue
    if known_classes:
        classification_time, classification_value = max(known_classes, key=lambda item: item[0])
        if len({value.lower() for timestamp, value in known_classes if timestamp == classification_time}) > 1:
            reasons.append("ambiguous_classification_revision")
    classification = str(classification_value).lower()
    if classification not in {"crypto", "cryptocurrency"}:
        reasons.append("excluded_or_unknown_classification")
    try:
        classification_available = utc_time(classification_time)
        if classification_available > as_of:
            reasons.append("classification_not_yet_available")
    except (TypeError, ValueError):
        reasons.append("missing_classification_available_at")
    try:
        effective = utc_time(meta.get("listing_effective_at", meta.get("listed_at")))
        available = utc_time(meta.get("listing_available_at", meta.get("available_at")))
        if available > as_of:
            reasons.append("listing_not_yet_available")
        if effective > as_of - pd.Timedelta(days=policy.min_listing_days):
            reasons.append("insufficient_listing_age")
    except (TypeError, ValueError):
        reasons.append("missing_listing_evidence")
    if meta.get("active") is False and meta.get("active_available_at") is not None:
        try:
            if utc_time(meta["active_available_at"]) <= as_of:
                reasons.append("inactive_membership")
        except (TypeError, ValueError):
            reasons.append("invalid_membership_available_at")
    # Undisclosed future delistings must not affect earlier decisions.
    for event in meta.get("events", ()):
        if event.get("kind", event.get("action", event.get("event_type"))) not in {
            "delist", "spot_delist", "spot_delisted", "delisting"
        } or event.get("source_status", "unverified") != "verified":
            continue
        try:
            if utc_time(event["available_at"]) <= as_of:
                reasons.append("delisted" if utc_time(event["effective_at"]) <= as_of
                               else "announced_spot_delisting")
        except (KeyError, TypeError, ValueError):
            continue
    return reasons


def select_all_qualified(histories: Mapping[str, pd.DataFrame], metadata: Mapping[str, Mapping[str, Any]], *,
                         as_of: Any, held_symbols: Iterable[str] = (),
                         policy: SelectionPolicyV2 | None = None) -> SelectionResultV2:
    policy = policy or SelectionPolicyV2()
    now = utc_time(as_of)
    originals = {normalize_symbol(symbol): symbol for symbol in histories}
    if len(originals) != len(histories):
        raise ValueError("duplicate normalized history symbol")
    normalized_metadata = {normalize_symbol(symbol): item for symbol, item in metadata.items()}
    if len(normalized_metadata) != len(metadata):
        raise ValueError("duplicate normalized metadata symbol")
    held = {originals.get(normalize_symbol(symbol), symbol) for symbol in held_symbols}
    rows: dict[str, SelectionRowV2] = {}
    return_columns = {}
    for symbol, history in sorted(histories.items()):
        meta = normalized_metadata.get(normalize_symbol(symbol), {})
        reasons = _metadata_reasons(meta, now, policy)
        if reasons:
            rows[symbol] = SelectionRowV2(symbol, False, tuple(dict.fromkeys(reasons)), False,
                                         cluster=str(meta.get("cluster", "crypto_beta")), held=symbol in held,
                                         force_exit=bool({"delisted", "announced_spot_delisting"} & set(reasons)))
            continue
        try:
            frame = completed_history(history, now)
            facts = _signal_facts_prepared(frame, as_of=now, policy=policy)
        except (ValueError, TypeError):
            facts = {"reasons": ["invalid_history"], "add_allowed": False}
        reasons.extend(facts["reasons"])
        if not reasons:
            returns = frame["close"].iloc[-policy.volatility_window - 1:].astype(float).pct_change(fill_method=None).iloc[1:]
            volatility = float(returns.std(ddof=1))
            if not math.isfinite(volatility) or volatility <= 1e-12:
                reasons.append("invalid_realized_volatility")
            else:
                return_columns[symbol] = returns
        rows[symbol] = SelectionRowV2(
            symbol=symbol, qualified=not reasons, reasons=tuple(dict.fromkeys(reasons)),
            add_allowed=not reasons and bool(facts.get("add_allowed")),
            score=facts.get("score"), close=facts.get("close"), stop_price=facts.get("stop_price"),
            observed_at=facts.get("observed_at"), available_at=facts.get("available_at"),
            cluster=str(meta.get("cluster", "crypto_beta")), held=symbol in held)
    for symbol in sorted(held - set(rows)):
        rows[symbol] = SelectionRowV2(symbol, False, ("missing_history",), False, held=True)
    selected = tuple(symbol for symbol in sorted(rows) if rows[symbol].qualified)
    return SelectionResultV2(now.isoformat(), rows, selected, pd.DataFrame(return_columns), policy)


@dataclass(frozen=True)
class PortfolioTargetsV2:
    as_of: str
    target_weights: dict[str, float]
    unconstrained_weights: dict[str, float]
    estimated_volatility_before: float
    estimated_volatility_after: float
    binding_constraints: dict[str, tuple[str, ...]]
    add_allowed: dict[str, bool]
    stop_prices: dict[str, float]
    applied_multipliers: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def size_portfolio_targets(selection: SelectionResultV2, *, health_multiplier: float = 1.0,
                           account_multiplier: float = 1.0, clusters: Mapping[str, str] | None = None,
                           stop_distances: Mapping[str, float] | None = None,
                           existing_weights: Mapping[str, float] | None = None,
                           daily_new_risk_budget: float = 0.02) -> PortfolioTargetsV2:
    """Inverse volatility/covariance target, followed by non-refilling caps.

    Multipliers are explicit and applied exactly once here. An executor must
    not apply the same multiplier again to these already-constrained targets.
    Existing weights only limit incremental risk; unsold exposure is reserved
    by the execution/account planner, never treated as settled buying power.
    """
    policy = selection.policy
    symbols = selection.selected_symbols
    for name, value in (("health_multiplier", health_multiplier), ("account_multiplier", account_multiplier)):
        if not math.isfinite(float(value)) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be in [0, 1]")
    if not math.isfinite(daily_new_risk_budget) or daily_new_risk_budget < 0:
        raise ValueError("daily_new_risk_budget must be finite and nonnegative")
    multiplier_facts = {"health": float(health_multiplier), "account": float(account_multiplier)}
    if not symbols:
        return PortfolioTargetsV2(selection.as_of, {}, {}, 0.0, 0.0, {}, {}, {}, multiplier_facts)
    returns = selection.returns.reindex(columns=symbols).tail(policy.volatility_window)
    if len(returns) != policy.volatility_window or not np.isfinite(returns.to_numpy(dtype=float)).all():
        raise ValueError("portfolio covariance requires common complete trailing returns")
    covariance = returns.cov(ddof=1).to_numpy(dtype=float) * policy.periods_per_year
    shrinkage = policy.covariance_diagonal_shrinkage
    covariance = (1.0 - shrinkage) * covariance + shrinkage * np.diag(np.diag(covariance))
    volatility = np.sqrt(np.diag(covariance))
    if not np.isfinite(volatility).all() or (volatility <= 1e-12).any():
        raise ValueError("invalid realized volatility")
    inverse = 1.0 / volatility
    base = inverse / inverse.sum()
    portfolio_volatility = float(np.sqrt(base @ covariance @ base))
    if not math.isfinite(portfolio_volatility) or portfolio_volatility <= 1e-12:
        raise ValueError("invalid portfolio volatility")
    unconstrained = base * policy.target_annual_volatility / portfolio_volatility
    weights = unconstrained.copy()
    constraints: dict[str, list[str]] = {symbol: [] for symbol in symbols}

    def cap_total(indices: list[int], cap: float, reason: str) -> None:
        total = float(np.sum(weights[indices]))
        if total > cap:
            weights[indices] *= cap / total
            for index in indices:
                constraints[symbols[index]].append(reason)

    for index, symbol in enumerate(symbols):
        if weights[index] > policy.max_symbol_weight:
            weights[index] = policy.max_symbol_weight
            constraints[symbol].append("symbol_exposure")
    cluster_map = {symbol: (clusters or {}).get(symbol, selection.rows[symbol].cluster) for symbol in symbols}
    for cluster in sorted(set(cluster_map.values())):
        cap_total([index for index, symbol in enumerate(symbols) if cluster_map[symbol] == cluster],
                  policy.max_cluster_weight, "cluster_exposure")
    cap_total(list(range(len(symbols))), policy.max_crypto_weight, "crypto_exposure")
    cap_total(list(range(len(symbols))), policy.max_gross_weight, "account_leverage")
    for reason, multiplier in multiplier_facts.items():
        if multiplier < 1:
            weights *= multiplier
            for symbol in symbols:
                constraints[symbol].append(f"{reason}_multiplier")
    distances = np.empty(len(symbols), dtype=float)
    existing = np.array([float((existing_weights or {}).get(symbol, 0.0)) for symbol in symbols])
    if not np.isfinite(existing).all() or (existing < 0).any():
        raise ValueError("existing_weights must be finite and nonnegative")
    for index, symbol in enumerate(symbols):
        row = selection.rows[symbol]
        distance = ((stop_distances or {}).get(symbol) if stop_distances and symbol in stop_distances
                    else (row.close - row.stop_price) / row.close if row.close and row.stop_price else None)
        if distance is None or not math.isfinite(float(distance)) or not 0 < distance < 1:
            raise ValueError(f"missing measurable stop distance for {symbol}")
        distances[index] = float(distance)
        maximum = policy.max_symbol_initial_risk / distances[index]
        if weights[index] > maximum:
            weights[index] = maximum
            constraints[symbol].append("symbol_initial_risk")
    total_risk = float(weights @ distances)
    if total_risk > policy.max_parent_initial_risk:
        weights *= policy.max_parent_initial_risk / total_risk
        for symbol in symbols:
            constraints[symbol].append("parent_initial_risk")
    # Breakout is a permission to add risk, never a demand to sell a held coin.
    for index, symbol in enumerate(symbols):
        if not selection.rows[symbol].add_allowed and weights[index] > existing[index]:
            weights[index] = existing[index]
            constraints[symbol].append("breakout_add_gate")
    increments = np.maximum(weights - existing, 0.0)
    new_risk = float(increments @ distances)
    if new_risk > daily_new_risk_budget:
        factor = daily_new_risk_budget / new_risk
        weights = np.minimum(weights, existing) + increments * factor
        for index, symbol in enumerate(symbols):
            if increments[index] > 0:
                constraints[symbol].append("daily_new_risk")
    return PortfolioTargetsV2(
        selection.as_of, dict(zip(symbols, map(float, weights))), dict(zip(symbols, map(float, unconstrained))),
        float(np.sqrt(unconstrained @ covariance @ unconstrained)), float(np.sqrt(weights @ covariance @ weights)),
        {symbol: tuple(reasons) for symbol, reasons in constraints.items()},
        {symbol: selection.rows[symbol].add_allowed for symbol in symbols},
        {symbol: float(selection.rows[symbol].stop_price) for symbol in symbols}, multiplier_facts)
