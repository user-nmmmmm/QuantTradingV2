"""Read-only causal decision, execution and reward diagnostics.

Ids come from observations, orders and authoritative lot facts. Archived
symbol/time coincidences are never promoted to execution linkage.
"""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json, sha256_file
from research.ml_selection.environment import _actual_trades, _evaluated_curve, equity_rewards


def _records(value):
    if value is None:
        return []
    return value.to_dict("records") if isinstance(value, pd.DataFrame) else [dict(x) for x in value]


def _missing(value):
    return value is None or (isinstance(value, (float, np.floating)) and not math.isfinite(value))


def _number(value):
    if _missing(value) or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _structure(value, default):
    if _missing(value) or value == "":
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return default
    return value


def _stamp(value):
    if _missing(value) or value == "":
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _bool(value):
    if _missing(value):
        return None
    if isinstance(value, str):
        return {"true": True, "false": False}.get(value.lower())
    return bool(value)


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.generic):
        return _clean(value.item())
    return None if _missing(value) else value


def _date_counts(rows):
    dates = Counter()
    for row in rows:
        time = _stamp(row.get("bar_time", row.get("timestamp")))
        if time is not None:
            dates[time.isoformat()] += 1
    return {"candidates": len(rows), "candidate_dates": len(dates),
            "competition_dates": sum(n > 1 for n in dates.values()),
            "batch_size_histogram": dict(sorted(Counter(dates.values()).items()))}


def probability_diagnostics(rows, thresholds=(.48, .49, .495, .5, .505, .51, .52)):
    """An action probability is never a calibrated probability of profit."""
    policy = [r for r in rows if r.get("mode") == "policy"]
    observed = [(r, _number(r.get("selection_probability"))) for r in policy]
    observed = [(r, p) for r, p in observed if p is not None]
    values = np.array([p for _, p in observed], dtype=float)
    if len(values) and np.any((values < 0) | (values > 1)):
        raise ValueError("selection probabilities must be in [0, 1]")
    frozen = [_number(r.get("policy_threshold")) for r, _ in observed]
    effective = [t if t is not None else .5 for t in frozen]
    return {
        "meaning": "Probability of the policy accept action; not probability of profit.",
        "policy_candidates": len(policy), "observed_probabilities": len(values),
        "selected_actions": sum(_bool(r.get("selected")) is True for r in policy),
        "rejected_actions": sum(_bool(r.get("selected")) is False for r in policy),
        "minimum": float(values.min()) if len(values) else None,
        "maximum": float(values.max()) if len(values) else None,
        "mean": float(values.mean()) if len(values) else None,
        "quantiles": {str(q): float(np.quantile(values, q)) for q in (0, .1, .5, .9, 1)} if len(values) else {},
        "threshold_source": "saved_decision" if frozen and all(t is not None for t in frozen)
                            else "legacy_default_0.5_or_saved_decision",
        "at_or_above_frozen_threshold": sum(p >= t for (_, p), t in zip(observed, effective)),
        "below_frozen_threshold": sum(p < t for (_, p), t in zip(observed, effective)),
        "within_0.01_of_half": int(np.count_nonzero(abs(values - .5) <= .01)),
        "threshold_sensitivity": [{"threshold": float(t),
                                   "would_accept": int(np.count_nonzero(values >= t)),
                                   "would_reject": int(np.count_nonzero(values < t)),
                                   "economic_result": None} for t in thresholds],
        "interpretation": ("All observed probabilities fall below the deterministic frozen threshold; "
                           "the all-cash evaluation follows from the action rule."
                           if len(values) and all(p < t for (_, p), t in zip(observed, effective))
                           else "Use saved actions and the declared evaluation rule to interpret probabilities."),
        "experiment_contract": "Register thresholds on training/validation before freezing. Sensitivity counts "
                               "are descriptive; rerun the original engine to measure changed account paths.",
    }


def ranking_observations(rows):
    """Report observed competition, without fabricating budget counterfactuals."""
    groups = defaultdict(list)
    for row in rows:
        stamp = _stamp(row.get("bar_time", row.get("timestamp")))
        if stamp is not None:
            groups[stamp.isoformat()].append(row)
    batches = []
    for stamp, batch in sorted(groups.items()):
        if len(batch) < 2:
            continue
        def ordered(field):
            if any(_number(r.get(field)) is None for r in batch):
                return None
            return [r.get("decision_id") or f"{r.get('strategy')}|{r.get('symbol')}"
                    for r in sorted(batch, key=lambda r: (-float(r[field]), str(r.get("strategy")), str(r.get("symbol"))))]
        native, actual = ordered("native_score"), ordered("allocation_score")
        limited = any(_number(r.get("target_qty")) is not None and
                      _number(r.get("approved_qty")) is not None and
                      float(r["approved_qty"]) < float(r["target_qty"]) - 1e-12 for r in batch)
        batches.append({"bar_time": stamp, "candidate_count": len(batch),
                        "native_order": native, "model_order": ordered("ranking_score"),
                        "allocation_order": actual, "observed_budget_limited": limited,
                        "order_changed": native != actual if native is not None and actual is not None else None,
                        "ranking_impact_on_allocation": None})
    return {"competition_batches": len(batches), "batches": batches,
            "ranking_impact": "unknown",
            "reason": "No identical-account allocator counterfactual was replayed. Changed ordering alone "
                      "does not demonstrate a changed choice, position or economic value.",
            "required_comparison": "Replay native, momentum, model and seeded random scores on the same frozen "
                                   "candidate batch with identical cash, holdings, reservations, health, risk, "
                                   "exit and execution conditions; separately report budget-constrained batches."}


def build_episode_diagnostics(result, rewards, selection_audit=(), dataset=None,
                              initial_capital=None, drawdown_penalty=.5, turnover_penalty=0.):
    """Return serializable ledgers, funnel and independently recomputed rewards."""
    observations = _records(result.get("entry_observations"))
    selections = _records(selection_audit)
    selection_ids = {str(r["decision_id"]): r for r in selections if not _missing(r.get("decision_id"))}
    if len(selection_ids) != sum(not _missing(r.get("decision_id")) for r in selections):
        raise ValueError("duplicate selector decision ids")
    decisions = []
    for source in observations:
        row = dict(source)
        key = row.get("decision_id")
        if key is not None and str(key) in selection_ids:
            # The engine's last reason and quantities are authoritative after
            # selection. Selection itself cannot overwrite allocation facts.
            selected = selection_ids[str(key)]
            row = {**selected, **row, "selection_reason": selected.get("reason")}
        row["linkage_source"] = "engine_observation" if key is not None else "legacy_observation_without_id"
        decisions.append(row)
    observed_ids = {str(r["decision_id"]) for r in decisions if r.get("decision_id") is not None}
    for source in selections:
        if source.get("decision_id") is None or str(source["decision_id"]) not in observed_ids:
            decisions.append({**source, "linkage_source": "selector_only",
                              "order_id": None, "engine_gate_facts_missing": True})
    # Old native runs only saved allocation candidates. They are still useful
    # for opportunity counts, but cannot explain upstream rejected signals.
    if not decisions:
        for row in _records(result.get("allocation_audit")):
            decisions.append({**row, "decision_id": None, "raw_setup": True,
                              "order_id": None, "linkage_source": "legacy_allocation_only",
                              "engine_gate_facts_missing": True})
    ids = [str(r["decision_id"]) for r in decisions if r.get("decision_id") is not None]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate engine decision ids")
    order_rows = {}
    for row in decisions:
        order = row.get("order_id")
        if not _missing(order):
            if str(order) in order_rows:
                raise ValueError("one opening order linked to multiple decisions")
            order_rows[str(order)] = row
    terminal_policy = result.get("terminal_policy", "mark_to_market")
    trades = _actual_trades(_records(result.get("trades")), terminal_policy)
    fills, openings, exits, identities = [], defaultdict(list), defaultdict(list), defaultdict(set)
    for row in decisions:
        if row.get("order_id"):
            for position in _structure(row.get("actual_position_ids"), []):
                identities[str(row["order_id"])].add(str(position))
    for index, trade in enumerate(trades):
        details = _structure(trade.get("lot_closes"), [])
        details = details if isinstance(details, list) else []
        order = str(trade["order_id"]) if not _missing(trade.get("order_id")) else None
        linked = set()
        close_links = []
        if trade.get("side") in {"buy", "short"} and order in order_rows:
            linked.add(order_rows[order].get("decision_id"))
            openings[order].append(index)
        for detail in details:
            # Broker archives name the originating order entry_order_id.
            # Older research archives used order_id. An explicit unknown in
            # the authoritative field must not be replaced by legacy data.
            source = "entry_order_id" if "entry_order_id" in detail else "order_id" if "order_id" in detail else None
            opening = detail.get(source) if source is not None else None
            opening = str(opening) if not _missing(opening) and opening != "" else None
            decision_id = order_rows.get(opening, {}).get("decision_id")
            close_links.append({"opening_order_id": opening, "opening_order_id_source": source,
                                "decision_id": decision_id, "lot_id": detail.get("lot_id"),
                                "position_id": detail.get("position_id"),
                                "qty_closed": _number(detail.get("qty_closed")),
                                "identity_status": "linked" if decision_id is not None else "unknown"})
            if opening is not None:
                if detail.get("position_id") is not None:
                    identities[opening].add(str(detail["position_id"]))
                exits[opening].append((index, detail))
                if decision_id is not None:
                    linked.add(decision_id)
        qty, price = _number(trade.get("qty")), _number(trade.get("fill_price"))
        commission, slip = _number(trade.get("commission")), _number(trade.get("slip"))
        fills.append({"fill_id": f"fill:{index}", "order_id": order,
                      "decision_ids": sorted(x for x in linked if x is not None),
                      "symbol": trade.get("symbol"), "side": trade.get("side"),
                      "fill_time": trade.get("fill_time"), "qty": qty, "fill_price": price,
                      "filled_notional": qty * price if qty is not None and price is not None else None,
                      "commission": commission,
                      "slippage_cost": abs(slip) * abs(qty) if slip is not None and qty is not None else None,
                      "exit_reason": trade.get("exit_reason"), "lot_closes": details,
                      "lot_close_linkage": close_links,
                      "position_ids": sorted({str(d["position_id"]) for d in details if d.get("position_id") is not None}),
                      "opening_position_ids": sorted(identities.get(order, set())) if order else [],
                      "identity_status": ("partially_linked" if linked and any(
                          link["identity_status"] == "unknown" for link in close_links)
                          else "linked" if linked else "unknown")})
    # Closing facts can reveal ids of earlier opening fills.
    for fill in fills:
        if fill["side"] in {"buy", "short"} and fill["order_id"]:
            fill["opening_position_ids"] = sorted(identities.get(fill["order_id"], set()))
    curve = _evaluated_curve(result)
    stamps = pd.DatetimeIndex(pd.to_datetime(curve.index, utc=True))
    execution = defaultdict(list)
    for fact in _records(result.get("execution_audit")):
        key = fact.get("order_id", fact.get("client_order_id"))
        if key is not None:
            execution[str(key)].append(fact)
    for row in decisions:
        order = str(row["order_id"]) if not _missing(row.get("order_id")) else None
        plan = _structure(row.get("capital_allocation"), {})
        plan = plan if isinstance(plan, dict) else {}
        row["target_qty"] = _number(plan.get("requested_qty", row.get("sized_qty", row.get("requested_qty"))))
        row["capital_approved_qty"] = _number(plan.get("approved_qty", row.get("capital_approved_qty")))
        row["approved_qty"] = (_number(row.get("drawdown_clamped_qty", row.get("budgeted_qty", row.get("clamped_qty"))))
                               if order else None)
        if order and row["approved_qty"] is None:
            row["approved_qty"] = row["capital_approved_qty"]
        indices = openings.get(order, [])
        row["fill_ids"] = [fills[i]["fill_id"] for i in indices]
        row["fill_count"] = len(indices)
        row["filled_qty"] = sum(fills[i]["qty"] or 0. for i in indices) if order else None
        row["unfilled_qty"] = (max(0., row["approved_qty"] - row["filled_qty"])
                               if row["approved_qty"] is not None and row["filled_qty"] is not None else None)
        row["actual_position_ids"] = sorted(identities.get(order, set())) if order else None
        row["exit_fill_ids"] = sorted({fills[i]["fill_id"] for i, _ in exits.get(order, [])})
        row["exit_order_ids"] = sorted({fills[i]["order_id"] for i, _ in exits.get(order, []) if fills[i]["order_id"]})
        row["closed_qty"] = sum(_number(d.get("qty_closed")) or 0. for _, d in exits.get(order, [])) if order else None
        row["execution_facts"] = execution.get(order, []) if order else None
        decision_time = _stamp(row.get("bar_time", row.get("timestamp")))
        mark = int(stamps.searchsorted(decision_time, side="left")) if decision_time is not None else len(curve)
        row["equity_sample_time"] = stamps[mark].isoformat() if mark < len(curve) else None
        row["account_equity_after_bar"] = float(curve.equity.iloc[mark]) if mark < len(curve) else None
        row["account_final_equity"] = float(curve.equity.iloc[-1]) if len(curve) else None
        row["individual_equity_contribution"] = None
        row["execution_linkage"] = "order_id" if order else "unknown"
        row["outcome"] = ("filled" if indices else "submitted_without_fill" if order
                          else row.get("reason", "unknown"))
    opportunity_rows = [r for r in decisions if r.get("original_strategy_signal") is True or _number(r.get("native_score")) is not None
                        or r.get("linkage_source") in {"selector_only", "legacy_allocation_only"}]
    reasons = Counter(str(r.get("reason", "unknown")) for r in decisions)
    counts = _date_counts(opportunity_rows)
    upstream_missing = not observations
    measured_stages = {
        "observations": len(observations), "original_strategy_candidates": len(opportunity_rows),
        "raw_setups": sum(_bool(r.get("raw_setup")) is True for r in decisions),
        "data_qualified_candidates": sum(_bool(r.get("eligible")) is True for r in opportunity_rows),
        "model_selected": sum(_bool(r.get("selected")) is True for r in opportunity_rows),
        "ranked": sum(_number(r.get("rank")) is not None for r in opportunity_rows),
        "submitted_opening_orders": len(order_rows) if not upstream_missing else None,
        "filled_opening_orders": len({str(t["order_id"]) for t in trades if t.get("side") in {"buy", "short"}
                                     and not _missing(t.get("order_id"))}),
        "linked_filled_opening_orders": len(openings),
        "opening_fill_count": sum(len(v) for v in openings.values()),
        "actual_fill_count": len(fills),
    }
    funnel = {**counts, "stages": measured_stages, "rejection_reasons": dict(sorted(reasons.items())),
              "historical_membership_qualification": None,
              "membership_reason": "A causal feature snapshot alone is not historical membership evidence.",
              "upstream_observations_available": not upstream_missing,
              "low_exposure_attribution": {"observed_gate_counts": dict(sorted(reasons.items())),
                  "mean_gross_exposure_fraction": float(curve.gross_exposure_pct_equity.mean())
                      if len(curve) and "gross_exposure_pct_equity" in curve else None,
                  "mean_cash_fraction": float((curve.cash / curve.equity).mean()) if len(curve) and "cash" in curve else None,
                  "interpretation_status": "partial_missing_upstream_facts" if upstream_missing else "observed_first_gate_only"},
              "linkage": {"decision_ids": len(ids), "unidentified_decisions": len(decisions) - len(ids),
                  "unlinked_opening_order_ids": sorted({str(t["order_id"]) for t in trades
                      if t.get("side") in {"buy", "short"} and not _missing(t.get("order_id"))} - set(order_rows)),
                  "unlinked_fill_count": sum(not f["decision_ids"] for f in fills)},
              "scope": "Passive first visited gates only. Unvisited original signals and individual equity contributions remain unknown.",
              "ranking": ranking_observations(opportunity_rows)}
    if dataset is not None:
        data = dataset if isinstance(dataset, pd.DataFrame) else pd.DataFrame(dataset)
        funnel["dataset"] = {"rows": len(data), "eligible_rows": int(data.eligible.sum()) if "eligible" in data else None,
                             "independent_sample_count": None}
    reward = {"initial_capital": _number(initial_capital), "reward_rows": len(rewards),
              "cost_contract": "Commissions, fill-price slippage and financing already enter engine net equity. "
                               "Reward adds only declared drawdown-deepening and turnover penalties.",
              "commission": sum(f["commission"] or 0. for f in fills),
              "slippage_cost": sum(f["slippage_cost"] or 0. for f in fills),
              "terminal_policy": terminal_policy,
              "lifecycle": result.get("lifecycle", {}), "accounting_check": result.get("accounting_check"),
              "evaluated_equity_rows": len(curve), "excluded_frozen_tail_rows": len(result["equity_curve"]) - len(curve)}
    for column in ("net_log_return", "drawdown_penalty", "turnover_penalty", "reward", "turnover"):
        reward[column + "_sum"] = float(rewards[column].sum()) if column in rewards else None
    if initial_capital is not None:
        expected = equity_rewards(curve, trades, initial_capital=initial_capital,
                                  drawdown_penalty=drawdown_penalty, turnover_penalty=turnover_penalty,
                                  terminal_policy=reward["terminal_policy"])
        aligned = (len(expected) == len(rewards) and
                   pd.DatetimeIndex(pd.to_datetime(expected.index, utc=True)).equals(
                       pd.DatetimeIndex(pd.to_datetime(rewards.index, utc=True))))
        errors = {c: (float(np.max(np.abs(expected[c].to_numpy() - rewards[c].to_numpy())))
                      if aligned and len(expected) and c in rewards else 0. if aligned and not len(expected) else None)
                  for c in expected.columns}
        reward.update({"index_aligned": aligned, "max_absolute_errors": errors,
                       "reconciliation_ok": aligned and all(e is not None and e <= 1e-10 for e in errors.values()),
                       "net_log_return_telescoping_error": abs(float(expected.net_log_return.sum()) -
                           math.log(float(curve.equity.iloc[-1]) / initial_capital)) if len(curve) else 0.,
                       "positive_net_return_negative_reward": bool(len(curve) and curve.equity.iloc[-1] > initial_capital
                           and float(rewards.reward.sum()) < 0),
                       "cash_episode": not fills and bool(len(curve) and np.allclose(curve.equity, initial_capital, rtol=0, atol=1e-10)),
                       "costs_subtracted_twice": False})
    else:
        reward.update({"reconciliation_ok": None, "reason": "Missing initial capital; do not infer it from the first post-trade equity sample."})
    return _clean({"schema": "ml-selection-diagnostics/v1", "decision_ledger": decisions,
                   "fill_ledger": fills, "funnel": funnel, "reward_reconciliation": reward,
                   "probability": probability_diagnostics(selections)})


def _csv(path, *, index=False):
    try:
        frame = pd.read_csv(path, float_precision="round_trip")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    if index and len(frame.columns):
        field = "bar_time" if "bar_time" in frame else frame.columns[0]
        frame.index = pd.to_datetime(frame.pop(field), utc=True)
    return frame


def audit_existing_run(folder, output):
    """Diagnose old immutable run artifacts, writing only to a new folder."""
    source, target = Path(folder).resolve(), Path(output).resolve()
    if target == source or target.is_relative_to(source):
        raise ValueError("audit output must be outside the original run")
    if target.exists():
        raise FileExistsError("audit output already exists; preserve previous diagnostics")
    registry_path = source / "artifacts.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    verified = {}
    def verify(path):
        path = Path(path).resolve()
        if not path.is_relative_to(source):
            raise ValueError("archived source path escapes run")
        relative = path.relative_to(source).as_posix()
        declared = registry.get(relative, registry.get(relative.replace("/", "\\")))
        if not path.is_file():
            if declared is not None:
                raise ValueError(f"recorded research artifact is missing: {relative}")
            return False
        digest = sha256_file(path)
        if registry and declared is None:
            raise ValueError(f"source artifact is not registered: {relative}")
        if declared is not None and digest != declared:
            raise ValueError(f"research artifact changed: {relative}")
        verified[relative] = {"sha256": digest, "manifest_verified": declared is not None}
        return True
    protocol_path = source / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8")) if verify(protocol_path) else {}
    protocol_verified = False
    if protocol.get("protocol_id"):
        identity = {key: value for key, value in protocol.items() if key != "protocol_id"}
        if hashlib.sha256(canonical_json(identity).encode()).hexdigest() != protocol["protocol_id"]:
            raise ValueError("frozen protocol content was changed")
        protocol_verified = True
    settings = protocol.get("settings", {})
    options = settings.get("rl", {})
    reports = {}
    episodes = source / "episodes"
    if not episodes.is_dir():
        raise ValueError("run has no archived episodes")
    summary_files = ([source / key for key in registry if key.replace("\\", "/").endswith("/summary.json")]
                     if registry else list(source.rglob("summary.json")))
    for summary_file in sorted(summary_files):
        episode = summary_file.parent.resolve()
        if not episode.is_relative_to(source):
            raise ValueError("archived episode path escapes source run")
        if not episode.is_dir() or not (episode / "equity.csv").exists():
            continue
        verify(episode / "summary.json")
        for name in ("equity.csv", "trades.csv", "rewards.csv", "allocation.csv", "selection.csv",
                     "entry_observations.json", "entry_observations.csv", "execution_audit.csv"):
            verify(episode / name)
        summary = json.loads((episode / "summary.json").read_text(encoding="utf-8"))
        lifecycle = {k: summary.get(k) for k in ("termination_timestamp", "termination_reason", "status", "operating_status")}
        observations_file = episode / "entry_observations.json"
        observations = (json.loads(observations_file.read_text(encoding="utf-8")) if observations_file.exists()
                        else _csv(episode / "entry_observations.csv"))
        result = {"equity_curve": _csv(episode / "equity.csv", index=True), "trades": _csv(episode / "trades.csv"),
                  "allocation_audit": _csv(episode / "allocation.csv"), "entry_observations": observations,
                  "execution_audit": _csv(episode / "execution_audit.csv"), "lifecycle": lifecycle,
                  "accounting_check": {"ok": summary.get("accounting_ok")},
                  "terminal_policy": summary.get("terminal_policy", "mark_to_market")}
        name = episode.name if episode.parent == episodes else episode.relative_to(source).as_posix()
        reports[name] = build_episode_diagnostics(result, _csv(episode / "rewards.csv", index=True),
            _csv(episode / "selection.csv"), initial_capital=summary.get("initial_capital"),
            drawdown_penalty=options.get("drawdown_penalty", .5), turnover_penalty=options.get("turnover_penalty", 0.))
    comparisons = [{"arm": name, **{k: diagnostic["funnel"][k] for k in
                    ("candidates", "candidate_dates", "competition_dates")},
                    "candidate_source": "engine_observation" if diagnostic["funnel"]["upstream_observations_available"]
                                        else "legacy_selection_or_allocation",
                    "upstream_rejections_known": diagnostic["funnel"]["upstream_observations_available"],
                    "mean_gross_exposure_fraction": diagnostic["funnel"]["low_exposure_attribution"]["mean_gross_exposure_fraction"],
                    "reward_reconciled": diagnostic["reward_reconciliation"]["reconciliation_ok"]}
                   for name, diagnostic in reports.items()]
    seed_path = source / "rl_seed_comparison.json"
    seed_results = json.loads(seed_path.read_text(encoding="utf-8")) if verify(seed_path) else {}
    best_seeds = {}
    for seed, item in seed_results.items():
        saved = item.get("validation", {})
        relative = str(saved.get("artifact_directory", "")).replace("\\", "/")
        name = f"rl_seeds/{seed}/{relative}"
        best_seeds[seed] = {"policy_id": item.get("policy_id"), "saved_validation": saved,
                            "artifact": name, "probability": reports.get(name, {}).get("probability"),
                            "reward_reconciliation": reports.get(name, {}).get("reward_reconciliation")}
    result = {"schema": "ml-selection-existing-run-audit/v1", "source_run": str(source),
              "read_only_source": True, "independent_holdout": False,
              "source_identity_receipt": {"protocol_id": protocol.get("protocol_id"),
                  "protocol_content_verified": protocol_verified,
                  "artifact_manifest_sha256": sha256_file(registry_path) if registry_path.exists() else None,
                  "verified_source_files": verified,
                  "identity_status": "manifest_and_protocol_verified" if registry and protocol_verified
                                     else "partial_missing_manifest_or_protocol"},
              "candidate_date_review": comparisons,
              "rl_probability_review": {name: r["probability"] for name, r in reports.items()
                                        if r["probability"]["policy_candidates"]},
              "best_rl_seed_review": best_seeds,
              "limitations": ["Older runs lack upstream original strategy observations and opening order linkage.",
                              "Archived test has already been observed and remains development material.",
                              "Date counts are per account path and per arm; they need not be identical.",
                              "Threshold sensitivity does not measure economic performance of a new policy."],
              "episodes": reports}
    target.mkdir(parents=True, exist_ok=False)
    for name, report in reports.items():
        destination = target / name
        destination.mkdir(parents=True)
        for ledger in ("decision_ledger", "fill_ledger"):
            pd.DataFrame(report[ledger]).to_csv(destination / f"{ledger}.csv", index=False)
        for item in ("funnel", "reward_reconciliation", "probability"):
            (destination / f"{item}.json").write_text(json.dumps(report[item], ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    pd.DataFrame(comparisons).to_csv(target / "candidate_date_review.csv", index=False)
    (target / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return result


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Read-only audit of immutable ML research episodes")
    parser.add_argument("folder")
    parser.add_argument("output")
    arguments = parser.parse_args(argv)
    result = audit_existing_run(arguments.folder, arguments.output)
    print(json.dumps({"source_run": result["source_run"], "output": str(Path(arguments.output).resolve()),
                      "episodes": len(result["episodes"]), "candidate_date_review": result["candidate_date_review"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
