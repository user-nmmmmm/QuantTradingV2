"""Frozen, opt-in P1 research policy and predeclared context partitions."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from pathlib import Path

from core.signal_observation_types import finite, fingerprint


@dataclass(frozen=True)
class EVPolicy:
    enabled: bool = False
    half_life_days: float = 90.0
    prior_strength: float = 20.0
    min_effective_samples: float = 20.0
    min_effective_blocks: float = 8.0
    min_weight_mass: float = 5.0
    confidence_z: float = 1.96
    min_std_bps: float = 50.0
    min_ev_bps: float = 0.0
    train_days: int = 365
    test_days: int = 90
    embargo_days: int = 20
    block_days: int = 5
    schema: str = "signal_ev/v1"

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("signal meta-layer enabled must be boolean")
        for name in ("half_life_days", "min_weight_mass", "confidence_z", "min_std_bps"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        for name, floor in (("prior_strength", 0), ("min_effective_samples", 2), ("min_effective_blocks", 2)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < floor:
                raise ValueError(f"{name} must be finite and >= {floor}")
        if (isinstance(self.min_ev_bps, bool) or not isinstance(self.min_ev_bps, (int, float))
                or finite(self.min_ev_bps) is None):
            raise ValueError("min_ev_bps must be finite")
        for name in ("train_days", "test_days", "embargo_days", "block_days"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "embargo_days" else 1):
                raise ValueError(f"invalid {name}")
        if self.schema != "signal_ev/v1":
            raise ValueError("unsupported signal EV schema")

    @classmethod
    def from_mapping(cls, value=None):
        return cls(**dict(value or {}))

    def to_dict(self):
        return asdict(self)


CONTEXT_DEFINITION = {
    "version": "fixed_context_axes/v1",
    "market_state": {"source": "market_state", "states": ["TREND_UP", "TREND_DOWN", "SIDEWAYS", "VOLATILE"]},
    "efficiency": {"source": "efficiency_ratio_10", "thresholds": [0.25, 0.5],
                   "states": ["low", "medium", "high"], "valid_range": [0.0, 1.0]},
    "volatility": {"source": "volatility_ratio_8_48", "thresholds": [0.8, 1.2],
                   "states": ["compressed", "normal", "expanded"], "valid_range": [0.0, None]},
    "boundary_rule": "left-closed upper bucket (value == threshold moves right)",
    "missing": "unknown; never impute a future/full-sample statistic",
    "weights": "uniform_additive; axes are not assumed independent",
    "direction_pooling": "separate long and short for all axes",
    "scope": "fixed interpretable P1 baseline; not Wasserstein clustering or calibrated probabilities",
}


def context_memberships(candidate):
    """One-hot special case of entry-time posterior stamping; no fitting."""
    context = candidate["context"]
    features = context.get("features") or {}
    state = context.get("market_state")
    result = {"market_state": {state if state in CONTEXT_DEFINITION["market_state"]["states"] else "unknown": 1.0}}
    for name in ("efficiency", "volatility"):
        spec = CONTEXT_DEFINITION[name]
        value = finite(features.get(spec["source"]))
        minimum, maximum = spec["valid_range"]
        if value is None or value < minimum or (maximum is not None and value > maximum+1e-12):
            label = "unknown"
        else:
            label = spec["states"][sum(value >= boundary for boundary in spec["thresholds"])]
        result[name] = {label: 1.0}
    return result


def implementation_identity(policy):
    """Pin this model's code, fixed partitions and all parameters, not test data."""
    root = Path(__file__).resolve().parent
    names = ("signal_ev_types.py", "signal_ev_ledger.py", "signal_meta_layer.py")
    sources = {name: (root/name).read_text(encoding="utf-8") for name in names}
    return fingerprint({"sources": sources, "context": CONTEXT_DEFINITION, "policy": policy.to_dict()})
