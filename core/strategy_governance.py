"""Governance status of each strategy, and what each status is allowed to do.

SR0-2 of ``docs/current_strategy_remediation_roadmap.md``: until the SR5
out-of-sample re-admission closes, ``TrendBreakout`` carries no independent
holdout evidence for its current version. The config now says
``paused_revalidation``, and this module is what makes that label mean
something at the entry point: research, shadow and sandbox runs are allowed,
real-money routing is not.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

#: Strategy may be routed with real money.
ADMITTED = "admitted"
#: Previously admitted, now waiting for re-admission evidence (SR5).
PAUSED_REVALIDATION = "paused_revalidation"
#: Paused pending a redesign; research only.
PAUSED_REDESIGN = "paused_redesign"
#: Never admitted; research sandbox only.
ISOLATED_RESEARCH = "isolated_research"

KNOWN_STATUSES = frozenset({
    ADMITTED, PAUSED_REVALIDATION, PAUSED_REDESIGN, ISOLATED_RESEARCH,
})

#: Statuses that may open real-money risk.
LIVE_CAPABLE_STATUSES = frozenset({ADMITTED})


class GovernanceError(RuntimeError):
    """Raised when a run would treat a non-admitted strategy as production."""


def governance_map(config_obj: Any) -> Dict[str, str]:
    """Read ``strategy_governance`` from config, rejecting unknown labels."""
    raw = config_obj.require("strategy_governance")
    if not isinstance(raw, Mapping):
        raise GovernanceError("strategy_governance must be a mapping")
    statuses = {str(name): str(status) for name, status in raw.items()}
    unknown = {
        name: status for name, status in statuses.items()
        if status not in KNOWN_STATUSES
    }
    if unknown:
        raise GovernanceError(
            f"unknown strategy governance status(es): {unknown}; "
            f"expected one of {sorted(KNOWN_STATUSES)}"
        )
    return statuses


def is_live_capable(status: Optional[str]) -> bool:
    return str(status) in LIVE_CAPABLE_STATUSES


def assert_live_admission(
    config_obj: Any, strategy_names: Iterable[str],
) -> Dict[str, str]:
    """Fail closed before any real-money run that routes a non-admitted strategy.

    A strategy with no entry in ``strategy_governance`` is treated as not
    admitted: silence is never evidence.
    """
    statuses = governance_map(config_obj)
    blocked = {}
    for name in strategy_names:
        if name in {"Cash", ""}:
            continue
        status = statuses.get(name, "unregistered")
        if not is_live_capable(status):
            blocked[name] = status
    if blocked:
        raise GovernanceError(
            "live routing refused: "
            + ", ".join(f"{name}={status}" for name, status in sorted(blocked.items()))
            + ". Only 'admitted' strategies may open real-money risk; run these "
            "in research/shadow/sandbox until SR5 re-admission completes."
        )
    return statuses


def routed_strategy_names(config_obj: Any) -> list[str]:
    """Strategy names the regime map would actually route to."""
    routing = config_obj.require("routing")
    return sorted({
        str(name) for name in routing.values() if str(name) not in {"Cash", ""}
    })
