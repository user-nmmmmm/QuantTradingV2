"""Causal, version-isolated EV diagnostics; this module cannot authorise orders.

Probabilities are frozen at the signal's entry stamp, not reassigned when its
outcome arrives. Uncertainty is an approximate, deliberately conservative
research diagnostic, not an independence claim or a calibrated confidence level.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Any, Mapping

from core.signal_observation_types import canonical, fingerprint
from core.timeframes import as_utc_timestamp, timeframe_delta

if TYPE_CHECKING:
    from core.signal_ev_types import EVPolicy


_SCOPE = "independent_fixed_notional_signal_diagnostic"
_BOOK_FIELDS = ("strategy", "signal_version", "direction", "timeframe",
                "snapshot_version", "horizon_bars")


def _timestamp(value: Any, name: str):
    try:
        result = as_utc_timestamp(value)
        if not math.isfinite(result.timestamp()):
            raise ValueError(name)
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a valid finite timestamp") from exc


def _horizon(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("horizon must be a positive integer")
    return value


def _identity(candidate: Mapping, horizon: int) -> tuple:
    context = candidate.get("context")
    if not isinstance(context, Mapping):
        raise ValueError("candidate requires a context snapshot")
    values = (candidate.get("strategy"), candidate.get("signal_version"),
              candidate.get("direction"), context.get("timeframe"),
              context.get("snapshot_version"))
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("candidate requires nonempty strategy/version/direction/timeframe/snapshot")
    if values[2] not in {"long", "short"}:
        raise ValueError("candidate direction must be long or short")
    timeframe_delta(values[3])
    return (*values, _horizon(horizon))


def _memberships(value: Mapping, *, allow_empty: bool = False) -> tuple:
    if not isinstance(value, Mapping) or (not value and not allow_empty):
        raise ValueError("memberships require at least one named axis")
    if any(not isinstance(axis, str) or not axis.strip() for axis in value):
        raise ValueError("membership axis labels must be nonempty strings")
    frozen = []
    for axis, states in sorted(value.items()):
        if not isinstance(axis, str) or not axis.strip() or not isinstance(states, Mapping) or not states:
            raise ValueError("each membership axis requires named states")
        if any(not isinstance(state, str) or not state.strip() for state in states):
            raise ValueError("membership state labels must be nonempty strings")
        numbers = []
        for state, probability in sorted(states.items()):
            if not isinstance(state, str) or not state.strip() or isinstance(probability, bool):
                raise ValueError("membership labels and probabilities are invalid")
            try:
                number = float(probability)
            except (ValueError, TypeError) as exc:
                raise ValueError("membership probabilities must be finite and nonnegative") from exc
            if not math.isfinite(number) or number < 0:
                raise ValueError("membership probabilities must be finite and nonnegative")
            numbers.append((state, number))
        if not math.isclose(math.fsum(number for _, number in numbers), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("membership probabilities must sum to one on every axis")
        frozen.append((axis, tuple(numbers)))
    return tuple(frozen)


@dataclass(frozen=True)
class _Observation:
    candidate_id: str
    book: tuple
    entry: Any
    label_at: Any
    outcome_bps: float
    memberships: tuple
    digest: str


class EVLedger:
    """An in-memory, append-only research ledger with strictly causal queries."""

    def __init__(self, policy: EVPolicy):
        self.policy = policy
        self._records: dict[tuple[str, int], _Observation] = {}
        self._books: dict[tuple, list[_Observation]] = defaultdict(list)

    def add(self, candidate: dict, outcome: dict,
            memberships: dict[str, dict[str, float]], *, cutoff) -> bool:
        """Append an already available, executable label; exact replays are no-ops."""
        book = _identity(candidate, outcome.get("horizon_bars"))
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate_id must be a nonempty string")
        if outcome.get("candidate_id") != candidate_id:
            raise ValueError("outcome candidate_id does not match candidate")
        for name in ("strategy", "direction", "symbol"):
            if name in outcome and outcome[name] != candidate.get(name):
                raise ValueError(f"outcome {name} does not match candidate")
        if outcome.get("status") != "matured" or outcome.get("scope") != _SCOPE:
            raise ValueError("only matured independent fixed-notional labels are admissible")
        if outcome.get("execution_flags") not in ([], ()):
            raise ValueError("outcome execution_flags must be explicitly empty")
        raw_return = outcome.get("net_return_bps")
        try:
            return_bps = float(raw_return)
        except (TypeError, ValueError) as exc:
            raise ValueError("net_return_bps must be finite") from exc
        if isinstance(raw_return, bool) or not math.isfinite(return_bps):
            raise ValueError("net_return_bps must be finite")
        entry = _timestamp(candidate["context"].get("available_at"), "entry available_at")
        label_at = _timestamp(outcome.get("available_at"), "label available_at")
        cutoff_at = _timestamp(cutoff, "cutoff")
        if label_at <= entry or label_at >= cutoff_at:
            raise ValueError("label availability must be after entry and strictly before cutoff")
        if label_at < entry + book[-1] * timeframe_delta(book[3]):
            raise ValueError("fixed-horizon label cannot mature before its horizon has elapsed")
        frozen_memberships = _memberships(memberships)
        try:
            digest = fingerprint({"candidate": candidate, "outcome": outcome,
                                  "memberships": frozen_memberships})
        except (TypeError, ValueError) as exc:
            raise ValueError("ledger facts must be finite, serialisable immutable snapshots") from exc
        key = (candidate_id, book[-1])
        if key in self._records:
            if self._records[key].digest != digest:
                raise ValueError("conflicting replay for the same candidate_id and horizon")
            return False
        observation = _Observation(candidate_id, book, entry, label_at,
                                   return_bps, frozen_memberships, digest)
        self._records[key] = observation
        self._books[book].append(observation)
        return True

    def _eligible(self, book: tuple, at) -> list[_Observation]:
        return [row for row in self._books.get(book, ()) if row.label_at < at]

    def _stats(self, observations: list[_Observation], book: tuple, at,
               *, axis: str | None = None, state: str | None = None) -> dict:
        horizon_days = book[-1] * timeframe_delta(book[3]).total_seconds() / 86400
        block_days = max(float(self.policy.block_days), horizon_days)
        values = []
        for row in observations:
            probability = 1.0
            if axis is not None:
                probability = dict(dict(row.memberships).get(axis, ())).get(state, 0.0)
            if probability <= 0:
                continue
            block = math.floor(row.entry.timestamp() / (block_days * 86400))
            values.append((probability, row.outcome_bps, block, row.label_at, row.entry))
        # Normalise at the freshest entry, not at query time. Absolute weights
        # can underflow (or their squares can) even when relative information is
        # perfectly usable. This keeps ESS invariant to common wall-clock aging;
        # absolute decayed mass remains separate and prevents stale admission.
        relative_values = []
        absolute_scale = 0.0
        if values:
            freshest = max(row[4] for row in values)
            log_weights = [math.log2(probability) - (freshest - entry).total_seconds()
                           / 86400 / self.policy.half_life_days
                           for probability, _, _, _, entry in values]
            max_log_weight = max(log_weights)  # freshest entry has finite log(p)
            relative_values = [(2.0 ** (log_weight - max_log_weight), *row[1:])
                               for row, log_weight in zip(values, log_weights)]
            age_days = (at - freshest).total_seconds() / 86400
            absolute_scale = 2.0 ** (max_log_weight - age_days / self.policy.half_life_days)
        relative_mass = math.fsum(row[0] for row in relative_values)
        mass = absolute_scale * relative_mass
        relative_mass2 = math.fsum(row[0] ** 2 for row in relative_values)
        groups = defaultdict(float)
        for weight, _, block, _, _ in relative_values:
            groups[block] += weight
        group_mass2 = math.fsum(weight ** 2 for weight in groups.values())
        effective_n = relative_mass ** 2 / relative_mass2 if relative_mass2 else 0.0
        effective_blocks = relative_mass ** 2 / group_mass2 if group_mass2 else 0.0
        block_count = sum(weight > 0 for weight in groups.values())
        mean = (math.fsum((weight / relative_mass) * result
                          for weight, result, _, _, _ in relative_values) if relative_mass else None)
        variance = None
        individual_se = cluster_se = floor_se = stderr = None
        if relative_mass and mean is not None:
            variance_denom = relative_mass - relative_mass2 / relative_mass
            variance = (math.fsum(weight * (result - mean) ** 2
                                  for weight, result, _, _, _ in relative_values) / variance_denom
                        if variance_denom > 0 else 0.0)
            individual_se = math.sqrt(max(0.0, variance) / effective_n)
            residuals = defaultdict(float)
            for weight, result, block, _, _ in relative_values:
                residuals[block] += weight * (result - mean)
            cluster_variance = (block_count / (block_count - 1)
                                * math.fsum(value ** 2 for value in residuals.values()) / relative_mass ** 2
                                if block_count > 1 else 0.0)
            cluster_se = math.sqrt(max(0.0, cluster_variance))
            floor_se = self.policy.min_std_bps / math.sqrt(max(1.0, effective_blocks))
            stderr = max(individual_se, cluster_se, floor_se)
        return {
            "raw_count": len(values), "weight_mass": mass,
            "effective_samples": effective_n, "effective_blocks": effective_blocks,
            "block_count": block_count, "block_days": block_days,
            "mean_bps": mean, "variance_bps2": variance,
            "individual_stderr_bps": individual_se,
            "cluster_stderr_bps": cluster_se, "floor_stderr_bps": floor_se,
            "stderr_bps": stderr,
            "max_label_available_at": max(row[3] for row in values).isoformat() if values else None,
            "first_entry_at": min(row[4] for row in values).isoformat() if values else None,
            "last_entry_at": max(row[4] for row in values).isoformat() if values else None,
        }

    def _support_reason(self, stats: dict) -> str | None:
        if not stats["raw_count"]:
            return "cold_start"
        if stats["effective_samples"] < self.policy.min_effective_samples:
            return "insufficient_samples"
        if stats["effective_blocks"] < self.policy.min_effective_blocks:
            return "insufficient_blocks"
        if stats["weight_mass"] < self.policy.min_weight_mass:
            return "stale_weight_mass"
        return None

    def _shrinkage_alpha(self, mass: float, raw_count: int) -> float:
        if self.policy.prior_strength == 0:
            return 1.0 if raw_count else 0.0
        return mass / (mass + self.policy.prior_strength)

    @staticmethod
    def _reason(reasons: list[str]) -> str | None:
        for reason in ("unknown_context", "cold_start", "insufficient_samples",
                       "insufficient_blocks", "stale_weight_mass"):
            if reason in reasons:
                return reason
        return None

    def query(self, candidate: dict, memberships: dict[str, dict[str, float]],
              horizon: int, *, as_of) -> dict:
        """Score a frozen entry context without advancing or mutating the ledger."""
        book = _identity(candidate, horizon)
        at = _timestamp(as_of, "as_of")
        entry = _timestamp(candidate["context"].get("available_at"), "entry available_at")
        if at < entry:
            raise ValueError("a query cannot precede its candidate context availability")
        frozen_memberships = _memberships(memberships, allow_empty=True)
        observations = self._eligible(book, at)
        prior = self._stats(observations, book, at)
        reasons = []
        prior_reason = self._support_reason(prior)
        if prior_reason:
            reasons.append(prior_reason)
        if not frozen_memberships:
            reasons.append("unknown_context")
        axes = {}
        all_cells = []
        for axis, states in frozen_memberships:
            cells = {}
            axis_reasons = []
            for state, probability in states:
                if probability <= 0:
                    continue
                stats = self._stats(observations, book, at, axis=axis, state=state)
                reason = self._support_reason(stats)
                if state == "unknown":
                    reason = "unknown_context"
                elif observations and not any(axis in dict(row.memberships) for row in observations):
                    reason = "unknown_context"
                if reason:
                    axis_reasons.append(reason)
                mass = stats["weight_mass"]
                alpha = self._shrinkage_alpha(mass, stats["raw_count"])
                estimate = stderr = None
                if stats["mean_bps"] is not None and prior["mean_bps"] is not None:
                    estimate = alpha * stats["mean_bps"] + (1 - alpha) * prior["mean_bps"]
                    # The cell belongs to the prior: their errors are not independent.
                    stderr = alpha * stats["stderr_bps"] + (1 - alpha) * prior["stderr_bps"]
                cells[state] = {**stats, "probability": probability,
                                "shrinkage_alpha": alpha, "estimate_bps": estimate,
                                "shrunk_stderr_bps": stderr,
                                "support_reason": reason}
                all_cells.append(stats)
            estimates_known = all(cell["estimate_bps"] is not None for cell in cells.values())
            axis_estimate = (math.fsum(cell["probability"] * cell["estimate_bps"] for cell in cells.values())
                             if estimates_known else None)
            axis_stderr = (math.fsum(cell["probability"] * cell["shrunk_stderr_bps"] for cell in cells.values())
                           if estimates_known else None)
            axes[axis] = {"estimate_bps": axis_estimate, "stderr_bps": axis_stderr,
                          "support_reason": self._reason(axis_reasons), "states": cells}
            reasons.extend(axis_reasons)
        means_known = bool(axes) and all(axis["estimate_bps"] is not None for axis in axes.values())
        estimate = (math.fsum(axis["estimate_bps"] for axis in axes.values()) / len(axes)
                    if means_known else None)
        stderr = (math.fsum(axis["stderr_bps"] for axis in axes.values()) / len(axes)
                  if means_known else None)
        lower = estimate - self.policy.confidence_z * stderr if estimate is not None else None
        upper = estimate + self.policy.confidence_z * stderr if estimate is not None else None
        reason = self._reason(reasons)
        allow = None if reason or lower is None else lower > self.policy.min_ev_bps
        return {
            "book": dict(zip(_BOOK_FIELDS, book)), "as_of": at.isoformat(),
            "estimate_bps": estimate, "stderr_bps": stderr,
            "lower_bound_bps": lower, "upper_bound_bps": upper,
            # A weak nonzero mixture component cannot hide behind stronger cells.
            **{key: min((cell[key] for cell in all_cells), default=0)
               for key in ("effective_samples", "effective_blocks", "weight_mass", "raw_count")},
            "status": "abstain" if allow is None else "allow" if allow else "veto",
            "reason": reason or ("lower_bound_positive" if allow else "lower_bound_not_positive"),
            "would_allow": allow, "prior": prior, "axes": axes,
            "max_label_available_at": prior["max_label_available_at"],
            "axis_weighting": "uniform_additive",
            "uncertainty_scope": "approximate_time_clustered_diagnostic_not_calibrated_ci",
        }

    def cell_rows(self, *, as_of) -> list[dict]:
        """Return causal per-book/per-state diagnostics, never future cell names."""
        at = _timestamp(as_of, "as_of")
        rows = []
        for book in sorted(self._books):
            observations = self._eligible(book, at)
            if not observations:
                continue
            prior = self._stats(observations, book, at)
            keys = sorted({(axis, state) for row in observations
                           for axis, states in row.memberships
                           for state, probability in states if probability > 0})
            for axis, state in keys:
                stats = self._stats(observations, book, at, axis=axis, state=state)
                alpha = self._shrinkage_alpha(stats["weight_mass"], stats["raw_count"])
                estimate = (alpha * stats["mean_bps"] + (1 - alpha) * prior["mean_bps"]
                            if stats["mean_bps"] is not None else None)
                stderr = (alpha * stats["stderr_bps"] + (1 - alpha) * prior["stderr_bps"]
                          if stats["stderr_bps"] is not None else None)
                rows.append({**dict(zip(_BOOK_FIELDS, book)), "axis": axis, "state": state,
                             "as_of": at.isoformat(), **stats,
                             "shrinkage_alpha": alpha, "estimate_bps": estimate,
                             "shrunk_stderr_bps": stderr, "prior_mean_bps": prior["mean_bps"],
                             "support_reason": "unknown_context" if state == "unknown" else self._reason(
                                 [reason for reason in (self._support_reason(stats), self._support_reason(prior))
                                  if reason])})
        # Exercise the same JSON-safety contract as exported P0/P1 reports.
        canonical(rows)
        return rows
