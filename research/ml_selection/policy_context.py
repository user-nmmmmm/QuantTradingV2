"""Decision-time policy inputs, opt-in for new artifacts; never execution outcomes."""
from collections.abc import Mapping
from copy import deepcopy
import math

import pandas as pd


POLICY_CONTEXT_FEATURES = (
    "native_candidate_score", "stop_distance_fraction", "batch_candidate_count",
    "health_risk_multiplier", "market_risk_multiplier", "portfolio_risk_multiplier",
    "requested_notional_fraction", "candidate_is_short",
)


class PolicyContextUnavailable(ValueError):
    def __init__(self, missing_features, available_features=None):
        self.missing_features = tuple(missing_features)
        self.available_features = dict(available_features or {})
        super().__init__("policy context unavailable: " + ", ".join(self.missing_features))


def _number(value, *, nonnegative=False, positive=False):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (nonnegative and result < 0) or (positive and result <= 0):
        return None
    return result


def _query(candidate, method_name, *args):
    method = getattr(getattr(candidate, "strategy", None), method_name, None)
    if not callable(method):
        return None
    try:
        return _number(method(*args), nonnegative=True)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def validate_decision_context_snapshot(context, *, as_of):
    if (not isinstance(context, Mapping)
            or context.get("schema") != "ml-selection-decision-context/v1"
            or context.get("source") != "decision_time_candidate_and_runtime_queries_not_post_allocation_audit"):
        raise ValueError("invalid captured policy decision context")
    recorded, cutoff = pd.Timestamp(context.get("available_at")), pd.Timestamp(as_of)
    if pd.isna(recorded) or pd.isna(cutoff):
        raise ValueError("captured policy decision context requires finite timestamps")
    recorded = recorded.tz_localize("UTC") if recorded.tzinfo is None else recorded.tz_convert("UTC")
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    if recorded != cutoff:
        raise ValueError("captured policy decision context must match its decision as_of")
    features = context.get("policy_context_features")
    if not isinstance(features, Mapping) or set(features) - set(POLICY_CONTEXT_FEATURES):
        raise ValueError("invalid captured policy context feature inventory")
    if any(_number(value) is None for value in features.values()):
        raise ValueError("captured policy context features must be finite")
    for name, value in features.items():
        number = float(value)
        if (name != "native_candidate_score" and number < 0
                or name == "stop_distance_fraction" and number <= 0
                or name == "batch_candidate_count" and (number <= 0 or not number.is_integer())
                or name == "candidate_is_short" and number not in {0., 1.}):
            raise ValueError("captured policy context feature is outside its domain: " + name)
    return context


def decision_context_snapshot(candidate, *, current_prices, as_of, features=None):
    """Freeze observable candidate facts before selection/ordering/sizing mutates audits."""
    captured = getattr(candidate, "decision_context", None)
    if captured is not None:
        validate_decision_context_snapshot(captured, as_of=as_of)
        return deepcopy(dict(captured))
    raw_signal = getattr(candidate, "signal", None)
    signal = raw_signal if isinstance(raw_signal, Mapping) else {}
    health = getattr(getattr(candidate, "strategy", None), "health", None)
    status = getattr(health, "status", None)
    raw_state = getattr(candidate, "state", None)
    state = getattr(raw_state, "name", raw_state)
    stamp = pd.Timestamp(as_of)
    if pd.isna(stamp):
        raise ValueError("decision context requires a finite as_of")
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    values = dict(features or {})
    return {"schema": "ml-selection-decision-context/v1", "available_at": stamp.isoformat(),
            "strategy": candidate.strategy_name, "market_state": str(state) if state is not None else None,
            "strategy_health": str(getattr(status, "value", status)) if status is not None else None,
            "native_candidate_score": _number(candidate.score),
            "current_price": _number(current_prices.get(candidate.symbol), positive=True),
            "stop_loss": _number(signal.get("stop_loss"), positive=True),
            "requested_qty": _number(signal.get("requested_qty"), nonnegative=True),
            "health_multiplier": values.get("health_risk_multiplier"),
            "market_multiplier": values.get("market_risk_multiplier"),
            "policy_context_features": values,
            "source": "decision_time_candidate_and_runtime_queries_not_post_allocation_audit"}


def build_policy_context(candidate, *, requested_features, batch_candidate_count,
                         current_prices, equity, risk_manager, as_of):
    """Return only requested finite facts; unknown inputs do not become neutral values."""
    requested = tuple(requested_features)
    if len(set(requested)) != len(requested) or set(requested) - set(POLICY_CONTEXT_FEATURES):
        raise ValueError("unsupported or duplicate policy context features")
    if not requested:
        return {}
    captured = getattr(candidate, "decision_context", None)
    if captured is not None:
        validate_decision_context_snapshot(captured, as_of=as_of)
        saved = captured["policy_context_features"]
        missing = [name for name in requested if name not in saved]
        present = {name: float(saved[name]) for name in requested if name in saved}
        if missing:
            raise PolicyContextUnavailable(missing, present)
        return present
    # Validate the caller's information cutoff even though no future audit is read.
    stamp = pd.Timestamp(as_of)
    if pd.isna(stamp):
        raise ValueError("policy context requires a finite as_of")
    raw_signal = getattr(candidate, "signal", None)
    signal = raw_signal if isinstance(raw_signal, Mapping) else {}
    price = _number(current_prices.get(candidate.symbol), positive=True)
    account_equity = _number(equity, positive=True)
    action = str(signal.get("action", ""))
    values = {}
    for name in requested:
        value = None
        if name == "native_candidate_score":
            value = _number(candidate.score)
        elif name == "batch_candidate_count":
            count = _number(batch_candidate_count, positive=True)
            value = count if count is not None and count.is_integer() else None
        elif name == "candidate_is_short":
            value = float(action == "short") if action in {"buy", "short"} else None
        elif name == "stop_distance_fraction":
            stop = _number(signal.get("stop_loss"), positive=True)
            if price is not None and stop is not None and (
                    (action == "buy" and stop < price) or (action == "short" and stop > price)):
                value = abs(price - stop) / price
        elif name == "health_risk_multiplier":
            value = _query(candidate, "health_risk_multiplier")
        elif name == "market_risk_multiplier":
            state = getattr(candidate, "state", None)
            value = _query(candidate, "entry_risk_multiplier", state) if state is not None else None
        elif name == "portfolio_risk_multiplier":
            value = _number(getattr(risk_manager, "risk_multiplier", None), nonnegative=True)
        elif name == "requested_notional_fraction":
            qty = _number(signal.get("requested_qty"), nonnegative=True)
            if qty is not None and price is not None and account_equity is not None:
                value = _number(qty * price / account_equity, nonnegative=True)
        values[name] = value
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise PolicyContextUnavailable(missing, {name: value for name, value in values.items() if value is not None})
    return values
