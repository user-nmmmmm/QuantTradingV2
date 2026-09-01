"""Economically meaningful ranking of same-bar entry candidates (SR3-1).

STR-P1-03: every ``EntryCandidate`` carried ``score=0``. Ranking therefore fell
through to the deterministic ``(strategy_name, symbol)`` tie-break, so whenever
capital or risk budget ran out the system was really allocating **in
alphabetical order** - an undisclosed rule that looks like a ranking in the
audit trail.

The score below is built only from facts available at signal time:

======================  ====================================================
component               meaning
======================  ====================================================
``breakout_extent``     how far price cleared the channel, in ATR units -
                        a 0.1-ATR poke and a 2-ATR thrust are not the same
                        signal
``trend_strength``      ADX above its threshold, normalised
``volume_confirmation`` OBV accumulation over the entry window, normalised
                        by its own recent scale
``liquidity``           log-scaled traded notional: a signal you cannot fill
                        without moving the market is worth less
======================  ====================================================

Portfolio-relative terms (marginal correlation, current cluster exposure) are
deliberately *not* here: they depend on the book, not on the signal, and are
applied at allocation time by :mod:`core.portfolio_risk`, which is the only
place that sees the whole same-timestamp batch.

Scores are comparable across symbols by construction (every term is a ratio),
which is what lets one ranking span the batch.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class CandidateScorePolicy:
    """Weights for the entry-quality score. All terms are dimensionless."""

    enabled: bool = True
    weights: Mapping[str, float] = field(default_factory=lambda: {
        "breakout_extent": 1.0,
        "trend_strength": 0.5,
        "volume_confirmation": 0.3,
        "liquidity": 0.2,
    })
    adx_threshold: float = 25.0
    #: Traded notional that scores 1.0 on the liquidity term.
    liquidity_reference_notional: float = 10_000_000.0
    #: Clip every component into [-cap, cap] so one extreme reading cannot
    #: dominate the ranking.
    component_cap: float = 5.0

    @classmethod
    def from_mapping(
        cls, mapping: Optional[Mapping[str, Any]],
    ) -> "CandidateScorePolicy":
        data = dict(mapping or {})
        weights = data.pop("weights", None)
        kwargs = {
            key: value for key, value in data.items()
            if key in cls.__dataclass_fields__ and key != "weights"
        }
        if weights is not None:
            kwargs["weights"] = {str(k): float(v) for k, v in dict(weights).items()}
        return cls(**kwargs)


@dataclass(frozen=True)
class ScoreBreakdown:
    """The score plus every component that produced it, for the audit trail."""

    total: float
    components: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        return {"total": self.total, "components": dict(self.components)}


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _clip(value: float, cap: float) -> float:
    return max(-cap, min(cap, value))


def score_breakout_candidate(
    *,
    reference_price: Any,
    channel_level: Any,
    atr: Any = None,
    adx: Any = None,
    obv_change: Any = None,
    obv_scale: Any = None,
    traded_notional: Any = None,
    policy: Optional[CandidateScorePolicy] = None,
    side: str = "buy",
) -> ScoreBreakdown:
    """Score one breakout/breakdown signal.

    ``channel_level`` is the Donchian level that was cleared. ``obv_scale`` is
    a positive normaliser for ``obv_change`` (e.g. the window's mean absolute
    OBV move); without it the volume term is skipped rather than compared
    across incomparable units.
    """
    policy = policy or CandidateScorePolicy()
    components: Dict[str, float] = {}
    if not policy.enabled:
        return ScoreBreakdown(0.0, components)

    price = _finite(reference_price)
    level = _finite(channel_level)
    atr_value = _finite(atr)
    long_side = side in {"buy", "long"}

    if price is not None and level is not None:
        excess = (price - level) if long_side else (level - price)
        scale = atr_value if atr_value and atr_value > 0 else (
            abs(price) * 0.01 if price else None
        )
        if scale:
            components["breakout_extent"] = _clip(excess / scale, policy.component_cap)

    adx_value = _finite(adx)
    if adx_value is not None and policy.adx_threshold > 0:
        components["trend_strength"] = _clip(
            (adx_value - policy.adx_threshold) / policy.adx_threshold,
            policy.component_cap,
        )

    change = _finite(obv_change)
    scale = _finite(obv_scale)
    if change is not None and scale and scale > 0:
        directional = change if long_side else -change
        components["volume_confirmation"] = _clip(
            directional / scale, policy.component_cap
        )

    notional = _finite(traded_notional)
    if notional is not None and notional > 0 and policy.liquidity_reference_notional > 0:
        components["liquidity"] = _clip(
            math.log10(notional / policy.liquidity_reference_notional) + 1.0,
            policy.component_cap,
        )

    total = sum(
        float(policy.weights.get(name, 0.0)) * value
        for name, value in components.items()
    )
    return ScoreBreakdown(total, components)
