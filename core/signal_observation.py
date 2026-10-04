"""Passive raw-candidate collection and first-observed-gate attribution (P0)."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import inspect
import json
from typing import Any

import pandas as pd

from core.entry_audit import capture
from core.signal_observation_types import (
    ContextSnapshot, ObservationPolicy, SignalCandidateEvent, canonical,
    close_time, context_features, finite, fingerprint, iso,
)
from core.signal_outcomes import ForwardOutcomeTracker, ObservationCosts
from core.temporal_data import strict_market_event, temporal_policy as parse_temporal_policy
from core.temporal_labels import VersionedOutcomeTracker


UPSTREAM = {
    "warmup": "warmup", "portfolio_block": "account_risk",
    "account_terminated": "account_risk", "symbol_entry_blocked": "universe",
    "position_held": "position", "router_cooldown": "router_cooldown",
    "router_switch_cooldown": "router_cooldown", "missing_data": "data",
}
GATES = {
    **UPSTREAM, "market_state_cash": "regime", "regime_not_routed": "regime",
    "strategy_unavailable": "regime", "strategy_health_block": "strategy_health",
    "strategy_cooldown": "strategy_health", "entry_pending": "pending_order",
    "entry_pending_or_disallowed_state": "pending_order_or_regime",
    "correlated_budget": "portfolio_risk", "drawdown_budget": "portfolio_risk",
    "below_minimum_notional": "minimum_notional", "zero_sizing": "position_sizing",
    "cash_limit": "portfolio_risk", "leverage_limit": "portfolio_risk",
    "concentration_limit": "portfolio_risk", "cluster_exposure": "portfolio_risk",
    "crypto_beta_exposure": "portfolio_risk", "liquidity_limit": "liquidity",
    "execution_rejected": "execution", "account_health_block": "account_risk",
    "notional_budget_exhausted": "portfolio_risk", "unverifiable_exposure": "portfolio_risk",
    "invalid_order_size_or_price": "position_sizing",
}


def strategy_identity(strategy):
    """Version source + declared scalar/policy parameters, excluding runtime state."""
    params = {}
    for name, value in vars(strategy).items():
        if name.startswith("_") or name in {"raw_setup_count", "suppressed_setup_count",
                                            "observed_close_events", "last_raw_setup_at",
                                            "last_suppressed_setup_at"}:
            continue
        if isinstance(value, (str, int, float, bool)) and not isinstance(value, pd.Timestamp):
            params[name] = value
    for name in ("stop_policy", "score_policy"):
        value = getattr(strategy, name, None)
        if is_dataclass(value):
            params[name] = asdict(value)
    sources = []
    for cls in type(strategy).__mro__:
        if cls.__module__ == "builtins":
            continue
        try:
            sources.append(inspect.getsource(cls))
        except (TypeError, OSError):
            sources.append(f"{cls.__module__}.{cls.__qualname__}")
    return fingerprint({"parameters": params, "source": sources})


class SignalObserver:
    def __init__(self, *, policy: ObservationPolicy, costs: ObservationCosts,
                 strategies: dict, state_machine: Any, config_identity: dict | None = None,
                 temporal_policy=None, financing=None):
        self.policy, self.costs, self.strategies = policy, costs, strategies
        self.state_machine = deepcopy(state_machine)
        self.versions = {name: strategy_identity(s) for name, s in strategies.items()}
        self.snapshot_version = fingerprint({"schema": policy.schema,
            "policy": asdict(policy), "costs": asdict(costs), "config": config_identity or {}})
        self.candidates: list[SignalCandidateEvent] = []
        self.decisions: list[dict] = []
        self.errors: list[dict] = []
        self.coverage: Counter = Counter()
        self.temporal_policy = parse_temporal_policy(temporal_policy)
        self.financing = financing
        self.outcomes = (VersionedOutcomeTracker(policy, costs, knowledge=self.temporal_policy.knowledge, financing=financing)
                         if self.temporal_policy.mode == "strict" else ForwardOutcomeTracker(policy, costs))
        self._frames: dict = {}
        self._seen: set = set()
        self._pending_decisions: list = []
        self._last_event = None

    def advance(self, event):
        if self._last_event is not None and event.timestamp < self._last_event:
            raise ValueError("signal observation events must be time ordered")
        self._ensure_temporal_tracker(event)
        self.outcomes.advance(event)
        self._last_event = event.timestamp

    def _ensure_temporal_tracker(self, event):
        if strict_market_event(event) and not isinstance(self.outcomes, VersionedOutcomeTracker):
            if self.candidates or self.outcomes.results:
                raise ValueError("cannot mix retrospective candidates with strict versioned outcomes")
            self.outcomes = VersionedOutcomeTracker(self.policy, self.costs, financing=self.financing)

    def _private_frame(self, symbol, source):
        signature = (id(source), len(source), source.index[-1],
                     (source.attrs.get("temporal_audit") or {}).get("as_of"))
        prior = self._frames.get(symbol)
        if prior is None or prior[0] != signature:
            frame = source.copy(deep=True)
            # Provenance was consumed by the versioned outcome tracker. Pandas
            # copies attrs on every derived Series, so keep the private feature
            # calculation frame free of the full raw-version proof map.
            frame.attrs.pop("temporal_source_versions", None)
            # Built-in providers use only trailing indicators. Precomputation
            # is private; raw_entry_signal below NEVER receives future rows.
            self.state_machine.get_states(frame)
            for strategy in self.strategies.values():
                ensure = getattr(strategy, "_ensure_indicators", None)
                if callable(ensure):
                    ensure(frame)
            self._frames[symbol] = signature, frame
        return self._frames[symbol][1]

    def observe(self, event, symbol, *, portfolio, risk_manager, router, audit):
        key = (iso(event.timestamp), symbol)
        if key in self._seen:
            return
        self._seen.add(key)
        source = event.histories.get(symbol)
        temporal = (source.attrs.get("temporal_audit") or {}) if source is not None else {}
        strict = event.source == "historical_strict" or temporal.get("mode") == "strict"
        if strict and (source is None or source.empty or event.timestamp not in source.index):
            self.coverage["temporal:unavailable_current_bar"] += 1
            return
        if source is None or source.empty or symbol not in event.bars:
            self.errors.append({"timestamp": key[0], "symbol": symbol, "reason": "missing_history"})
            return
        try:
            self._ensure_temporal_tracker(event)
            if isinstance(self.outcomes, VersionedOutcomeTracker) and self._last_event is None:
                self.outcomes.advance(event)
            loc = source.index.get_loc(event.timestamp)
            frame = self._private_frame(symbol, source).iloc[:loc+1]
            current = frame.iloc[-1]
            available = close_time(event.timestamp, event.timeframe)
            if strict:
                cutoff = temporal.get("as_of")
                if cutoff is None or pd.Timestamp(cutoff) < pd.Timestamp(available):
                    raise ValueError("strict temporal history lacks a valid decision cutoff")
                available = iso(cutoff)
            if "available_at" in frame:
                input_times = pd.to_datetime(frame.available_at, utc=True, errors="coerce")
                if input_times.isna().any() or (input_times > pd.Timestamp(available)).any():
                    raise ValueError("input not yet available at candidate close")
            state = current["market_state"]
            features = canonical(context_features(frame))
            for name, strategy in sorted(self.strategies.items()):
                self.coverage[f"{name}:evaluated_bars"] += 1
                try:
                    with capture(None):
                        signal = strategy.raw_entry_signal(symbol, loc, frame.copy(deep=True))
                    if not signal:
                        continue
                    side = signal["action"]
                    if side not in {"buy", "short"}:
                        raise ValueError("raw signal must open long or short")
                    price = finite(signal.get("price", current.close))
                    score = finite(signal.get("score", signal.get("priority", 0)))
                    if price is None or price <= 0 or score is None:
                        raise ValueError("nonfinite raw price or score")
                    # JSON serialisation freezes nested stop/score dictionaries.
                    signal_json = canonical(signal)
                    health = getattr(strategy, "health", None)
                    ctx = getattr(strategy, "context", {}).get(symbol, {})
                    snapshot = ContextSnapshot(
                        key[0], available, event.timeframe, state.name,
                        getattr(getattr(health, "status", None), "value", "not_applicable"),
                        float(strategy.health_risk_multiplier()),
                        getattr(risk_manager.breaker_action, "value", str(risk_manager.breaker_action)),
                        float(risk_manager.risk_multiplier),
                        float(portfolio.get_position(symbol)["qty"]), bool(ctx.get("entry_pending")),
                        features, self.snapshot_version,
                    )
                    identity = {"timeframe": event.timeframe, "timestamp": key[0],
                                "symbol": symbol, "strategy": name, "side": side,
                                "version": self.versions[name], "snapshot_version": self.snapshot_version}
                    candidate = SignalCandidateEvent(
                        "candidate_" + fingerprint(identity)[:32], key[0], symbol, name,
                        "long" if side == "buy" else "short", score, price,
                        self.versions[name], signal_json, snapshot,
                        self.costs.estimate_bps(price, self.policy.reference_notional/price, side, current),
                    )
                    self.candidates.append(candidate)
                    self.outcomes.add(candidate)
                    self.coverage[f"{name}:raw_candidates"] += 1
                    selected = getattr(router, "regime_map", {}).get(state.name)
                    self._pending_decisions.append((candidate, audit, selected))
                except Exception as exc:
                    self.errors.append({"timestamp": key[0], "symbol": symbol, "strategy": name,
                                        "reason": type(exc).__name__, "message": str(exc)})
        except Exception as exc:
            self.errors.append({"timestamp": key[0], "symbol": symbol,
                                "reason": type(exc).__name__, "message": str(exc)})

    def settle_decisions(self):
        for candidate, audit, selected in self._pending_decisions:
            reason = audit.get("reason", "not_evaluated")
            accepted = None
            if reason not in UPSTREAM and selected != candidate.strategy:
                reason = "regime_not_routed"
            if reason == "order_accepted":
                accepted, stage = True, "accepted"
            elif reason in GATES:
                accepted, stage = False, GATES[reason]
            else:
                stage = "unknown"
            row = {"candidate_id": candidate.candidate_id, "timestamp": candidate.timestamp,
                   "available_at": candidate.context.available_at, "symbol": candidate.symbol,
                   "strategy": candidate.strategy, "accepted": accepted, "veto_stage": stage,
                   "veto_reason": reason, "gate_scope": "first_observed_block_only",
                   "unvisited_gates": "unknown", "order_id": None,
                   "audit": dict(audit) if selected == candidate.strategy or reason in UPSTREAM else {}}
            if selected == candidate.strategy:
                row["order_id"] = audit.get("order_id")
            self.decisions.append(row)
        self._pending_decisions.clear()

    def finish(self):
        self.settle_decisions()
        at = (close_time(self._last_event, next(iter(self.candidates)).context.timeframe)
              if self.candidates and self._last_event is not None else None)
        self.outcomes.finish(at)

    def export(self):
        result = {"schema": self.policy.schema, "policy": asdict(self.policy),
                "snapshot_version": self.snapshot_version, "costs": asdict(self.costs),
                "strategy_versions": self.versions,
                "candidates": [c.to_dict() for c in self.candidates],
                "decisions": deepcopy(self.decisions), "outcomes": deepcopy(self.outcomes.results),
                "errors": deepcopy(self.errors), "coverage": dict(sorted(self.coverage.items())),
                "status": "incomplete" if self.errors or any(d["accepted"] is None for d in self.decisions) else "complete"}
        if isinstance(self.outcomes, VersionedOutcomeTracker):
            result.update(outcome_revisions=self.outcomes.revisions,
                          temporal_label_protocol=self.outcomes.protocol)
        return result
