"""Causal ids, passive attribution and independent economic reconciliation."""
from copy import deepcopy
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.allocation import EntryCandidate
from core.entry_audit import capture, note
from core.runtime import EventProcessor, MarketDataSlice
from core.reproducibility import canonical_json, sha256_file
from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.diagnostics import (audit_existing_run, build_episode_diagnostics,
                                                probability_diagnostics, ranking_observations)
from research.ml_selection.environment import equity_rewards
from research.ml_selection.selector import ResearchSelector


def fixture_result():
    stamps = pd.date_range("2024-01-01", periods=4, tz="UTC")
    curve = pd.DataFrame({"equity": [100., 99., 101., 100.5], "cash": [100., 89., 81., 100.5],
                          "gross_exposure_pct_equity": [0., .1, .2, 0.]}, index=stamps)
    observation = {"decision_id": "D1", "timestamp": stamps[0], "symbol": "A", "strategy": "S",
                   "raw_setup": True, "native_score": 1., "order_id": "O1", "reason": "order_accepted",
                   "sized_qty": 2., "clamped_qty": 2., "actual_position_ids": ["P1"]}
    trades = [{"order_id": "O1", "fill_time": stamps[1], "symbol": "A", "side": "buy",
               "qty": .5, "fill_price": 10., "commission": .05, "slip": .1},
              {"order_id": "O1", "fill_time": stamps[2], "symbol": "A", "side": "buy",
               "qty": 1., "fill_price": 11., "commission": .11, "slip": .2},
              {"order_id": "X1", "fill_time": stamps[3], "symbol": "A", "side": "sell",
               "qty": 1.5, "fill_price": 12., "commission": .18, "slip": .05,
               "lot_closes": [{"entry_order_id": "O1", "position_id": "P1", "lot_id": "L1", "qty_closed": 1.5}]},
              # Same symbol is deliberately insufficient to link this fill.
              {"order_id": "O2", "fill_time": stamps[2], "symbol": "A", "side": "buy",
               "qty": .1, "fill_price": 11., "commission": .011, "slip": 0.}]
    return {"equity_curve": curve, "trades": trades, "entry_observations": [observation],
            "execution_audit": [{"order_id": "O1", "status": "partially_filled", "remaining_qty": .5}],
            "terminal_policy": "forced_liquidation", "lifecycle": {"status": "completed"},
            "accounting_check": {"ok": True}}


def test_partial_fills_close_lots_and_equity_keep_authoritative_ids():
    result = fixture_result()
    rewards = equity_rewards(result["equity_curve"], result["trades"], initial_capital=100.)
    report = build_episode_diagnostics(result, rewards, initial_capital=100.)
    decision = report["decision_ledger"][0]
    assert decision["fill_ids"] == ["fill:0", "fill:1"]
    assert decision["filled_qty"] == 1.5 and decision["unfilled_qty"] == .5
    assert decision["actual_position_ids"] == ["P1"]
    assert decision["exit_fill_ids"] == ["fill:2"] and decision["closed_qty"] == 1.5
    assert decision["account_equity_after_bar"] == 100.
    assert decision["account_final_equity"] == 100.5
    assert decision["individual_equity_contribution"] is None
    assert report["fill_ledger"][2]["decision_ids"] == ["D1"]
    assert report["fill_ledger"][3]["decision_ids"] == []
    assert report["funnel"]["linkage"]["unlinked_opening_order_ids"] == ["O2"]
    assert report["reward_reconciliation"]["reconciliation_ok"] is True
    assert report["reward_reconciliation"]["positive_net_return_negative_reward"] is True
    assert report["reward_reconciliation"]["commission"] == pytest.approx(.351)
    assert report["reward_reconciliation"]["slippage_cost"] == pytest.approx(.325)
    assert report["reward_reconciliation"]["costs_subtracted_twice"] is False


@pytest.mark.parametrize("detail,linked,source", [
    ({"entry_order_id": "O1", "order_id": "WRONG"}, True, "entry_order_id"),
    ({"order_id": "O1"}, True, "order_id"),
    ({"entry_order_id": None, "order_id": "O1"}, False, "entry_order_id"),
    ({"entry_order_id": float("nan")}, False, "entry_order_id"),
    ({}, False, None),
])
def test_exit_origin_prefers_engine_identity_and_keeps_unknown_explicit(detail, linked, source):
    result = fixture_result()
    result["trades"][2]["lot_closes"][0] = {
        **detail, "position_id": "P1", "lot_id": "L1", "qty_closed": 1.5}
    rewards = equity_rewards(result["equity_curve"], result["trades"], initial_capital=100.)
    report = build_episode_diagnostics(result, rewards, initial_capital=100.)
    fill = report["fill_ledger"][2]
    assert fill["decision_ids"] == (["D1"] if linked else [])
    assert fill["identity_status"] == ("linked" if linked else "unknown")
    assert fill["lot_close_linkage"][0]["opening_order_id_source"] == source
    assert fill["lot_close_linkage"][0]["decision_id"] == ("D1" if linked else None)
    assert report["decision_ledger"][0]["closed_qty"] == (1.5 if linked else 0.)


def test_real_broker_partial_fills_shared_exit_and_terminal_close_keep_lot_quantities():
    from core.broker import Broker
    from core.portfolio import Portfolio

    symbol = "BTC/USDT"
    portfolio = Portfolio(10_000.)
    broker = Broker(portfolio, commission_rate=0., commission_rate_maker=0., slippage=0.)
    stamps = pd.date_range("2024-01-01", periods=6)
    equity = [10_000.]

    def bar(day, price=100., volume=1_000.):
        return {symbol: pd.Series({"open": price, "high": price + 1., "low": price - 1.,
                                   "close": price, "volume": volume}, name=stamps[day])}

    first = broker.submit_order(symbol, "buy", 2., price=100., timestamp=stamps[0], strategy_id="S")
    broker.process_orders(bar(1, volume=1.))
    equity.append(portfolio.get_total_value({symbol: 100.}))
    broker.process_orders(bar(2, volume=1.))
    equity.append(portfolio.get_total_value({symbol: 100.}))
    second = broker.submit_order(symbol, "buy", 1., price=100., timestamp=stamps[2], strategy_id="S")
    broker.process_orders(bar(3))
    equity.append(portfolio.get_total_value({symbol: 100.}))
    broker.submit_order(symbol, "sell", 2.5, price=110., timestamp=stamps[3], strategy_id="Router")
    broker.process_orders(bar(4, price=110.))
    equity.append(portfolio.get_total_value({symbol: 110.}))
    broker.force_liquidate(bar(5, price=110.), timestamp=stamps[5], reason="EndOfBacktest")
    equity.append(portfolio.get_total_value({symbol: 110.}))
    curve = pd.DataFrame({"equity": equity}, index=stamps)
    result = {"equity_curve": curve, "trades": broker.trades, "terminal_policy": "forced_liquidation",
              "entry_observations": [{"decision_id": "D1", "timestamp": stamps[0], "symbol": symbol,
                                      "order_id": first.id, "clamped_qty": 2.},
                                     {"decision_id": "D2", "timestamp": stamps[2], "symbol": symbol,
                                      "order_id": second.id, "clamped_qty": 1.}]}
    rewards = equity_rewards(curve, broker.trades, initial_capital=10_000., terminal_policy="forced_liquidation")
    report = build_episode_diagnostics(result, rewards, initial_capital=10_000.)
    decisions, fills = report["decision_ledger"], report["fill_ledger"]
    assert [d["fill_count"] for d in decisions] == [2, 1]
    assert [d["filled_qty"] for d in decisions] == [2., 1.]
    assert [d["closed_qty"] for d in decisions] == [2., 1.]
    assert fills[3]["decision_ids"] == ["D1", "D2"]
    # The real lot book merges partial fills belonging to one opening order.
    assert len(fills[3]["lot_close_linkage"]) == 2
    assert sum(d["qty_closed"] for d in fills[3]["lot_close_linkage"] if d["decision_id"] == "D1") == 2.
    assert sum(d["qty_closed"] for d in fills[3]["lot_close_linkage"] if d["decision_id"] == "D2") == .5
    assert fills[4]["decision_ids"] == ["D2"] and fills[4]["exit_reason"] == "EndOfBacktest"
    assert all(f["identity_status"] == "linked" for f in fills)
    assert all(link["opening_order_id_source"] == "entry_order_id"
               for f in fills for link in f["lot_close_linkage"])
    assert sum(d["closed_qty"] for d in decisions) == sum(f["qty"] for f in fills if f["side"] == "sell")
    assert report["funnel"]["linkage"]["unlinked_fill_count"] == 0
    assert report["reward_reconciliation"]["reconciliation_ok"] is True


def test_shared_exit_keeps_unidentified_lot_quantity_out_of_known_decision():
    result = fixture_result()
    result["trades"][2]["lot_closes"] = [
        {"entry_order_id": "O1", "position_id": "P1", "lot_id": "L1", "qty_closed": 1.},
        {"position_id": "P1", "lot_id": "L2", "qty_closed": .5}]
    rewards = equity_rewards(result["equity_curve"], result["trades"], initial_capital=100.)
    report = build_episode_diagnostics(result, rewards, initial_capital=100.)
    fill = report["fill_ledger"][2]
    assert fill["decision_ids"] == ["D1"]
    assert fill["identity_status"] == "partially_linked"
    assert fill["lot_close_linkage"][1]["opening_order_id"] is None
    assert fill["lot_close_linkage"][1]["decision_id"] is None
    assert report["decision_ledger"][0]["closed_qty"] == 1.


def test_reward_reconciliation_detects_changed_value_and_excludes_frozen_tail():
    result = fixture_result()
    result["lifecycle"]["termination_timestamp"] = result["equity_curve"].index[-1]
    tail = pd.DataFrame({"equity": [100.5], "cash": [100.5], "gross_exposure_pct_equity": [0.]},
                        index=[pd.Timestamp("2024-01-05", tz="UTC")])
    original = result["equity_curve"]
    result["equity_curve"] = pd.concat([original, tail])
    rewards = equity_rewards(original, result["trades"], initial_capital=100.)
    rewards.loc[rewards.index[-1], "reward"] += .001
    report = build_episode_diagnostics(result, rewards, initial_capital=100.)
    assert report["reward_reconciliation"]["reconciliation_ok"] is False
    assert report["reward_reconciliation"]["excluded_frozen_tail_rows"] == 1
    assert report["reward_reconciliation"]["max_absolute_errors"]["reward"] == pytest.approx(.001)


def test_cash_episode_is_legal_and_zero_reward_without_forcing_trades():
    curve = pd.DataFrame({"equity": [100., 100.]}, index=pd.date_range("2024-01-01", periods=2))
    rewards = equity_rewards(curve, [], initial_capital=100.)
    report = build_episode_diagnostics({"equity_curve": curve, "trades": []}, rewards, initial_capital=100.)
    assert report["reward_reconciliation"]["cash_episode"] is True
    assert report["reward_reconciliation"]["reward_sum"] == 0.
    assert report["reward_reconciliation"]["reconciliation_ok"] is True


def test_passive_gate_sequence_preserves_reason_before_submission():
    row = {"decision_id": "D1"}
    with capture(row):
        note("cash_limit", requested_qty=10.)
        note("order_accepted", order_id="O1", clamped_qty=1.)
    assert row["reason"] == "order_accepted"
    assert [item["reason"] for item in row["gate_facts"]] == ["cash_limit", "order_accepted"]
    with capture(None):
        note("cash_limit")
    assert len(row["gate_facts"]) == 2


def selector_fixture():
    stamp = pd.Timestamp("2024-01-01", tz="UTC")
    dataset = pd.DataFrame([{"symbol": "A", "as_of": stamp + pd.Timedelta(days=1), "eligible": True,
                             "exclusion_reason": None, **{feature: .01 for feature in FEATURE_COLUMNS}}])
    frame = pd.DataFrame({"close": [10.]}, index=[stamp])
    candidate = EntryCandidate("A", SimpleNamespace(name="S"), 0, frame, "trend",
                               {"action": "buy", "requested_qty": 2.}, 1., audit={"decision_id": "D1"})
    context = {"event": MarketDataSlice(stamp, {"A": frame.iloc[0]}, {"A": frame}, timeframe="1d"),
               "portfolio": SimpleNamespace(cash=100., positions={}, get_total_value=lambda p: 100.),
               "broker": SimpleNamespace(), "risk_manager": SimpleNamespace(), "current_prices": {"A": 10.}}
    return dataset, candidate, context


def test_ranker_score_is_separate_from_calibrated_return_gate():
    dataset, candidate, context = selector_fixture()
    model = SimpleNamespace(kind="lambdarank", predict=lambda frame: np.array([100.]),
                            predict_net_return=lambda frame: np.array([-.01]))
    selector = ResearchSelector(dataset, model=model)
    assert selector.select([candidate], **context) == []
    assert selector.audit[0]["ranking_score"] == 100.
    assert selector.audit[0]["expected_net_return"] == -.01
    assert candidate.audit["decision_id"] == "D1"
    assert candidate.audit["reason"] == "model_gate"
    assert candidate.signal["requested_qty"] == 2.


def test_frozen_policy_threshold_changes_only_action_rule():
    dataset, candidate, context = selector_fixture()
    calls = []
    def act(frame, deterministic, threshold=.5):
        calls.append((deterministic, threshold))
        probabilities = np.array([.499])
        return probabilities >= threshold, probabilities
    policy = SimpleNamespace(metadata={"evaluation_threshold": .495}, act=act)
    model = SimpleNamespace(kind="ridge", predict=lambda frame: np.array([.02]))
    selector = ResearchSelector(dataset, mode="policy", model=model, policy=policy)
    assert len(selector.select([candidate], **context)) == 1
    assert calls == [(True, .495)]
    assert selector.audit[0]["policy_threshold"] == .495
    rejected = ResearchSelector(dataset, mode="policy", model=model, policy=policy, policy_threshold=.5)
    assert rejected.select([candidate], **context) == []
    stats = probability_diagnostics(rejected.audit)
    assert stats["at_or_above_frozen_threshold"] == 0
    assert next(x for x in stats["threshold_sensitivity"] if x["threshold"] == .495)["would_accept"] == 1
    assert stats["selected_actions"] == 0


@pytest.mark.parametrize("threshold", [True, -.1, 1.1, float("nan")])
def test_invalid_policy_threshold_is_rejected(threshold):
    dataset, _, _ = selector_fixture()
    with pytest.raises(ValueError, match="policy_threshold"):
        ResearchSelector(dataset, mode="qualified_native", policy_threshold=threshold)


def test_runtime_reads_actual_open_lot_order_identity_without_symbol_guessing():
    processor = object.__new__(EventProcessor)
    processor.entry_audit_enabled = True
    processor.entry_audit = [{"decision_id": "D1", "symbol": "A", "order_id": "O1"}]
    processor._audit_registered_rows, processor._audit_order_rows = 0, {}
    processor.portfolio = SimpleNamespace(lot_books={"A": SimpleNamespace(open_lots=[
        SimpleNamespace(order_id="O1", position_id="P1", lot_id="L1"),
        SimpleNamespace(order_id="O2", position_id="P2", lot_id="L2")])})
    processor._settle_entry_identities()
    processor._settle_entry_identities()
    assert processor.entry_audit[0]["actual_position_ids"] == ["P1"]
    assert processor.entry_audit[0]["actual_lot_ids"] == ["L1"]


def test_order_ambiguity_rejected_and_unknown_initial_capital_not_inferred():
    result = fixture_result()
    rewards = equity_rewards(result["equity_curve"], result["trades"], initial_capital=100.)
    report = build_episode_diagnostics(result, rewards)
    assert report["reward_reconciliation"]["reconciliation_ok"] is None
    result["entry_observations"].append({**result["entry_observations"][0], "decision_id": "D2"})
    with pytest.raises(ValueError, match="multiple decisions"):
        build_episode_diagnostics(result, rewards)


def test_changed_order_without_allocator_replay_keeps_impact_unknown():
    rows = [{"bar_time": "2024-01-01", "symbol": symbol, "strategy": "S", "native_score": native,
             "allocation_score": allocation, "ranking_score": allocation, "target_qty": 2., "approved_qty": 1.}
            for symbol, native, allocation in (("A", 2., 1.), ("B", 1., 2.))]
    report = ranking_observations(rows)
    assert report["batches"][0]["order_changed"] is True
    assert report["batches"][0]["observed_budget_limited"] is True
    assert report["ranking_impact"] == "unknown"


def test_existing_run_audit_preserves_source_and_marks_legacy_facts(tmp_path):
    source, target = tmp_path / "original", tmp_path / "audit"
    episode = source / "episodes" / "validation_native"
    episode.mkdir(parents=True)
    curve = pd.DataFrame({"equity": [100., 100.]}, index=pd.date_range("2024-01-01", periods=2))
    curve.to_csv(episode / "equity.csv", index_label="bar_time")
    equity_rewards(curve, [], initial_capital=100.).to_csv(episode / "rewards.csv", index_label="bar_time")
    pd.DataFrame(columns=["order_id"]).to_csv(episode / "trades.csv", index=False)
    pd.DataFrame([{"timestamp": "2024-01-01", "symbol": "A", "accepted": False},
                  {"timestamp": "2024-01-01", "symbol": "B", "accepted": False}]).to_csv(episode / "allocation.csv", index=False)
    (episode / "summary.json").write_text(json.dumps({"initial_capital": 100., "terminal_policy": "forced_liquidation"}), encoding="utf-8")
    before = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    report = audit_existing_run(source, target)
    assert report["candidate_date_review"][0]["candidate_dates"] == 1
    assert report["candidate_date_review"][0]["competition_dates"] == 1
    assert report["candidate_date_review"][0]["upstream_rejections_known"] is False
    assert report["episodes"]["validation_native"]["decision_ledger"][0]["decision_id"] is None
    after = {p.relative_to(source).as_posix(): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert before == after
    with pytest.raises(FileExistsError):
        audit_existing_run(source, target)
    with pytest.raises(ValueError, match="outside"):
        audit_existing_run(source, source / "new")


@pytest.mark.parametrize("tamper", ["artifact", "protocol"])
def test_existing_run_refuses_changed_artifact_or_protocol_content(tmp_path, tamper):
    import hashlib
    source = tmp_path / "source"
    episode = source / "episodes" / "native"
    episode.mkdir(parents=True)
    curve = pd.DataFrame({"equity": [100.]}, index=[pd.Timestamp("2024-01-01")])
    curve.to_csv(episode / "equity.csv", index_label="bar_time")
    equity_rewards(curve, [], initial_capital=100.).to_csv(episode / "rewards.csv", index_label="bar_time")
    (episode / "summary.json").write_text(json.dumps({"initial_capital": 100.}), encoding="utf-8")
    protocol = {"settings": {}, "created_at": "2024-01-01"}
    protocol["protocol_id"] = hashlib.sha256(canonical_json(protocol).encode()).hexdigest()
    (source / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    registry = {p.relative_to(source).as_posix(): sha256_file(p) for p in source.rglob("*") if p.is_file()}
    if tamper == "artifact":
        (episode / "summary.json").write_text(json.dumps({"initial_capital": 200.}), encoding="utf-8")
    else:
        protocol["created_at"] = "2025-01-01"
        (source / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
        # Even a recomputed outer file digest cannot repair the frozen id.
        registry["protocol.json"] = sha256_file(source / "protocol.json")
    (source / "artifacts.json").write_text(json.dumps(registry), encoding="utf-8")
    target = tmp_path / "diagnostic"
    with pytest.raises(ValueError, match="changed"):
        audit_existing_run(source, target)
    assert not target.exists()
