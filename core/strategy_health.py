"""Strategy health lifecycle: exit cohorts + ACTIVE/COOLDOWN/PROBATION/MANUAL_LOCK.

Implements SR1-1/SR1-2/SR1-3 of
``docs/current_strategy_remediation_roadmap.md`` and the contract written down
in ``docs/strategy_health_contract.md``.

Two defects in the previous ``is_alive`` gate are closed here:

* **STR-P0-01** - ``is_alive=False`` was a permanent kill switch with no expiry.
  It is replaced by a state machine whose only non-recovering state
  (``MANUAL_LOCK``) is reached explicitly, is persisted, and is reported.
* **STR-P0-02/04** - health counted one observation per *symbol* close, so a
  single portfolio-level risk action closing fifteen correlated symbols looked
  like fifteen independent strategy failures. The authoritative unit is now the
  **exit cohort**::

      cohort_id = opening_strategy
                + exit_session (UTC date of the closing fill)
                + exit_controller (who forced the exit)
                + risk_action_id (breaker action / epoch, when present)

  and, by default, cohorts whose controller is *not* the strategy itself do not
  feed the health trigger at all - they are still recorded and reported so the
  attribution required by SR3-3 stays available.

Thresholds here are research candidates, not validated production constants:
``docs/research/current_strategy_experiment_registry.jsonl`` registers the
families that must be searched before any of these values is called admitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import math
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd


class HealthStatus(str, Enum):
    """Lifecycle states of a strategy's alpha-health gate."""

    ACTIVE = "active"
    COOLDOWN = "cooldown"
    PROBATION = "probation"
    MANUAL_LOCK = "manual_lock"


#: Exit reasons produced by portfolio/account level risk controls. A cohort
#: closed by these is *attributed* to the opening strategy for PnL, but does not
#: by itself prove the alpha stopped working (STR-P0-04).
ACCOUNT_RISK_EXIT_REASONS = frozenset({
    "DailyLossLimit",
    "AccountLiquidation",
    "MarginLiquidation",
    "DrawdownReduce",
    "DrawdownBudgetReduce",
    "GapRiskResize",
    "unprotected_flatten",
})

#: Exits forced by the run itself, never a statement about the alpha.
SYSTEM_EXIT_REASONS = frozenset({"EndOfBacktest", "AnnouncedMarginDelisting"})

#: Exits owned by the router/allocator rather than by the strategy's own signal.
ROUTER_EXIT_REASONS = frozenset({"MaxHoldingPeriod", "StateSwitch"})

CONTROLLER_STRATEGY = "strategy"
CONTROLLER_ACCOUNT_RISK = "account_risk"
CONTROLLER_ROUTER = "router"
CONTROLLER_SYSTEM = "system"


def classify_exit_controller(exit_reason: Optional[str]) -> str:
    """Map an exit reason onto the controller that actually forced the close."""

    reason = str(exit_reason or "signal")
    if reason in ACCOUNT_RISK_EXIT_REASONS:
        return CONTROLLER_ACCOUNT_RISK
    if reason in SYSTEM_EXIT_REASONS:
        return CONTROLLER_SYSTEM
    if reason in ROUTER_EXIT_REASONS or reason.startswith("Regime "):
        return CONTROLLER_ROUTER
    return CONTROLLER_STRATEGY


def _as_utc(value: Any) -> Optional[datetime]:
    """Normalize any timestamp-ish value to an aware UTC datetime."""

    if value is None:
        return None
    try:
        point = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if point is pd.NaT or pd.isna(point):
        return None
    point = point.tz_localize("UTC") if point.tzinfo is None else point.tz_convert("UTC")
    return point.to_pydatetime()


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


@dataclass(frozen=True)
class StrategyHealthPolicy:
    """Configuration of the health lifecycle.

    Every field is a *research candidate*. SR1-3 registers the families
    ``consecutive_negative_cohorts in [2,3,4]``, ``cooldown_days in [14,30,60]``,
    ``probation_risk_multiplier in [0.25,0.50]`` and
    ``probation_required_cohorts in [3,5,10]``; the defaults below are the
    midpoints used to keep the machine deterministic until that search runs.
    """

    consecutive_negative_cohorts: int = 3
    cooldown_days: float = 30.0
    probation_risk_multiplier: float = 0.25
    probation_required_cohorts: int = 3
    probation_min_total_r: float = 0.0
    max_failed_probation_cycles: int = 2
    # Opt-in research alternative; explicit/previous manual locks never expire.
    repeated_failure_action: str = "manual_lock"
    extended_cooldown_days: float = 90.0
    recovery_risk_multiplier: float = 0.10
    recovery_stages: tuple[float, ...] = ()
    recovery_stage_min_days: float = 30.0
    unified_recovery: bool = False
    probation_min_distinct_symbols: int = 1
    probation_require_positive_without_best: bool = False
    rolling_cohort_window: int = 20
    max_retained_cohorts: int = 500
    max_retained_transitions: int = 200
    #: Controllers whose cohorts may trigger a health transition. Account-risk
    #: exits are recorded but excluded by default (STR-P0-04 / SR3-3).
    counted_controllers: tuple[str, ...] = (CONTROLLER_STRATEGY, CONTROLLER_ROUTER)
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.consecutive_negative_cohorts < 1:
            raise ValueError("consecutive_negative_cohorts must be >= 1")
        if self.cooldown_days < 0:
            raise ValueError("cooldown_days must be >= 0")
        if not 0.0 <= self.probation_risk_multiplier <= 1.0:
            raise ValueError("probation_risk_multiplier must be in [0, 1]")
        if self.probation_required_cohorts < 1:
            raise ValueError("probation_required_cohorts must be >= 1")
        if self.probation_min_distinct_symbols < 1:
            raise ValueError("probation_min_distinct_symbols must be >= 1")
        if self.unified_recovery and not self.recovery_stages:
            raise ValueError("Unified recovery requires explicit recovery stages")
        if self.max_failed_probation_cycles < 1:
            raise ValueError("max_failed_probation_cycles must be >= 1")
        if self.repeated_failure_action not in ("manual_lock", "extended_cooldown"):
            raise ValueError("Unknown repeated_failure_action")
        if not math.isfinite(self.extended_cooldown_days) or self.extended_cooldown_days <= 0:
            raise ValueError("extended_cooldown_days must be finite and positive")
        if not 0 < self.recovery_risk_multiplier <= 1:
            raise ValueError("recovery_risk_multiplier must be in (0, 1]")
        if self.repeated_failure_action == "extended_cooldown":
            if self.extended_cooldown_days < self.cooldown_days:
                raise ValueError("Extended cooldown cannot shorten ordinary cooldown")
            if not 0 < self.probation_risk_multiplier or self.recovery_risk_multiplier > self.probation_risk_multiplier:
                raise ValueError("Recovery risk must be positive and no larger than probation risk")
        if self.recovery_stages:
            if self.repeated_failure_action != "extended_cooldown":
                raise ValueError("Staged recovery requires extended_cooldown")
            values = self.recovery_stages
            if (values[0] != self.recovery_risk_multiplier or values[-1] != 1.0
                    or any(not 0 < x <= 1 for x in values)
                    or any(a >= b for a, b in zip(values, values[1:]))):
                raise ValueError("Recovery stages must increase from recovery risk to 1.0")
        if not math.isfinite(self.recovery_stage_min_days) or self.recovery_stage_min_days <= 0:
            raise ValueError("Recovery stage duration must be finite and positive")

    @classmethod
    def from_mapping(cls, mapping: Optional[Dict[str, Any]]) -> "StrategyHealthPolicy":
        data = dict(mapping or {})
        if "recovery_stages" in data:
            data["recovery_stages"] = tuple(data["recovery_stages"])
        controllers = data.pop("counted_controllers", None)
        kwargs = {
            key: value for key, value in data.items()
            if key in cls.__dataclass_fields__ and key != "counted_controllers"
        }
        if controllers is not None:
            kwargs["counted_controllers"] = tuple(str(item) for item in controllers)
        return cls(**kwargs)


@dataclass
class HealthCohort:
    """One health observation: every close forced by the same exit action."""

    cohort_id: str
    strategy: str
    exit_controller: str
    exit_session: str
    risk_action_id: Optional[str] = None
    net_pnl: float = 0.0
    initial_risk: float = 0.0
    trade_count: int = 0
    symbols: List[str] = field(default_factory=list)
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    counts_toward_health: bool = True

    @property
    def r(self) -> float:
        """Cohort return in units of the risk it put at stake.

        Falls back to the sign of the PnL when no initial risk was recorded, so
        a cohort never silently disappears from a threshold that is defined on
        R (see ``r_is_estimated``).
        """
        if self.initial_risk > 0:
            return self.net_pnl / self.initial_risk
        if self.net_pnl > 0:
            return 1.0
        if self.net_pnl < 0:
            return -1.0
        return 0.0

    @property
    def r_is_estimated(self) -> bool:
        return self.initial_risk <= 0

    @property
    def is_negative(self) -> bool:
        return self.net_pnl < 0

    def add(
        self, *, realized_pnl: float, initial_risk: Optional[float],
        symbol: str, timestamp: Optional[datetime],
    ) -> None:
        self.net_pnl += float(realized_pnl)
        self.initial_risk += float(initial_risk or 0.0)
        self.trade_count += 1
        if symbol and symbol not in self.symbols:
            self.symbols.append(symbol)
        if timestamp is not None:
            if self.opened_at is None or timestamp < self.opened_at:
                self.opened_at = timestamp
            if self.closed_at is None or timestamp > self.closed_at:
                self.closed_at = timestamp

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cohort_id": self.cohort_id,
            "strategy": self.strategy,
            "exit_controller": self.exit_controller,
            "exit_session": self.exit_session,
            "risk_action_id": self.risk_action_id,
            "net_pnl": self.net_pnl,
            "initial_risk": self.initial_risk,
            "r": self.r,
            "r_is_estimated": self.r_is_estimated,
            "trade_count": self.trade_count,
            "symbols": list(self.symbols),
            "opened_at": _iso(self.opened_at),
            "closed_at": _iso(self.closed_at),
            "counts_toward_health": self.counts_toward_health,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthCohort":
        return cls(
            cohort_id=str(data["cohort_id"]),
            strategy=str(data.get("strategy", "")),
            exit_controller=str(data.get("exit_controller", CONTROLLER_STRATEGY)),
            exit_session=str(data.get("exit_session", "")),
            risk_action_id=data.get("risk_action_id"),
            net_pnl=float(data.get("net_pnl", 0.0)),
            initial_risk=float(data.get("initial_risk", 0.0)),
            trade_count=int(data.get("trade_count", 0)),
            symbols=list(data.get("symbols") or []),
            opened_at=_as_utc(data.get("opened_at")),
            closed_at=_as_utc(data.get("closed_at")),
            counts_toward_health=bool(data.get("counts_toward_health", True)),
        )


class StrategyHealthMachine:
    """Durable health lifecycle for one strategy.

    Ingestion is idempotent per ``close_event_id`` (SR1-2): re-delivering the
    same CloseEvent never double-counts. Transitions are evaluated lazily, in
    :meth:`evaluate`, so that every close belonging to the same exit action has
    already been folded into its cohort before the cohort is judged.
    """

    def __init__(
        self, strategy_name: str,
        policy: Optional[StrategyHealthPolicy] = None,
    ) -> None:
        self.strategy_name = strategy_name
        self.policy = policy or StrategyHealthPolicy()
        self.reset()

    # ------------------------------------------------------------------ state

    def reset(self) -> None:
        self.status = HealthStatus.ACTIVE
        self.status_changed_at: Optional[datetime] = None
        self.cooldown_started_at: Optional[datetime] = None
        self.cooldown_until: Optional[datetime] = None
        self.trigger_event_id: Optional[str] = None
        self.trigger_reason: Optional[str] = None
        self.probation_started_at: Optional[datetime] = None
        # Cohorts that already existed when the current health epoch entered
        # probation.  They are an immutable boundary: probation may only be
        # judged on cohorts created after this snapshot, and the losing streak
        # cannot leak across the cooldown/probation boundary.
        self._streak_baseline_cohort_ids: set[str] = set()
        self.failed_probation_cycles = 0
        self.recovery_stage = -1
        self.manual_lock_reason: Optional[str] = None
        self.resume_count = 0
        self.cohorts: List[HealthCohort] = []
        self.transitions: List[Dict[str, Any]] = []
        self._cohort_index: Dict[str, HealthCohort] = {}
        self._consumed_close_event_ids: set[str] = set()
        self._last_close_at: Optional[datetime] = None
        self._last_seen_now: Optional[datetime] = None

    # -------------------------------------------------------------- ingestion

    def ingest_close(
        self, *,
        close_event_id: str,
        symbol: str,
        realized_pnl: float,
        exit_reason: Optional[str] = None,
        initial_risk: Optional[float] = None,
        timestamp: Any = None,
        risk_action_id: Optional[str] = None,
        bar_index: Optional[int] = None,
    ) -> Optional[HealthCohort]:
        """Fold one authoritative close into its cohort. Idempotent."""

        key = str(close_event_id)
        if key in self._consumed_close_event_ids:
            return None
        self._consumed_close_event_ids.add(key)

        moment = _as_utc(timestamp)
        controller = classify_exit_controller(exit_reason)
        session = self._session_of(moment, bar_index)
        cohort_id = ":".join([
            self.strategy_name, session, controller, str(risk_action_id or "-"),
        ])
        cohort = self._cohort_index.get(cohort_id)
        if cohort is None:
            cohort = HealthCohort(
                cohort_id=cohort_id,
                strategy=self.strategy_name,
                exit_controller=controller,
                exit_session=session,
                risk_action_id=risk_action_id,
                counts_toward_health=(
                    controller in self.policy.counted_controllers
                ),
            )
            self._cohort_index[cohort_id] = cohort
            self.cohorts.append(cohort)
            self._trim()
        cohort.add(
            realized_pnl=realized_pnl, initial_risk=initial_risk,
            symbol=symbol, timestamp=moment,
        )
        if moment is not None and (
            self._last_close_at is None or moment > self._last_close_at
        ):
            self._last_close_at = moment
        return cohort

    def _session_of(self, moment: Optional[datetime], bar_index: Optional[int]) -> str:
        if moment is not None:
            return moment.date().isoformat()
        # No timestamp (unit-test/legacy caller): fall back to the bar index so
        # unrelated closes are not silently merged into one cohort.
        return f"bar-{bar_index}" if bar_index is not None else "unknown"

    def _trim(self) -> None:
        overflow = len(self.cohorts) - self.policy.max_retained_cohorts
        if overflow > 0:
            for dropped in self.cohorts[:overflow]:
                self._cohort_index.pop(dropped.cohort_id, None)
            del self.cohorts[:overflow]

    # ------------------------------------------------------------ transitions

    def evaluate(self, now: Any = None) -> HealthStatus:
        """Apply time- and cohort-driven transitions; return the new status."""

        moment = _as_utc(now) or self._last_close_at or self._last_seen_now
        if moment is not None:
            self._last_seen_now = moment
        if not self.policy.enabled:
            return self.status
        if self.status is HealthStatus.MANUAL_LOCK:
            return self.status

        if self.status is HealthStatus.COOLDOWN:
            if self.cooldown_until is None and moment is not None:
                # A cooldown entered without any known time (a caller that
                # delivered closes without timestamps) must still be bounded:
                # anchor it to the first real time we observe rather than
                # letting it become the permanent kill switch SR1-1 removes.
                self.cooldown_started_at = moment
                self.cooldown_until = moment + timedelta(days=self._cooldown_days)
            if self.cooldown_until is not None and moment is not None and (
                moment >= self.cooldown_until
            ):
                self._transition(
                    HealthStatus.PROBATION, moment,
                    reason="cooldown_expired",
                )
            return self.status

        if self.status is HealthStatus.PROBATION:
            if self.probation_started_at is None and moment is not None:
                # A migrated checkpoint starts a fresh observation clock at the
                # first actual evaluation, never at an invented wall-clock time.
                self.probation_started_at = moment
            # A probation verdict is deliberately sample-gated.  In
            # particular, the negative cohorts that caused the preceding
            # cooldown must never make a brand-new probation fail immediately.
            closed = self.probation_closed_cohorts
            if closed < self.policy.probation_required_cohorts:
                return self.status
            if self.recovery_stage >= 0 and (
                moment is None or self.probation_started_at is None
                or moment < self.probation_started_at + timedelta(days=self.policy.recovery_stage_min_days)
            ):
                # Negative evidence never waits for a minimum-time gate.
                if self.probation_total_r <= self.policy.probation_min_total_r:
                    self._fail_probation(moment, reason="probation_total_r_below_gate")
                return self.status
            if self.probation_total_r > self.policy.probation_min_total_r:
                if len(self.probation_symbols) < self.policy.probation_min_distinct_symbols:
                    return self.status
                if (self.policy.probation_require_positive_without_best
                        and self.probation_r_without_best <= 0):
                    return self.status
                if self.recovery_stage >= 0 and self.recovery_stage < len(self.policy.recovery_stages) - 2:
                    self.recovery_stage += 1
                    self._transition(HealthStatus.PROBATION, moment, reason="recovery_stage_passed")
                    return self.status
                self.recovery_stage = -1
                self.resume_count += 1
                self._transition(
                    HealthStatus.ACTIVE, moment,
                    reason="probation_passed",
                )
                self.failed_probation_cycles = 0
            else:
                self._fail_probation(moment, reason="probation_total_r_below_gate")
            return self.status

        consecutive = self.consecutive_negative_cohorts
        if consecutive >= self.policy.consecutive_negative_cohorts:
            trigger = self._last_counted_cohort()
            self._enter_cooldown(
                moment,
                reason=f"consecutive_negative_cohorts>={consecutive}",
                trigger_event_id=trigger.cohort_id if trigger else None,
            )
            return self.status
        return self.status

    def _enter_cooldown(
        self, moment: Optional[datetime], *, reason: str,
        trigger_event_id: Optional[str] = None,
    ) -> None:
        started = moment or self._last_close_at
        self.cooldown_started_at = started
        self.cooldown_until = (
            started + timedelta(days=self._cooldown_days)
            if started is not None else None
        )
        self.trigger_event_id = trigger_event_id
        self.probation_started_at = None
        self._transition(HealthStatus.COOLDOWN, moment, reason=reason)

    def _fail_probation(self, moment: Optional[datetime], *, reason: str) -> None:
        self.failed_probation_cycles += 1
        if self.failed_probation_cycles >= self.policy.max_failed_probation_cycles:
            if self.policy.repeated_failure_action == "extended_cooldown":
                self.recovery_stage = 0 if self.policy.recovery_stages else -1
                self._enter_cooldown(moment, reason=f"{reason}; extended_recovery")
                return
            self.manual_lock_reason = (
                f"{reason}; failed_probation_cycles="
                f"{self.failed_probation_cycles}"
            )
            self.cooldown_until = None
            self._transition(HealthStatus.MANUAL_LOCK, moment, reason=reason)
            return
        self._enter_cooldown(moment, reason=reason)

    def _transition(
        self, target: HealthStatus, moment: Optional[datetime], *, reason: str,
    ) -> None:
        previous = self.status
        if self.policy.unified_recovery and target is HealthStatus.COOLDOWN:
            self.recovery_stage = 0
        if (self.policy.unified_recovery and target is HealthStatus.PROBATION
                and previous is not HealthStatus.PROBATION):
            self.recovery_stage = 0
        evidence = {
            "evidence_cohorts": self.probation_closed_cohorts,
            "evidence_symbols": sorted(self.probation_symbols),
            "evidence_total_r": self.probation_total_r,
            "evidence_r_without_best": self.probation_r_without_best,
        }
        self.status = target
        self.status_changed_at = moment
        self.trigger_reason = reason
        if target is HealthStatus.PROBATION:
            self.probation_started_at = moment
            self._streak_baseline_cohort_ids = {
                cohort.cohort_id for cohort in self.counted_cohorts()
            }
        if target is HealthStatus.ACTIVE:
            self.cooldown_started_at = None
            self.cooldown_until = None
            self.probation_started_at = None
        self.transitions.append({
            **evidence,
            "strategy": self.strategy_name,
            "at": _iso(moment),
            "from": previous.value,
            "to": target.value,
            "reason": reason,
            "trigger_event_id": self.trigger_event_id,
            "cooldown_until": _iso(self.cooldown_until),
            "consecutive_negative_cohorts": self.consecutive_negative_cohorts,
            "risk_multiplier": self.risk_multiplier,
            **({"recovery_stage": self.recovery_stage} if self.policy.recovery_stages else {}),
        })
        overflow = len(self.transitions) - self.policy.max_retained_transitions
        if overflow > 0:
            del self.transitions[:overflow]

    # --------------------------------------------------------------- readouts

    @property
    def _extended_recovery(self) -> bool:
        return (
            self.policy.repeated_failure_action == "extended_cooldown"
            and self.failed_probation_cycles >= self.policy.max_failed_probation_cycles
        )

    @property
    def _cooldown_days(self) -> float:
        return self.policy.extended_cooldown_days if self._extended_recovery else self.policy.cooldown_days

    def counted_cohorts(self) -> List[HealthCohort]:
        return [cohort for cohort in self.cohorts if cohort.counts_toward_health]

    @property
    def consecutive_negative_cohorts(self) -> int:
        count = 0
        cohorts = [
            cohort for cohort in self.counted_cohorts()
            if cohort.cohort_id not in self._streak_baseline_cohort_ids
        ]
        for cohort in reversed(cohorts):
            if cohort.is_negative:
                count += 1
            else:
                break
        return count

    @property
    def rolling_cohort_r(self) -> float:
        window = self.counted_cohorts()[-self.policy.rolling_cohort_window:]
        if not window:
            return 0.0
        return sum(cohort.r for cohort in window) / len(window)

    def _probation_cohorts(self) -> List[HealthCohort]:
        if self.probation_started_at is None:
            return []
        return [
            cohort for cohort in self.counted_cohorts()
            if cohort.cohort_id not in self._streak_baseline_cohort_ids
            and cohort.closed_at is not None
            and cohort.closed_at >= self.probation_started_at
        ]

    @property
    def probation_closed_cohorts(self) -> int:
        return len(self._probation_cohorts())

    @property
    def probation_total_r(self) -> float:
        return sum(cohort.r for cohort in self._probation_cohorts())

    @property
    def probation_symbols(self) -> set[str]:
        return {symbol for cohort in self._probation_cohorts() for symbol in cohort.symbols}

    @property
    def probation_r_without_best(self) -> float:
        values = [cohort.r for cohort in self._probation_cohorts()]
        return sum(values) - max([0.0, *values])

    def _last_counted_cohort(self) -> Optional[HealthCohort]:
        counted = self.counted_cohorts()
        return counted[-1] if counted else None

    @property
    def risk_multiplier(self) -> float:
        if not self.policy.enabled:
            return 1.0
        if self.status is HealthStatus.ACTIVE:
            return 1.0
        if self.status is HealthStatus.PROBATION:
            if self.recovery_stage >= 0:
                return float(self.policy.recovery_stages[self.recovery_stage])
            if self._extended_recovery:
                return float(self.policy.recovery_risk_multiplier)
            return float(self.policy.probation_risk_multiplier)
        return 0.0

    def allows_new_entries(self, now: Any = None) -> bool:
        """Gate for opening *new* risk. Exits are never gated (REG-01)."""
        self.evaluate(now)
        return self.status in (HealthStatus.ACTIVE, HealthStatus.PROBATION)

    def manual_lock(self, reason: str, *, at: Any = None) -> None:
        self.manual_lock_reason = str(reason)
        self._transition(HealthStatus.MANUAL_LOCK, _as_utc(at), reason="manual_lock")

    def manual_resume(self, *, approved_by: str, reason: str, at: Any = None) -> None:
        """Only exit MANUAL_LOCK through an audited, human-attributed action."""
        moment = _as_utc(at)
        self.manual_lock_reason = None
        self.failed_probation_cycles = 0
        self.recovery_stage = -1
        self.trigger_event_id = None
        self._transition(
            HealthStatus.PROBATION, moment,
            reason=f"manual_resume by {approved_by}: {reason}",
        )

    def snapshot(self) -> Dict[str, Any]:
        """Report-facing view of the current lifecycle state."""
        return {
            "strategy": self.strategy_name,
            "status": self.status.value,
            "status_changed_at": _iso(self.status_changed_at),
            "cooldown_started_at": _iso(self.cooldown_started_at),
            "cooldown_until": _iso(self.cooldown_until),
            "trigger_event_id": self.trigger_event_id,
            "trigger_reason": self.trigger_reason,
            "consecutive_negative_cohorts": self.consecutive_negative_cohorts,
            "rolling_cohort_r": self.rolling_cohort_r,
            "probation_closed_cohorts": self.probation_closed_cohorts,
            "probation_total_r": self.probation_total_r,
            "probation_symbols": sorted(self.probation_symbols),
            "probation_r_without_best": self.probation_r_without_best,
            "probation_risk_multiplier": float(
                self.policy.probation_risk_multiplier
            ),
            "failed_probation_cycles": self.failed_probation_cycles,
            "manual_lock_reason": self.manual_lock_reason,
            "automatic_failure_action": self.policy.repeated_failure_action,
            "next_recovery_at": _iso(self.cooldown_until) if self.status is HealthStatus.COOLDOWN else None,
            "recovery_requires_operator": self.status is HealthStatus.MANUAL_LOCK,
            "risk_multiplier": self.risk_multiplier,
            **({"recovery_stage": self.recovery_stage} if self.policy.recovery_stages else {}),
            "resume_count": self.resume_count,
            "total_cohorts": len(self.cohorts),
            "counted_cohorts": len(self.counted_cohorts()),
            "allows_new_entries": self.status in (
                HealthStatus.ACTIVE, HealthStatus.PROBATION
            ),
        }

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "strategy_health/v3" if self.policy.unified_recovery else "strategy_health/v2",
            "risk_multiplier": self.risk_multiplier,
            "recovery_stage": self.recovery_stage,
            "strategy": self.strategy_name,
            "status": self.status.value,
            "status_changed_at": _iso(self.status_changed_at),
            "cooldown_started_at": _iso(self.cooldown_started_at),
            "cooldown_until": _iso(self.cooldown_until),
            "trigger_event_id": self.trigger_event_id,
            "trigger_reason": self.trigger_reason,
            "probation_started_at": _iso(self.probation_started_at),
            "streak_baseline_cohort_ids": sorted(
                self._streak_baseline_cohort_ids
            ),
            "failed_probation_cycles": self.failed_probation_cycles,
            "manual_lock_reason": self.manual_lock_reason,
            "resume_count": self.resume_count,
            "last_close_at": _iso(self._last_close_at),
            "cohorts": [cohort.to_dict() for cohort in self.cohorts],
            "transitions": list(self.transitions),
            "consumed_close_event_ids": sorted(self._consumed_close_event_ids),
        }

    def load(self, data: Dict[str, Any]) -> None:
        """Restore durable lifecycle state; unknown/legacy payloads are ignored.

        ``cooldown_until`` is stored as an absolute UTC timestamp precisely so a
        restart cannot shorten or extend a cooldown (SR1-1).
        """
        if not isinstance(data, dict) or data.get("schema") not in {"strategy_health/v2", "strategy_health/v3"}:
            return
        self.reset()
        try:
            self.status = HealthStatus(str(data.get("status", "active")))
        except ValueError:
            self.status = HealthStatus.ACTIVE
        self.status_changed_at = _as_utc(data.get("status_changed_at"))
        self.cooldown_started_at = _as_utc(data.get("cooldown_started_at"))
        self.cooldown_until = _as_utc(data.get("cooldown_until"))
        self.trigger_event_id = data.get("trigger_event_id")
        self.trigger_reason = data.get("trigger_reason")
        self.probation_started_at = _as_utc(data.get("probation_started_at"))
        self.failed_probation_cycles = int(data.get("failed_probation_cycles", 0))
        self.recovery_stage = int(data.get("recovery_stage", -1))
        if self.recovery_stage >= len(self.policy.recovery_stages) or self.recovery_stage < -1:
            raise ValueError("Checkpoint recovery stage does not match configured policy")
        self.manual_lock_reason = data.get("manual_lock_reason")
        self.resume_count = int(data.get("resume_count", 0))
        self._last_close_at = _as_utc(data.get("last_close_at"))
        self.cohorts = [
            HealthCohort.from_dict(item) for item in (data.get("cohorts") or [])
        ]
        if "streak_baseline_cohort_ids" in data:
            self._streak_baseline_cohort_ids = set(
                data.get("streak_baseline_cohort_ids") or []
            )
        elif self.probation_started_at is not None:
            # Best-effort migration for v2 payloads written before the cohort
            # boundary was explicit. Same-timestamp cohorts remain new; all
            # strictly earlier observations are isolated as pre-probation.
            self._streak_baseline_cohort_ids = {
                cohort.cohort_id for cohort in self.counted_cohorts()
                if cohort.closed_at is None
                or cohort.closed_at < self.probation_started_at
            }
        self._cohort_index = {cohort.cohort_id: cohort for cohort in self.cohorts}
        self.transitions = list(data.get("transitions") or [])
        self._consumed_close_event_ids = set(
            data.get("consumed_close_event_ids") or []
        )
        if (self.policy.unified_recovery and data.get("schema") == "strategy_health/v2"
                and self.status is HealthStatus.PROBATION):
            prior = data.get("risk_multiplier")
            if prior is None:
                prior = (self.policy.recovery_risk_multiplier if self._extended_recovery
                         else self.policy.probation_risk_multiplier)
                for transition in reversed(self.transitions):
                    if transition.get("to") == HealthStatus.PROBATION.value:
                        prior = transition.get("risk_multiplier", prior)
                        break
            eligible = [i for i, value in enumerate(self.policy.recovery_stages[:-1])
                        if value <= float(prior)]
            if not eligible:
                # An older, still smaller multiplier cannot be increased by migration.
                self.status = HealthStatus.COOLDOWN
                self.cooldown_started_at = None
                self.cooldown_until = None
                self.recovery_stage = 0
            else:
                self.recovery_stage = max(eligible)
            self.probation_started_at = None
            self._streak_baseline_cohort_ids = {c.cohort_id for c in self.counted_cohorts()}
            self.trigger_reason = "legacy_probation_migrated_new_evidence_required"


def cohort_rows(machines: Sequence[StrategyHealthMachine]) -> List[Dict[str, Any]]:
    """Flatten cohorts of several strategies for ``cohort_trades.csv``."""
    return [cohort.to_dict() for machine in machines for cohort in machine.cohorts]


def transition_rows(machines: Sequence[StrategyHealthMachine]) -> List[Dict[str, Any]]:
    """Flatten transitions for ``strategy_health_timeline.csv``."""
    rows: List[Dict[str, Any]] = []
    for machine in machines:
        rows.extend(machine.transitions)
    rows.sort(key=lambda row: (row.get("at") or "", row.get("strategy") or ""))
    return rows


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
