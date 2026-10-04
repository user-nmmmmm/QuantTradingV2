"""Same-batch allocation counterfactuals through the original allocator.

Only candidate scores change. Each arm receives an isolated copy of the whole
strategy/account/order/risk/allocator graph. Approval is observed; no historical
matching is run, and these results cannot establish fill or return differences.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, replace
from threading import RLock
import math

import numpy as np

from backtest.execution_adapter import SimulatedExecutionAdapter
from core.allocation import PortfolioSignalAllocator
from core.broker import Broker


def _venue(broker):
    return broker.broker if isinstance(broker, SimulatedExecutionAdapter) else broker


def copy_allocation_graph(candidates, context, allocator):
    """Copy one connected graph, preserving every mutable shared reference.

    The backtest's adapter forwards missing attributes and cannot be copied by
    Python's generic reconstruction. Seed its shell and independent lock copies
    before copying. Already frozen event envelopes may safely be shared.
    """
    broker = context["broker"]
    venue = _venue(broker)
    if not isinstance(venue, Broker):
        raise ValueError("batch comparison requires the original offline Broker")
    pipeline = venue.event_pipeline
    if pipeline.store is not None:
        raise ValueError("batch comparison cannot copy a broker with an external event store")
    for callbacks in pipeline._subscribers.values():
        if any(getattr(callback, "__self__", None) is None for callback in callbacks):
            raise ValueError("batch comparison cannot isolate external callback closures")
    if getattr(allocator.risk_governor, "_state_store", None) is not None or getattr(allocator.risk_governor, "_order_store", None) is not None:
        raise ValueError("batch comparison requires an allocator without external persistence")
    memo = {id(pipeline._transaction_lock): RLock(), id(venue.reservation_projection._lock): RLock()}
    # EventEnvelope deep-freezes every payload at publication. It contains no
    # mutable allocator state; sharing those facts avoids copying mappingproxy.
    for event in pipeline._by_id.values():
        memo[id(event)] = event
    if isinstance(broker, SimulatedExecutionAdapter):
        shell = object.__new__(type(broker))
        memo[id(broker)] = shell
        shell.__dict__.update(deepcopy(broker.__dict__, memo))
    graph = deepcopy({"candidates": list(candidates), "context": dict(context), "allocator": allocator}, memo)
    copied_broker = _venue(graph["context"]["broker"])
    if copied_broker.portfolio is not graph["context"]["portfolio"]:
        raise ValueError("copied broker/account graph lost the original shared portfolio")
    if copied_broker is venue or graph["context"]["portfolio"] is context["portfolio"]:
        raise ValueError("allocation comparison did not isolate mutable account state")
    return graph


def _orders(broker):
    venue = _venue(broker)
    return dict(venue.orders_by_id)


def _candidate_key(candidate):
    return f"{candidate.strategy_name}|{candidate.symbol}"


class BatchAllocationComparisonSelector:
    """Record bounded allocation-only comparisons before the real selector.

    ``score_provider(candidates, context)`` returns finite momentum/model score
    sequences for the exact candidate order. ``qualification_provider`` may
    provide one shared eligible subset before all arms. ``allocator_provider``
    may return the running engine's actual allocator. A configured template is
    explicitly weaker evidence about carried allocator state.
    """

    def __init__(self, selector, *, score_provider, allocator=None, allocator_provider=None,
                 qualification_provider=None, seed=42, max_batches=12,
                 allocator_state_source="configured_template"):
        if allocator is None and allocator_provider is None:
            raise ValueError("an original allocator or allocator_provider is required")
        if allocator is not None and not isinstance(allocator, PortfolioSignalAllocator):
            raise ValueError("batch comparisons must use PortfolioSignalAllocator")
        if isinstance(max_batches, bool) or not isinstance(max_batches, int) or max_batches < 1:
            raise ValueError("max_batches must be a positive integer")
        self.selector = selector
        self.score_provider = score_provider
        self.qualification_provider = qualification_provider
        self.allocator, self.allocator_provider = allocator, allocator_provider
        self.allocator_state_source = allocator_state_source
        self.rng = np.random.default_rng(seed)
        self.seed, self.max_batches = int(seed), max_batches
        self.comparisons = []
        self.competitive_batches_seen = 0

    def __getattr__(self, name):
        selector = self.__dict__.get("selector")
        if selector is None:
            raise AttributeError(name)
        return getattr(selector, name)

    def _compare(self, candidates, context):
        supplied = self.score_provider(candidates, context)
        if not isinstance(supplied, Mapping) or not {"momentum", "model"} <= set(supplied):
            raise ValueError("score_provider must supply momentum and model scores")
        scores = {"native": np.asarray([candidate.score for candidate in candidates], dtype=float),
                  "momentum": np.asarray(supplied["momentum"], dtype=float),
                  "model": np.asarray(supplied["model"], dtype=float),
                  "random": self.rng.random(len(candidates))}
        if any(values.shape != (len(candidates),) or not np.isfinite(values).all() for values in scores.values()):
            raise ValueError("all comparison scores must be finite and cover the same eligible batch")
        allocator = self.allocator_provider(context) if self.allocator_provider else self.allocator
        if not isinstance(allocator, PortfolioSignalAllocator):
            raise ValueError("allocator_provider must return PortfolioSignalAllocator")
        event = context["event"]
        batch = {"timestamp": str(event.timestamp), "candidate_count": len(candidates),
                 "candidate_keys": [_candidate_key(candidate) for candidate in candidates],
                 "qualification": "same_original_candidates_for_all_arms", "seed": self.seed,
                 "allocator_state_source": self.allocator_state_source,
                 "estimand": "same_batch_order_approval_and_quantity_only",
                 "matching_executed": False, "portfolio_return_difference": None,
                 "status": "compared", "arms": {}}
        for arm, values in scores.items():
            graph = copy_allocation_graph(candidates, context, allocator)
            account, broker, risk = (graph["context"][key] for key in ("portfolio", "broker", "risk_manager"))
            copied_allocator = graph["allocator"]
            rescored = [replace(candidate, score=float(score)) for candidate, score in zip(graph["candidates"], values)]
            previous_ids = set(_orders(broker))
            fills_before = len(_venue(broker).trades)
            decisions = copied_allocator.allocate(rescored, portfolio=account, broker=broker,
                                                   risk_manager=risk, current_prices=graph["context"]["current_prices"])
            if len(_venue(broker).trades) != fills_before:
                raise ValueError("allocation comparison unexpectedly executed matching")
            new_orders = [order for key, order in _orders(broker).items() if key not in previous_ids]
            approvals = [{"order_id": order.id, "candidate_key": f"{order.strategy_id}|{order.symbol}",
                          "symbol": order.symbol, "strategy": order.strategy_id, "side": order.side,
                          "qty": float(order.qty), "reference_price": float(order.price or 0.),
                          "status": order.status.value, "approved": bool(order.accepted),
                          "approved_risk_amount": order.intent.approved_risk_amount if order.intent else None}
                         for order in new_orders]
            approved = {row["candidate_key"]: row["qty"] for row in approvals if row["approved"]}
            rejected = [asdict(decision) for decision in decisions if not decision.accepted]
            candidate_audits = {_candidate_key(candidate): deepcopy(candidate.audit or {}) for candidate in rescored}
            limited = any(
                float((decision.capital_allocation or {}).get("approved_qty", 0.)) + 1e-9 < float((decision.capital_allocation or {}).get("requested_qty", 0.))
                or (decision.capital_allocation or {}).get("reason") in {"position_slots_exhausted", "below_minimum_allocation", "unverifiable_shared_risk_budget"}
                or (decision.risk_budget or {}).get("scale", 1.) < 1.
                for decision in decisions) or any(
                    float((audit.get("correlated_budget") or {}).get("scale", 1.)) < 1.
                    or (audit.get("clamped_qty") is not None and audit.get("sized_qty") is not None
                        and float(audit["clamped_qty"]) + 1e-9 < float(audit["sized_qty"]))
                    for audit in candidate_audits.values())
            batch["arms"][arm] = {"scores": values.tolist(), "decisions": [asdict(decision) for decision in decisions],
                "orders": approvals, "approved_quantities": approved, "rejected_candidates": rejected,
                "budget_limited": limited, "candidate_audits": candidate_audits,
                "capital_summary": deepcopy(copied_allocator.last_capital_summary),
                "risk_budget_summary": {"session": str(copied_allocator.risk_governor._session),
                    "session_risk_after": copied_allocator.risk_governor.session_risk_used,
                    "policy": asdict(copied_allocator.risk_governor.policy)},
                "additional_fill_count": 0}
        native = batch["arms"]["native"]["approved_quantities"]
        for arm, facts in batch["arms"].items():
            actual = facts["approved_quantities"]
            facts["changed_selected_set_from_native"] = set(actual) != set(native)
            facts["changed_quantity_from_native"] = any(not math.isclose(actual.get(key, 0.), native.get(key, 0.), rel_tol=1e-9, abs_tol=1e-9)
                                                         for key in set(actual) | set(native))
        batch["ranking_changed_approval_or_quantity"] = any(row["changed_selected_set_from_native"] or row["changed_quantity_from_native"]
                                                             for arm, row in batch["arms"].items() if arm != "native")
        return batch

    def select(self, candidates, **context):
        original = list(candidates)
        eligible = self.qualification_provider(original, context) if self.qualification_provider else original
        eligible = list(eligible)
        if any(not any(item is candidate for candidate in original) for item in eligible):
            raise ValueError("shared qualification must preserve original candidate objects")
        if len(eligible) > 1:
            self.competitive_batches_seen += 1
            if len(self.comparisons) < self.max_batches:
                try:
                    self.comparisons.append(self._compare(eligible, context))
                except (ValueError, TypeError, AttributeError, RecursionError) as exc:
                    # A diagnostic failure never changes the actual selector.
                    self.comparisons.append({"timestamp": str(context["event"].timestamp),
                        "candidate_count": len(eligible), "status": "unmeasured", "reason": str(exc),
                        "matching_executed": False, "portfolio_return_difference": None})
        return self.selector.select(original, **context)

    def comparison_summary(self):
        measured = [batch for batch in self.comparisons if batch["status"] == "compared"]
        return {"schema": "ml-selection-same-batch-allocation/v1", "max_batches": self.max_batches,
                "seed": self.seed, "competitive_batches_seen": self.competitive_batches_seen,
                "recorded_batches": len(self.comparisons), "measured_batches": len(measured),
                "unmeasured_batches": len(self.comparisons) - len(measured),
                "ranking_changed_approval_or_quantity_batches": sum(batch["ranking_changed_approval_or_quantity"] for batch in measured),
                "budget_limited_batches": sum(any(arm["budget_limited"] for arm in batch["arms"].values()) for batch in measured),
                "allocation_state_source": self.allocator_state_source,
                "portfolio_return_difference": None, "matching_executed": False,
                "batches": self.comparisons}
