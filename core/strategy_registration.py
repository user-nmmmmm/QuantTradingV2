"""Freeze health policy identity and reject unreachable recovery universes."""
from dataclasses import asdict
import hashlib
import json


def register_health_policies(strategies, symbols, routing, *, research=None):
    research = research or {}
    overrides = research.get("strategy_eligible_symbols", {})
    universe = set(symbols)
    registrations = {}
    for name in sorted(set(routing.values()) - {"Cash", ""}):
        strategy = strategies.get(name)
        health = getattr(strategy, "health", None)
        if health is None:
            continue
        declared = overrides.get(name, universe)
        if isinstance(declared, (str, bytes)):
            raise ValueError(f"Registered health universe must be a symbol sequence: {name}")
        eligible = set(declared)
        if not eligible or not eligible.issubset(universe):
            raise ValueError(f"Invalid registered health universe: {name}")
        policy = health.policy
        if policy.enabled and policy.probation_min_distinct_symbols > len(eligible):
            raise ValueError(
                f"Unreachable health recovery: {name} requires "
                f"{policy.probation_min_distinct_symbols} distinct symbols, "
                f"registered universe contains {len(eligible)}. Register an explicit "
                "per-strategy research policy; thresholds are never silently lowered."
            )
        payload = {"schema": "strategy-health-registration/v1", "strategy": name,
                   "eligible_symbols": sorted(eligible), "policy": asdict(policy),
                   "experiment_id": research.get("experiment_id")}
        # Normalize tuples to the same JSON types used in persisted checkpoints.
        payload = json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))
        payload["sha256"] = hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        health.registration = payload
        strategy.eligible_symbols = eligible
        registrations[name] = payload
    return registrations
