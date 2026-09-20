"""Independent P2/P3 research policies; P0/P1 source identities stay unchanged."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from pathlib import Path

from core.signal_ev_types import EVPolicy
from core.signal_observation_types import fingerprint


def _positive(value, name, *, zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < 0 or (not zero and value == 0)):
        raise ValueError(f"{name} must be {'nonnegative' if zero else 'positive'} and finite")


@dataclass(frozen=True)
class AdaptiveEVPolicy:
    enabled: bool = False
    ev_policy: EVPolicy = field(default_factory=lambda: EVPolicy(enabled=True))
    regime_fit_days: int = 120
    regime_clusters: int = 2
    regime_neighbors: int = 20
    regime_quantiles: int = 21
    min_regime_samples: int = 40
    regime_restarts: int = 3
    regime_max_iter: int = 50
    seed: int = 20260918
    attribution_window_days: int = 90
    attribution_min_samples: int = 50
    attribution_min_blocks: float = 8.0
    axis_weight_floor: float = 0.15
    axis_weight_cap: float = 0.60
    attribution_ema_rate: float = 0.25
    schema: str = "signal_adaptive_ev/v1"

    def __post_init__(self):
        if type(self.enabled) is not bool or not isinstance(self.ev_policy, EVPolicy):
            raise ValueError("enabled must be boolean and ev_policy must be EVPolicy")
        for name in ("regime_fit_days", "regime_clusters", "regime_neighbors", "regime_quantiles",
                     "min_regime_samples", "regime_restarts", "regime_max_iter", "attribution_window_days",
                     "attribution_min_samples"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        if self.regime_clusters < 2 or self.regime_quantiles < 3 or self.regime_neighbors < 2:
            raise ValueError("at least two states/neighbors and three quantiles are required")
        if self.min_regime_samples <= max(self.regime_clusters, self.regime_neighbors):
            raise ValueError("regime samples must exceed cluster and neighborhood counts")
        if self.regime_fit_days >= self.ev_policy.train_days:
            raise ValueError("regime fitting must leave a later EV population period")
        if self.attribution_min_samples < 3:
            raise ValueError("attribution requires at least three predictions")
        _positive(self.attribution_min_blocks, "attribution_min_blocks")
        if self.attribution_min_blocks < 2:
            raise ValueError("attribution requires at least two time blocks")
        for name in ("axis_weight_floor", "axis_weight_cap", "attribution_ema_rate"):
            _positive(getattr(self, name), name)
        if not 0 < self.axis_weight_floor <= 1/3 <= self.axis_weight_cap < 1:
            raise ValueError("bounds must contain uniform three-axis weights")
        if self.attribution_ema_rate > 1:
            raise ValueError("EMA rate must be <= 1")
        if self.schema != "signal_adaptive_ev/v1":
            raise ValueError("unsupported adaptive EV schema")

    @classmethod
    def from_mapping(cls, value=None):
        if isinstance(value, cls):
            return value
        values = dict(value or {})
        if "ev_policy" in values and not isinstance(values["ev_policy"], EVPolicy):
            values["ev_policy"] = EVPolicy.from_mapping(values["ev_policy"])
        return cls(**values)

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MetaReplayPolicy:
    enabled: bool = False
    horizon_bars: int = 5
    initial_capital: float = 10000.0
    reference_notional: float = 1000.0
    max_gross_fraction: float = 1.0
    min_size_multiplier: float = 0.25
    max_size_multiplier: float = 1.0
    full_size_ev_bps: float = 100.0
    schema: str = "signal_meta_replay/v1"

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.horizon_bars) is not int or self.horizon_bars < 1:
            raise ValueError("enabled must be boolean and horizon_bars a positive integer")
        for name in ("initial_capital", "reference_notional", "max_gross_fraction",
                     "min_size_multiplier", "max_size_multiplier", "full_size_ev_bps"):
            _positive(getattr(self, name), name)
        if not self.min_size_multiplier <= self.max_size_multiplier <= 1:
            raise ValueError("research sizing multipliers must lie in (0, 1]")
        if self.max_gross_fraction > 1:
            raise ValueError("research account pre-trade gross budget must be <= equity")
        if self.schema != "signal_meta_replay/v1":
            raise ValueError("unsupported replay schema")

    @classmethod
    def from_mapping(cls, value=None):
        return value if isinstance(value, cls) else cls(**dict(value or {}))

    def to_dict(self):
        return asdict(self)


def adaptive_implementation_identity(policy):
    root = Path(__file__).resolve().parent
    names = ("signal_adaptive_types.py", "signal_regime_model.py", "signal_axis_attribution.py",
             "signal_adaptive.py", "signal_ev_types.py", "signal_ev_ledger.py", "signal_meta_layer.py")
    return fingerprint({"sources": {name: (root/name).read_text(encoding="utf-8") for name in names},
                        "policy": policy.to_dict()})
