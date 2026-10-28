"""Causal candidate gating injected explicitly into offline backtests only."""
from __future__ import annotations

from dataclasses import replace
import math
from collections.abc import Mapping
from collections import Counter

import numpy as np
import pandas as pd

from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.policy_context import (
    POLICY_CONTEXT_FEATURES, PolicyContextUnavailable, build_policy_context,
    decision_context_snapshot,
)


ACCOUNT_FEATURES = ("cash_fraction", "gross_exposure_fraction", "portfolio_drawdown",
                    "held_count_fraction", "pending_count_fraction")
POLICY_PROBABILITY_SEMANTICS = "policy_action_acceptance_probability_not_profit_probability"
SELECTOR_CONTRACT_FIELDS = {"eligibility_filter", "ranking", "return_gate", "policy_gate"}


def utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("a finite timestamp is required")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _pending_order_count(broker):
    """Count outstanding matching-book orders, including partial fills, once."""
    venue = getattr(broker, "broker", broker)
    seen = set()
    terminal = {"filled", "canceled", "cancelled", "rejected", "no_position",
                "expired", "expired_unsubmitted"}
    for name in ("pending_orders", "active_orders"):
        book = getattr(venue, name, ()) or ()
        records = book.values() if isinstance(book, Mapping) else book
        for order in records:
            value = order.get if isinstance(order, Mapping) else lambda key, default=None, order=order: getattr(order, key, default)
            status = value("status", "")
            if str(getattr(status, "value", status)).lower() in terminal:
                continue
            remaining = float(value("remaining_qty", 0.0))
            if not math.isfinite(remaining) or remaining < 0:
                raise ValueError("outstanding order quantity must be finite and nonnegative")
            if remaining > 0:
                identifier = value("id", None)
                seen.add(("order", str(identifier)) if identifier else ("object", id(order)))
    return len(seen)


class ResearchSelector:
    """Transform complete original-strategy batches, without sizing or ordering.

    Dataset labels are deliberately removed from the serving table. Actual
    fills and risk-approved sizes remain the original engine's responsibility.
    ``policy_only`` preserves the frozen RL contract: its parent predicts
    allocation scores, while the policy decides acceptance. ``policy_and_return``
    is an explicit research option requiring both the policy action and positive
    expected return. Neither changes the frozen weights or default threshold.
    """

    def __init__(self, dataset, *, mode="model", model=None, policy=None,
                 deterministic=True, seed=42, min_expected_return=0.0,
                 score_scale=100.0, score_cap=5.0, initial_capital=None,
                 policy_threshold=None, policy_gate_mode=None, selector_contract=None,
                 capital_score_source="selector_score", temporal_context=None):
        if mode not in {"model", "policy", "momentum", "random", "qualified_native"}:
            raise ValueError("unsupported selection mode")
        metadata = getattr(policy, "metadata", {}) or {}
        gate_source = ("explicit_override" if policy_gate_mode is not None else
                       "policy_metadata" if "policy_gate_mode" in metadata else "legacy_default")
        if policy_gate_mode is None:
            policy_gate_mode = metadata.get("policy_gate_mode", "policy_only")
        if policy_gate_mode not in {"policy_only", "policy_and_return"}:
            raise ValueError("policy_gate_mode must be policy_only or policy_and_return")
        if mode != "policy" and policy_gate_mode != "policy_only":
            raise ValueError("policy_and_return requires policy selection mode")
        contract = {
            "eligibility_filter": True, "ranking": mode != "qualified_native",
            "return_gate": mode == "model" or (mode == "policy" and policy_gate_mode == "policy_and_return"),
            "policy_gate": mode == "policy",
        }
        if selector_contract is not None:
            if (not isinstance(selector_contract, Mapping)
                    or set(selector_contract) != SELECTOR_CONTRACT_FIELDS
                    or any(type(value) is not bool for value in selector_contract.values())):
                raise ValueError("selector_contract requires four boolean eligibility_filter/ranking/return_gate/policy_gate fields")
            contract = dict(selector_contract)
            gate_source = "explicit_selector_contract"
        if contract["policy_gate"] and mode != "policy":
            raise ValueError("policy_gate requires policy selection mode")
        if contract["return_gate"] and mode not in {"model", "policy"}:
            raise ValueError("return_gate requires model or policy selection mode")
        if (contract["return_gate"] or (contract["ranking"] and mode in {"model", "policy"})) and model is None:
            raise ValueError("model required")
        if contract["policy_gate"] and policy is None:
            raise ValueError("policy required")
        if capital_score_source not in {"original_score", "selector_score"}:
            raise ValueError("capital_score_source must be original_score or selector_score")
        if any(isinstance(value, bool) or not math.isfinite(float(value))
               for value in (min_expected_return, score_scale, score_cap)) or score_scale <= 0 or score_cap <= 0:
            raise ValueError("invalid selection thresholds")
        if initial_capital is not None and (isinstance(initial_capital, bool)
                or not math.isfinite(float(initial_capital)) or initial_capital <= 0):
            raise ValueError("initial_capital must be finite and positive")
        self.mode, self.model, self.policy = mode, model, policy
        from research.ml_selection.temporal_context import SelectionTemporalContext
        self.temporal_context = (None if temporal_context is None else
                                 SelectionTemporalContext.from_mapping(temporal_context))
        self._active_temporal_context = None
        self.contract = contract
        self.explicit_selector_contract = selector_contract is not None
        self.selector_contract = self.contract
        self.capital_score_source = capital_score_source
        self.policy_gate_mode = ("policy_and_return" if contract["return_gate"] else "policy_only")
        self.gate_contract_source = gate_source
        self.gate_contract = (self.policy_gate_mode if contract["policy_gate"] else
                              "return_only" if contract["return_gate"] else
                              "ranking_only" if contract["ranking"] else
                              "qualification_only" if contract["eligibility_filter"] else "passthrough")
        policy_features = tuple(getattr(policy, "features", ())) if contract["policy_gate"] else ()
        unsupported = set(policy_features) - set((*FEATURE_COLUMNS, *ACCOUNT_FEATURES, *POLICY_CONTEXT_FEATURES))
        if unsupported:
            raise ValueError("unsupported policy context features: " + ", ".join(sorted(unsupported)))
        self.requested_context_features = tuple(name for name in policy_features if name in POLICY_CONTEXT_FEATURES)
        self.policy_threshold = (metadata.get("evaluation_threshold", .5)
                                 if policy_threshold is None else policy_threshold)
        if (isinstance(self.policy_threshold, bool) or
                not math.isfinite(float(self.policy_threshold)) or
                not 0 <= float(self.policy_threshold) <= 1):
            raise ValueError("policy_threshold must be in [0, 1]")
        self.policy_threshold = float(self.policy_threshold)
        self.deterministic = deterministic
        self.threshold, self.score_scale, self.score_cap = min_expected_return, score_scale, score_cap
        self.rng = np.random.default_rng(seed)
        rows = dataset.copy()
        rows["as_of"] = pd.to_datetime(rows.as_of, utc=True)
        keep = ["symbol", "as_of", "eligible", "exclusion_reason", *FEATURE_COLUMNS]
        keep += [name for name in ("bar_time", "membership_basis", "temporal_mode", "temporal_knowledge",
            "temporal_context_id", "source_versions_sha256", "feature_source_versions_sha256", "feature_snapshot_sha256", "pit_membership_verified") if name in rows]
        self.table = rows[keep].set_index(["symbol", "as_of"])
        if self.table.index.has_duplicates:
            raise ValueError("duplicate candidate snapshots")
        self.audit = []
        self.trajectory = []
        self.initial_capital = float(initial_capital) if initial_capital is not None else None
        self.high_water = self.initial_capital or 0.0

    def _record(self, candidate, facts):
        """Share ids with the original passive entry observation, if enabled."""
        attached = getattr(candidate, "audit", None)
        identifier = (attached or {}).get("decision_id") or (
            facts["bar_time"] + "|" + candidate.symbol)
        signal = getattr(candidate, "signal", None)
        signal = dict(signal) if isinstance(signal, Mapping) else None
        row = {"decision_id": identifier, "strategy": candidate.strategy_name,
               "native_score": float(candidate.score), "original_signal": signal,
               "signal": signal,
               "requested_qty": signal.get("requested_qty") if signal is not None else None,
               "original_strategy_signal": True,
               "gate_contract": self.gate_contract,
               "gate_contract_source": self.gate_contract_source,
               "selector_contract": dict(self.contract),
               "capital_score_source": self.capital_score_source,
               "return_gate_applied": False, "policy_gate_applied": False,
               "return_gate_passed": None, "policy_action": None,
               "prediction_evaluated": False, "parent_model_evaluated": False,
               "effective_gate_passed": bool(facts["selected"]),
               "effective_rejection_reasons": [] if facts["selected"] else [facts["reason"]],
               "selection_probability_semantics": (POLICY_PROBABILITY_SEMANTICS
                                                   if self.contract["policy_gate"] else None),
               **facts}
        context = self._active_temporal_context
        if context is not None and context.policy.mode == "strict":
            row.update(temporal_mode="strict", temporal_knowledge=context.policy.knowledge,
                       temporal_context_id=context.identity,
                       decision_cutoff=context.decision_cutoff.isoformat())
        self.audit.append(row)
        if attached is not None:
            attached.update({key: value for key, value in row.items() if key != "reason"})
            attached["selection_reason"] = row["reason"]
            if not row["selected"]:
                attached["reason"] = row["reason"]
        return row

    def summary(self):
        """Count observed selector stages without treating acceptance as a fill."""
        qualified = sum(row.get("data_qualified") is True for row in self.audit)
        data_rejected = sum(row.get("data_qualified") is False for row in self.audit)
        data_not_evaluated = sum(row.get("data_qualified") is None for row in self.audit)
        scored = sum(row.get("prediction_evaluated") is True for row in self.audit)
        selected = sum(row["selected"] is True for row in self.audit)
        gate_rejected = sum(row.get("data_qualified") is True and row["selected"] is False
                            for row in self.audit)
        return {"gate_contract": self.gate_contract,
                "gate_contract_source": self.gate_contract_source,
                "selector_contract": dict(self.contract),
                "capital_score_source": self.capital_score_source,
                "selection_probability_semantics": (POLICY_PROBABILITY_SEMANTICS
                                                    if self.contract["policy_gate"] else None),
                "candidate_count": len(self.audit), "data_qualified_count": qualified,
                "data_rejected_count": data_rejected,
                "data_qualification_not_evaluated_count": data_not_evaluated,
                "scored_count": scored, "selected_count": selected,
                "gate_rejected_count": gate_rejected,
                "selected_without_data_qualification_count": sum(
                    row.get("data_qualified") is None and row["selected"] is True for row in self.audit),
                "reason_counts": dict(Counter(row["reason"] for row in self.audit)),
                "selected_count_semantics": "selector_acceptance_not_order_or_fill"}

    def _decision_inputs(self, event, current_prices):
        from research.ml_selection.temporal_context import SelectionTemporalContext
        event_context = SelectionTemporalContext.from_event(event)
        context = self.temporal_context or event_context
        if (self.temporal_context is not None and event_context.policy.mode == "strict"
                and context.identity != event_context.identity):
            raise ValueError("selector context differs from the actual strict event")
        bar_time, as_of = utc(event.timestamp), context.decision_cutoff
        if as_of < bar_time + pd.Timedelta(days=1):
            raise ValueError("ML cutoff precedes signal candle close")
        self._active_temporal_context = context
        proofs, snapshots = {}, {}
        if context.policy.mode == "strict":
            from research.ml_selection.temporal_context import validate_causal_prices
            from research.ml_selection.dataset import serving_snapshots_from_sources
            proofs = getattr(event, "selection_source_versions", None)
            if proofs is None:
                proofs = {symbol: history.attrs.get("temporal_source_versions", {})
                          for symbol, history in getattr(event, "histories", {}).items()}
            validate_causal_prices(current_prices, proofs, context)
            snapshots = serving_snapshots_from_sources(proofs, context)
        return context, bar_time, as_of, proofs, snapshots

    def _account_features(self, portfolio, broker, risk_manager, current_prices):
        equity = float(portfolio.get_total_value(dict(current_prices)))
        if not math.isfinite(equity):
            raise ValueError("portfolio equity must be finite")
        capital = self.initial_capital or getattr(portfolio, "initial_capital", None)
        if capital is not None and (not math.isfinite(float(capital)) or float(capital) <= 0):
            raise ValueError("portfolio initial capital must be finite and positive")
        peak = getattr(risk_manager, "high_water_equity", None)
        if peak is not None:
            peak = float(peak)
            if not math.isfinite(peak) or peak <= 0:
                raise ValueError("risk high-water equity must be finite and positive")
            # Risk is updated on every engine bar, including blocked/no-entry
            # bars, and can explicitly rebase its peak on an approved recovery.
            self.high_water = max(peak, equity)
            drawdown = getattr(risk_manager, "last_drawdown", None)
        else:
            self.high_water = max(self.high_water, float(capital or 0.0), equity)
            drawdown = None
        if drawdown is None:
            drawdown = max(0.0, 1 - equity / self.high_water) if self.high_water > 0 else 0.0
        drawdown = float(drawdown)
        if not math.isfinite(drawdown) or drawdown < 0:
            raise ValueError("portfolio drawdown must be finite and nonnegative")
        held = [(s, p) for s, p in portfolio.positions.items() if p.get("qty", 0)]
        gross = sum(abs(float(p.get("qty", 0)) * float(current_prices.get(s, p.get("avg_price", 0))))
                    for s, p in held)
        pending_count = _pending_order_count(broker)
        account = {"cash_fraction": float(portfolio.cash) / equity if equity > 0 else 0.0,
                   "gross_exposure_fraction": gross / equity if equity > 0 else 0.0,
                   "portfolio_drawdown": drawdown,
                   "held_count_fraction": len(held) / 8.0,
                   "pending_count_fraction": pending_count / 8.0}
        return account, equity

    def _strict_snapshot_reason(self, row, candidate, context, bar_time, proofs, snapshots):
        from research.ml_selection.temporal_context import feature_snapshot_identity
        from signal_meta.signal_observation_types import fingerprint
        if (row.get("temporal_mode") != "strict" or
                row.get("temporal_knowledge") != context.policy.knowledge or
                row.get("temporal_context_id") != context.identity or
                row.get("bar_time") is None or pd.isna(row.get("bar_time")) or
                utc(row.get("bar_time")) != bar_time or
                not isinstance(row.get("source_versions_sha256"), str)):
            return "temporal_snapshot_identity_mismatch"
        matching = (candidate.symbol in proofs and
            row["source_versions_sha256"] == fingerprint(proofs[candidate.symbol]) and
            row.get("feature_snapshot_sha256") == feature_snapshot_identity(
                {**row.to_dict(), "symbol": candidate.symbol, "as_of": context.decision_cutoff}))
        actual = snapshots.get(candidate.symbol)
        if actual is None or row.get("feature_source_versions_sha256") != actual["feature_source_versions_sha256"]:
            matching = False
        if matching:
            try:
                matching = utc(actual["bar_time"]) == bar_time and bool(np.allclose(
                    [float(row[name]) for name in FEATURE_COLUMNS],
                    [float(actual[name]) for name in FEATURE_COLUMNS],
                    rtol=1e-12, atol=1e-14, equal_nan=True))
            except (TypeError, ValueError):
                matching = False
        return "" if matching else "strict_feature_or_source_identity_mismatch"

    def _serving_rows(self, candidates, *, context, bar_time, as_of, proofs, actual_snapshots,
                      model_scoring, current_prices, equity, risk_manager, account):
        needs_features = (model_scoring or self.contract["policy_gate"] or
                          (self.contract["ranking"] and self.mode == "momentum"))
        needs_snapshot = context.policy.mode == "strict" or self.contract["eligibility_filter"] or needs_features
        valid, features, eligibility, decision_contexts = [], [], [], []
        for candidate in candidates:
            key = (candidate.symbol, as_of)
            if needs_snapshot and key not in self.table.index:
                self._record(candidate, {"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                                   "symbol": candidate.symbol, "selected": False,
                                   "eligible": None, "membership_qualified": None,
                                   "data_qualified": False, "mode": self.mode,
                                   "reason": "missing_causal_snapshot", **account})
                continue
            row = self.table.loc[key] if needs_snapshot else None
            if context.policy.mode == "strict":
                reason = self._strict_snapshot_reason(row, candidate, context, bar_time,
                                                      proofs, actual_snapshots)
                if reason:
                    self._record(candidate, {"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                        "symbol": candidate.symbol, "selected": False, "eligible": False,
                        "membership_qualified": None, "data_qualified": False, "mode": self.mode,
                        "reason": reason, **account})
                    continue
            if self.contract["eligibility_filter"] and not bool(row.eligible):
                self._record(candidate, {"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                                   "symbol": candidate.symbol, "selected": False,
                                   "eligible": False, "membership_qualified": None,
                                   "data_qualified": False, "mode": self.mode,
                                   "reason": str(row.exclusion_reason), **account})
                continue
            # Disabling the experimental eligibility filter cannot manufacture
            # unavailable causal inputs. This guard is independent of liquidity.
            if needs_features and not np.isfinite(row[list(FEATURE_COLUMNS)].to_numpy(dtype=float)).all():
                self._record(candidate, {"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                                   "symbol": candidate.symbol, "selected": False,
                                   "eligible": bool(row.eligible), "membership_qualified": None,
                                   "data_qualified": False, "mode": self.mode,
                                   "reason": "feature_unavailable", **account})
                continue
            try:
                policy_context = build_policy_context(candidate,
                    requested_features=self.requested_context_features,
                    batch_candidate_count=len(candidates), current_prices=current_prices,
                    equity=equity, risk_manager=risk_manager, as_of=as_of)
            except PolicyContextUnavailable as exc:
                self._record(candidate, {"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                    "symbol": candidate.symbol, "selected": False, "eligible": bool(row.eligible),
                    "data_qualified": False, "mode": self.mode,
                    "reason": "policy_context_unavailable", "missing_context_features": list(exc.missing_features),
                    "decision_context": decision_context_snapshot(candidate, current_prices=current_prices,
                                                                  as_of=as_of, features=exc.available_features),
                    "decision_context_available_at": as_of.isoformat(), **account})
                continue
            valid.append(candidate)
            eligibility.append(bool(row.eligible) if row is not None else None)
            decision_contexts.append(decision_context_snapshot(candidate, current_prices=current_prices,
                                                              as_of=as_of, features=policy_context))
            features.append({**({name: row[name] for name in FEATURE_COLUMNS} if row is not None else {}),
                             **account, **policy_context})
        return valid, features, eligibility, decision_contexts, needs_snapshot

    def _check_artifact_times(self, as_of, model_scoring):
        context = self._active_temporal_context
        if context is None or context.policy.mode != "strict":
            return
        artifacts = ([self.model] if model_scoring else []) + ([self.policy] if self.contract["policy_gate"] else [])
        for artifact in artifacts:
            metadata = getattr(artifact, "metadata", {}) or {}
            for name in ("train_latest_label_available_at", "available_at", "trained_at"):
                if metadata.get(name) is not None:
                    bound = utc(metadata[name])
                    if bound > as_of or (name == "train_latest_label_available_at" and bound == as_of):
                        raise ValueError("model or policy training/availability exceeds decision cutoff")

    def _score_candidates(self, valid, frame, model_scoring):
        return_gates, expected_returns = None, None
        if model_scoring:
            predictions = np.asarray(self.model.predict(frame), dtype=float)
            if predictions.shape != (len(valid),) or not np.isfinite(predictions).all():
                raise ValueError("nonfinite or malformed model predictions")
            expected_returns = (np.asarray(self.model.predict_net_return(frame), dtype=float)
                                if getattr(self.model, "kind", None) == "lambdarank"
                                else predictions)
            if expected_returns.shape != (len(valid),) or not np.isfinite(expected_returns).all():
                raise ValueError("nonfinite or malformed expected net returns")
            scaled = predictions * self.score_scale
            scores = np.clip(self.score_cap * .5 * (1 + scaled / (1 + np.abs(scaled))), .01, self.score_cap)
            return_gates = expected_returns > self.threshold
            gates = return_gates if self.contract["return_gate"] else np.ones(len(valid), dtype=bool)
        elif self.contract["ranking"] and self.mode == "momentum":
            predictions = frame["return_20d"].to_numpy(dtype=float)
            scaled = predictions * self.score_scale
            scores = np.clip(self.score_cap * .5 * (1 + scaled / (1 + np.abs(scaled))), .01, self.score_cap)
            gates = np.ones(len(valid), dtype=bool)
        elif self.contract["ranking"] and self.mode == "random":
            predictions = self.rng.random(len(valid))
            scores, gates = predictions * self.score_cap, np.ones(len(valid), dtype=bool)
        else:
            predictions = np.array([c.score for c in valid], dtype=float)
            scores, gates = predictions, np.ones(len(valid), dtype=bool)
        return predictions, expected_returns, scores, gates, return_gates

    def _policy_choices(self, valid, frame, *, bar_time, as_of, gates, return_gates, decision_contexts):
        probabilities = np.ones(len(valid))
        policy_actions = None
        if self.contract["policy_gate"]:
            options = {"deterministic": self.deterministic}
            # Preserve compatibility with older policy adapters at the old
            # default, while requiring explicit support for changed thresholds.
            if self.policy_threshold != .5:
                options["threshold"] = self.policy_threshold
            policy_actions, probabilities = self.policy.act(frame, **options)
            policy_actions, probabilities = np.asarray(policy_actions), np.asarray(probabilities, dtype=float)
            if (policy_actions.shape != (len(valid),) or probabilities.shape != (len(valid),)
                    or not np.isin(policy_actions, [False, True]).all()
                    or not np.isfinite(probabilities).all()
                    or np.any((probabilities < 0) | (probabilities > 1))):
                raise ValueError("malformed policy actions or probabilities")
            policy_actions = policy_actions.astype(bool)
            gates = policy_actions & gates
            if not self.deterministic:
                for i in range(len(valid)):
                    self.trajectory.append({"bar_time": bar_time, "as_of": as_of,
                                            "decision_id": (getattr(valid[i], "audit", None) or {}).get("decision_id")
                                                or (bar_time.isoformat() + "|" + valid[i].symbol),
                                            "features": {name: float(frame.iloc[i][name])
                                                         for name in self.policy.features},
                                            # The gradient consumes the sampled action,
                                            # not the later execution/return gate.
                                            "action": bool(policy_actions[i]),
                                            "sampled_action": bool(policy_actions[i]),
                                            "effective_action": bool(gates[i]),
                                            "effective_gate_passed": bool(gates[i]),
                                            "return_gate_applied": self.contract["return_gate"],
                                            "return_gate_passed": bool(return_gates[i]) if return_gates is not None else None,
                                            "original_candidate_score": float(valid[i].score),
                                            "original_candidate_signal": (dict(valid[i].signal)
                                                                          if isinstance(valid[i].signal, Mapping) else None),
                                            "decision_context": decision_contexts[i],
                                            "decision_context_available_at": as_of.isoformat(),
                                            "credit_assignment": "episode_account_return_to_go_not_individual_pnl",
                                            "probability": float(probabilities[i])})
        return gates, probabilities, policy_actions

    def _accepted_candidates(self, valid, *, predictions, expected_returns, scores, gates,
                             probabilities, policy_actions, return_gates, eligibility,
                             decision_contexts, model_scoring, needs_snapshot, bar_time, as_of, account):
        selected = []
        return_applied = self.contract["return_gate"]
        for i, candidate in enumerate(valid):
            failures = []
            if return_applied and not return_gates[i]:
                failures.append("return_gate")
            if policy_actions is not None and not policy_actions[i]:
                failures.append("policy_gate")
            reason = ("selected" if gates[i] else "model_gate" if self.mode == "model"
                      else "policy_and_return_gate" if len(failures) == 2 else failures[0])
            self._record(candidate, {"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                               "symbol": candidate.symbol, "strategy": candidate.strategy_name,
                               "native_score": float(candidate.score),
                               "predicted_value": float(predictions[i]),
                               "ranking_score": float(predictions[i]),
                               "expected_net_return": float(expected_returns[i]) if model_scoring else None,
                               "return_gate_threshold": float(self.threshold) if model_scoring else None,
                               "eligible": eligibility[i], "data_qualified": True if needs_snapshot else None,
                               "membership_qualified": None,
                               "allocation_score": (float(scores[i]) if self.contract["ranking"]
                                                    and self.capital_score_source == "selector_score" else float(candidate.score)),
                               "selection_rank_score": float(scores[i]) if self.contract["ranking"] else None,
                               "selection_probability": float(probabilities[i]),
                               "selected": bool(gates[i]), "mode": self.mode,
                               "decision_context": decision_contexts[i],
                               "decision_context_available_at": as_of.isoformat(),
                               "prediction_evaluated": model_scoring or self.contract["policy_gate"] or self.contract["ranking"],
                               "parent_model_evaluated": model_scoring,
                               "return_gate_applied": return_applied,
                               "policy_gate_applied": self.contract["policy_gate"],
                               "return_gate_passed": bool(return_gates[i]) if return_gates is not None else None,
                               "policy_action": bool(policy_actions[i]) if policy_actions is not None else None,
                               "effective_rejection_reasons": failures,
                               "policy_threshold": self.policy_threshold if self.mode == "policy" else None,
                               "deterministic": bool(self.deterministic) if self.mode == "policy" else None,
                               "reason": reason,
                               **account})
            if gates[i]:
                if not self.contract["ranking"]:
                    selected.append(candidate)
                elif self.capital_score_source == "original_score":
                    selected.append(replace(candidate, selection_rank_score=float(scores[i])))
                elif self.explicit_selector_contract:
                    # Keep ranking fixed across capital-score ablations. The
                    # smart planner otherwise picks slots by score/volatility.
                    selected.append(replace(candidate, score=float(scores[i]),
                                            selection_rank_score=float(scores[i])))
                else:
                    selected.append(replace(candidate, score=float(scores[i])))
        return selected

    def select(self, candidates, *, event, portfolio, broker, risk_manager, current_prices):
        candidates = list(candidates)
        if event.timeframe != "1d":
            raise ValueError("ML selection requires daily closed bars")
        context, bar_time, as_of, proofs, snapshots = self._decision_inputs(event, current_prices)
        account, equity = self._account_features(portfolio, broker, risk_manager, current_prices)
        if not candidates:
            return []
        model_scoring = self.mode in {"model", "policy"} and (
            self.contract["ranking"] or self.contract["return_gate"])
        valid, features, eligibility, decision_contexts, needs_snapshot = self._serving_rows(
            candidates, context=context, bar_time=bar_time, as_of=as_of, proofs=proofs,
            actual_snapshots=snapshots, model_scoring=model_scoring, current_prices=current_prices,
            equity=equity, risk_manager=risk_manager, account=account)
        if not valid:
            return []
        self._check_artifact_times(as_of, model_scoring)
        frame = pd.DataFrame(features)
        predictions, expected_returns, scores, gates, return_gates = self._score_candidates(valid, frame, model_scoring)
        gates, probabilities, policy_actions = self._policy_choices(valid, frame,
            bar_time=bar_time, as_of=as_of, gates=gates, return_gates=return_gates,
            decision_contexts=decision_contexts)
        return self._accepted_candidates(valid, predictions=predictions, expected_returns=expected_returns,
            scores=scores, gates=gates, probabilities=probabilities, policy_actions=policy_actions,
            return_gates=return_gates, eligibility=eligibility, decision_contexts=decision_contexts,
            model_scoring=model_scoring, needs_snapshot=needs_snapshot,
            bar_time=bar_time, as_of=as_of, account=account)
