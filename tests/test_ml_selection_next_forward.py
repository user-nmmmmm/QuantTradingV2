"""Causal RL account bridges and immutable forward evidence boundaries."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from core.broker.types import Order, OrderStatus
from core.reproducibility import canonical_json
from research.ml_selection.dataset import build_dataset
from research.ml_selection.forward_bridge import (RecordingSelector, capture_hook_state,
                                                  bridge_decision, validate_hook_state)
from research.ml_selection.forward_evidence import (evaluate_forward_store, collect_public_forward,
                                                   load_public_forward_collection)
from research.ml_selection.protocol import save_json
from research.ml_selection.selector import ResearchSelector
from tests.test_ml_selection_pipeline import market, candidate, context, ScoreModel


class FixedPolicy:
    model_id = "frozen-policy"
    metadata = {"evaluation_threshold": .49}

    def act(self, frame, *, deterministic=True, threshold=.5):
        probabilities = np.full(len(frame), .495)
        return probabilities >= threshold, probabilities


def full_context():
    ctx = context()
    portfolio = ctx["portfolio"]
    portfolio.initial_capital = 10000.
    portfolio.cash = 8000.
    portfolio.positions = {"A/USDT": {"qty": 10., "avg_price": 120.}}
    equity = portfolio.cash + 10 * ctx["current_prices"]["A/USDT"]
    portfolio.get_total_value = lambda prices: equity
    order = Order("B/USDT", "buy", 5., timestamp=ctx["event"].timestamp,
                  id="stable-order", status=OrderStatus.PARTIALLY_FILLED, remaining_qty=3., filled_qty=2.)
    # The same order exists in both books but must count exactly once.
    ctx["broker"] = SimpleNamespace(pending_orders=[order], active_orders=[order])
    budget = {"budget": 500., "open_risk": 100., "pending_risk": 50., "available": 350., "verifiable": True}
    ctx["risk_manager"] = SimpleNamespace(high_water_equity=11000., last_drawdown=1 - equity / 11000.,
        breaker_action="normal", risk_per_trade=.01, circuit_breaker_triggered=False,
        daily_loss_triggered=False, health_assessment=None,
        drawdown_budget=SimpleNamespace(snapshot=lambda: SimpleNamespace(to_dict=lambda: budget)))
    return ctx


def captured(ctx=None):
    ctx = ctx or full_context()
    return capture_hook_state([candidate(), candidate("B/USDT")], protocol_id="protocol",
                              policy_id=FixedPolicy.model_id, parent_model_id=ScoreModel.model_id, **ctx)


def resign(value, key="snapshot_id"):
    value.pop(key, None)
    value[key] = hashlib.sha256(canonical_json(value).encode()).hexdigest()
    return value


def test_complete_account_partial_order_probability_threshold_and_allocation_parity():
    selector = RecordingSelector(ResearchSelector(build_dataset(market(), horizon_bars=3),
        mode="policy", model=ScoreModel(), policy=FixedPolicy(), policy_threshold=.49), "protocol")
    originals = [candidate(), candidate("B/USDT")]
    result = selector.select(originals, **full_context())
    assert len(result) == 2
    assert selector.verification_errors == []
    assert len(selector.snapshots[0]["orders"]) == 1
    assert selector.replay_comparisons[0]["identical"]
    assert selector.replay_comparisons[0]["decision_facts_identical"]
    row = selector.audit[0]
    assert row["selection_probability"] == .495
    assert row["policy_threshold"] == .49
    assert row["pending_count_fraction"] == 1 / 8
    assert row["portfolio_drawdown"] == pytest.approx(1 - 9200 / 11000)
    assert row["cash_fraction"] == pytest.approx(8000 / 9200)
    assert row["gross_exposure_fraction"] == pytest.approx(1200 / 9200)
    assert row["held_count_fraction"] == 1 / 8
    assert result[0].signal is originals[0].signal
    assert row["allocation_score"] == result[0].score


def test_missing_capture_state_does_not_change_original_selector_decisions():
    ctx = full_context()
    del ctx["risk_manager"].drawdown_budget
    selector = RecordingSelector(ResearchSelector(build_dataset(market(), horizon_bars=3),
        mode="policy", model=ScoreModel(), policy=FixedPolicy()), "protocol")
    selected = selector.select([candidate()], **ctx)
    assert len(selected) == 1 and selector.verification_errors[0]["stage"] == "capture"
    assert not selector.snapshots and not selector.replay_comparisons


def test_replay_validation_failure_does_not_change_original_selector_decisions():
    ctx = full_context()
    ctx["risk_manager"].drawdown_budget.snapshot = lambda: SimpleNamespace(to_dict=lambda:
        {"budget": 500., "open_risk": 100., "pending_risk": 50., "available": 999., "verifiable": True})
    selector = RecordingSelector(ResearchSelector(build_dataset(market(), horizon_bars=3),
        mode="policy", model=ScoreModel(), policy=FixedPolicy()), "protocol")
    assert len(selector.select([candidate()], **ctx)) == 1
    assert selector.verification_errors[0]["stage"] == "replay"
    assert not selector.replay_comparisons


@pytest.mark.parametrize("mutation,match", [
    (lambda s: s["portfolio"].pop("cash"), "incomplete RL portfolio"),
    (lambda s: s["risk"].pop("daily_loss_triggered"), "risk capability"),
    (lambda s: s["orders"][0].update(filled_qty=6), "matching order"),
    (lambda s: s["risk"]["budget"].update(available=999), "budget does not reconcile"),
    (lambda s: s.update(as_of="2020-03-24T00:00:00Z"), "daily closed-bar"),
])
def test_bridge_rejects_missing_or_inconsistent_state(mutation, match):
    snapshot = captured()
    mutation(snapshot)
    resign(snapshot)
    with pytest.raises(ValueError, match=match):
        bridge_decision(snapshot, build_dataset(market(), horizon_bars=3), ScoreModel(), FixedPolicy(),
                        protocol_id="protocol", information_cutoff="2020-03-22T00:00:00Z")


def test_bridge_identity_and_stale_closed_bar_are_rejected():
    snapshot = captured()
    snapshot["portfolio"]["cash"] += 1
    with pytest.raises(ValueError, match="identity mismatch"):
        bridge_decision(snapshot, build_dataset(market(), horizon_bars=3), ScoreModel(), FixedPolicy(),
                        protocol_id="protocol", information_cutoff=snapshot["available_at"])
    snapshot = captured()
    with pytest.raises(ValueError, match="stale or unavailable"):
        bridge_decision(snapshot, build_dataset(market(), horizon_bars=3), ScoreModel(), FixedPolicy(),
                        protocol_id="protocol", information_cutoff=pd.Timestamp(snapshot["available_at"]) + pd.Timedelta(days=1))


@pytest.mark.parametrize("risk_changes", [{"breaker_action": "block_new"}, {"breaker_action": "liquidate"},
    {"breaker_action": "locked"}, {"daily_loss_triggered": True}, {"circuit_breaker_triggered": True},
    {"system_health_allows_new_risk": False}])
def test_risk_blocked_bridge_never_emits_an_entry(risk_changes):
    snapshot = captured()
    snapshot["risk"].update(risk_changes)
    resign(snapshot)
    result = bridge_decision(snapshot, build_dataset(market(), horizon_bars=3), ScoreModel(), FixedPolicy(),
        protocol_id="protocol", information_cutoff=snapshot["available_at"])
    assert result["selected"] == [] and result["risk_blocks_new_entries"]
    assert all(not row["selected"] and row["policy_selected_before_risk"] for row in result["decisions"])
    assert not result["simulated_account_performance_available"]


def forward_store(tmp_path, *, rl=False):
    protocol = {"created_at": "2020-01-01T00:00:00Z", "settings": {"splits": {"train_end": "2020-01-01"},
        "next_research": {"forward_observation": {"start": "2020-03-01T00:00:00Z",
            "end": "2020-04-01T00:00:00Z", "minimum_decision_dates": 2, "maturity_horizon_bars": 20}}}}
    resign(protocol, "protocol_id")
    save_json(tmp_path / "candidate.json", {"selected_candidate": "rl" if rl else "ridge",
        "model_id": "policy" if rl else "model", "parent_model_id": "model",
        "frozen_at": "2020-01-02T00:00:00Z"})
    observation = {"observed_at": "2020-03-02T12:00:00Z", "information_cutoff": "2020-03-02T12:00:00Z",
        "protocol_id": protocol["protocol_id"], "frozen_candidate_id": "policy" if rl else "model",
        "source_kind": "immutable_public_forward_receipt", "retrospective_observation": False,
        "membership_evidence": {"basis": "current_trading_membership", "verified_at": "2020-03-02T10:00:00Z",
                                "all_eligible_symbols_verified": True},
        "observations": [{"symbol": "A/USDT", "as_of": "2020-03-02T00:00:00Z", "eligible": True,
            "predicted_value": .01, "features": {}, "entry_not_before": "2020-03-02T12:00:00Z"}]}
    resign(observation, "observation_id")
    save_json(tmp_path / "shadow" / "observation_first.json", observation)
    return protocol, observation


def test_forward_dates_and_maturity_remain_pending_without_invented_account_return(tmp_path):
    protocol, observation = forward_store(tmp_path)
    before = (tmp_path / "shadow" / "observation_first.json").read_bytes()
    result = evaluate_forward_store(tmp_path, protocol, as_of="2020-03-04T00:00:00Z")
    assert result["verified_forward_decision_dates"] == 1
    assert result["proxy_evidence"]["pending_labels"] == 1
    assert result["status"] == "pending_forward_evidence"
    assert result["simulated_account_evidence"]["portfolio_return"] is None
    assert (tmp_path / "shadow" / "observation_first.json").read_bytes() == before


@pytest.mark.parametrize("frozen_at,reason", [
    (None, "candidate_freeze_time_missing"),
    ("not-a-time", "candidate_freeze_time_invalid"),
    ("2020-03-01T00:00:00Z", "candidate_not_frozen_before_registered_start"),
    ("2020-03-02T00:00:00Z", "candidate_not_frozen_before_registered_start"),
])
def test_forward_requires_candidate_frozen_strictly_before_registered_start(tmp_path, frozen_at, reason):
    protocol, _ = forward_store(tmp_path)
    candidate_path = tmp_path / "candidate.json"
    frozen = json.loads(candidate_path.read_text(encoding="utf-8"))
    if frozen_at is None:
        frozen.pop("frozen_at")
    else:
        frozen["frozen_at"] = frozen_at
    save_json(candidate_path, frozen)
    before = candidate_path.read_bytes()
    result = evaluate_forward_store(tmp_path, protocol, as_of="2020-03-04T00:00:00Z")
    assert result["protocol_preregistered_before_start"] is True
    assert result["preregistered_before_start"] is False
    assert result["verified_forward_decision_dates"] == 0
    assert result["proxy_evidence"]["eligible_labels"] == 0
    assert reason in result["pending_reasons"]
    assert "protocol_not_frozen_before_registered_start" not in result["pending_reasons"]
    assert candidate_path.read_bytes() == before


def test_candidate_clock_does_not_repair_late_protocol_registration(tmp_path):
    protocol, observation = forward_store(tmp_path)
    protocol["created_at"] = "2020-03-01T00:00:00Z"
    resign(protocol, "protocol_id")
    observation["protocol_id"] = protocol["protocol_id"]
    resign(observation, "observation_id")
    save_json(tmp_path / "shadow" / "observation_first.json", observation)
    result = evaluate_forward_store(tmp_path, protocol, as_of="2020-03-04T00:00:00Z")
    assert result["candidate_frozen_before_start"] is True
    assert result["protocol_preregistered_before_start"] is False
    assert result["preregistered_before_start"] is False
    assert result["verified_forward_decision_dates"] == 0
    assert "protocol_not_frozen_before_registered_start" in result["pending_reasons"]


def test_next_candidate_freeze_clock_is_stable_across_repeated_freezes(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from research.ml_selection import pipeline
    clock = [datetime(2024, 1, 1, tzinfo=timezone.utc)]
    class ControlledClock:
        @staticmethod
        def now(zone):
            return clock[0]
    monkeypatch.setattr(pipeline, "datetime", ControlledClock)
    model = SimpleNamespace(kind="ridge", model_id="model")
    models = {"ridge": model, "primary": model}
    validation = {"ridge": {"reward_sum": .01, "net_return": .02, "max_drawdown": .01, "accounting_ok": True}}
    protocol = {"settings": {"next_research": {}, "gates": {}}}
    first = pipeline.freeze_candidate(tmp_path, protocol, models, validation)
    original = (tmp_path / "candidate.json").read_bytes()
    assert first["frozen_at"] == "2024-01-01T00:00:00+00:00"
    clock[0] = datetime(2024, 2, 1, tzinfo=timezone.utc)
    assert pipeline.freeze_candidate(tmp_path, protocol, models, validation) == first
    assert (tmp_path / "candidate.json").read_bytes() == original
    validation["ridge"]["reward_sum"] = .02
    with pytest.raises(ValueError, match="already frozen"):
        pipeline.freeze_candidate(tmp_path, protocol, models, validation)


def test_missing_old_candidate_clock_is_not_backfilled_and_v1_freeze_remains_compatible(tmp_path):
    from research.ml_selection import pipeline
    model = SimpleNamespace(kind="ridge", model_id="model")
    models = {"ridge": model, "primary": model}
    validation = {"ridge": {"reward_sum": .01, "net_return": .02, "max_drawdown": .01, "accounting_ok": True}}
    legacy = pipeline.freeze_candidate(tmp_path, {"settings": {}}, models, validation)
    assert "frozen_at" not in legacy
    original = (tmp_path / "candidate.json").read_bytes()
    assert pipeline.freeze_candidate(tmp_path, {"settings": {"next_research": {}}}, models, validation) == legacy
    assert (tmp_path / "candidate.json").read_bytes() == original


def test_mature_proxy_append_is_separate_and_prediction_rewrite_fails(tmp_path):
    protocol, observation = forward_store(tmp_path)
    outcome = {"protocol_id": protocol["protocol_id"], "observed_at": "2020-03-25T12:00:00Z",
        "information_cutoff": "2020-03-25T12:00:00Z", "resolved": [{
            "observation_id": observation["observation_id"], "symbol": "A/USDT", "as_of": "2020-03-02T00:00:00Z",
            "predicted_value": .01, "entry_at": "2020-03-03T00:00:00Z", "label_net_return": .2,
            "label_available_at": "2020-03-24T00:00:00Z", "label_basis": "independent_shadow_proxy_not_portfolio"}]}
    save_json(tmp_path / "shadow" / "outcomes_first.json", outcome)
    result = evaluate_forward_store(tmp_path, protocol, as_of="2020-03-26T00:00:00Z")
    assert result["proxy_evidence"]["mature_labels"] == 1
    assert result["proxy_evidence"]["pending_labels"] == 0
    assert result["simulated_account_evidence"]["portfolio_return"] is None
    outcome["resolved"][0]["predicted_value"] = .2
    save_json(tmp_path / "shadow" / "outcomes_second.json", outcome)
    with pytest.raises(ValueError, match="rewrites the original prediction"):
        evaluate_forward_store(tmp_path, protocol, as_of="2020-03-26T00:00:00Z")


def test_unverified_membership_or_rl_diagnostic_scores_never_count_as_forward_decisions(tmp_path):
    protocol, observation = forward_store(tmp_path, rl=True)
    result = evaluate_forward_store(tmp_path, protocol, as_of="2020-03-04T00:00:00Z")
    assert result["verified_forward_decision_dates"] == 0
    assert result["gaps"]["frozen_rl_state_missing"] == 1
    observation["membership_evidence"] = {}
    observation["rl_decision"] = {"policy_id": "policy", "model_id": "model", "snapshot_id": "state"}
    resign(observation, "observation_id")
    save_json(tmp_path / "shadow" / "observation_first.json", observation)
    result = evaluate_forward_store(tmp_path, protocol, as_of="2020-03-04T00:00:00Z")
    assert result["verified_forward_decision_dates"] == 0 and result["gaps"]["membership_unverified"] == 1


def test_changed_original_observation_and_immature_appended_labels_are_rejected(tmp_path):
    protocol, observation = forward_store(tmp_path)
    observation["observations"][0]["predicted_value"] = .99
    save_json(tmp_path / "shadow" / "observation_first.json", observation)
    with pytest.raises(ValueError, match="changed after recording"):
        evaluate_forward_store(tmp_path, protocol, as_of="2020-03-04T00:00:00Z")


def test_public_intake_missing_source_records_pending_and_preserves_utc_clock(tmp_path):
    fetcher = SimpleNamespace(fetch_ccxt=lambda *args, **kwargs: pd.DataFrame())
    result = collect_public_forward(tmp_path, fetcher=fetcher)
    assert result["status"] == "pending_public_market_data"
    assert not result["manifest"]["historical_pit_membership"]
    assert result["manifest"]["membership_evidence"]["status"] == "pending_public_membership_response"
    assert pd.Timestamp(result["manifest"]["received_at"]).tzinfo is not None
    assert Path(result["folder"]).joinpath("collection.json").exists()


def public_receipt(tmp_path):
    clock = pd.Timestamp.now(tz="UTC")
    times = pd.date_range(clock.normalize() - pd.Timedelta(days=90), periods=90)
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 100000.}, index=times)
    raw = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "isSpotTradingAllowed": True},
                       {"symbol": "ETHUSDT", "status": "TRADING", "isSpotTradingAllowed": True}]}
    exchange = SimpleNamespace(public_get_exchangeinfo=lambda: raw)
    fetcher = SimpleNamespace(fetch_ccxt=lambda *args, **kwargs: frame.copy(),
        _public_clients=SimpleNamespace(get=lambda *args: exchange), request_timeout_ms=100,
        _build_ccxt_proxies=lambda: None)
    return collect_public_forward(tmp_path, fetcher=fetcher)


def test_public_receipt_verifies_market_and_membership_bytes_without_full_universe_claim(tmp_path):
    result = public_receipt(tmp_path)
    frames, evidence = load_public_forward_collection(result["folder"], ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
        information_cutoff=pd.Timestamp.now(tz="UTC"))
    assert len(frames) == 2 and all(len(frame) == 90 for frame in frames.values())
    assert evidence["registered_symbol_count"] == 3 and evidence["collected_registered_symbol_count"] == 2
    assert evidence["verified_symbols"] == ["BTC/USDT", "ETH/USDT"]
    assert not evidence["historical_pit_membership"] and not evidence["full_registered_universe_covered"]
    target = Path(result["folder"]) / "BTC_USDT.csv"
    target.write_text(target.read_text() + "changed", encoding="utf-8")
    with pytest.raises(ValueError, match="market bytes changed"):
        load_public_forward_collection(result["folder"], ["BTC/USDT"], information_cutoff=pd.Timestamp.now(tz="UTC"))


def test_public_receipt_unavailable_clock_and_membership_rewrites_are_rejected(tmp_path):
    result = public_receipt(tmp_path)
    with pytest.raises(ValueError, match="unavailable at the information cutoff"):
        load_public_forward_collection(result["folder"], ["BTC/USDT"], information_cutoff="2020-01-01")
    raw_path = Path(result["folder"]) / "exchange_info_raw.json"
    save_json(raw_path, {"symbols": []})
    with pytest.raises(ValueError, match="current-membership receipt is changed"):
        load_public_forward_collection(result["folder"], ["BTC/USDT"], information_cutoff=pd.Timestamp.now(tz="UTC"))


def test_pipeline_consumes_verified_public_subset_and_keeps_prestart_pending(tmp_path, monkeypatch):
    from research.ml_selection import pipeline
    result = public_receipt(tmp_path / "intake")
    folder = tmp_path / "run"
    save_json(folder / "candidate.json", {"selected_candidate": "ridge", "primary_model": "ridge", "model_id": "model"})
    model = ScoreModel()
    model.model_id = "model"
    monkeypatch.setattr(pipeline, "load_model", lambda path: model)
    now = pd.Timestamp.now(tz="UTC")
    protocol = {"created_at": now.isoformat(), "settings": {"dataset": {"horizon_bars": 20},
        "selection": {}, "splits": {"train_end": "2020-01-01"},
        "next_research": {"forward_observation": {"start": (now.normalize() + pd.Timedelta(days=2)).isoformat(),
                                                    "minimum_decision_dates": 30}}},
        "data_evidence": {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]}}
    resign(protocol, "protocol_id")
    observed = pipeline.shadow(folder, protocol, market_data_dir=result["folder"])
    assert observed["source_kind"] == "immutable_public_forward_receipt"
    assert observed["membership_evidence"]["all_eligible_symbols_verified"]
    assert observed["membership_evidence"]["eligible_registered_symbol_count"] == 2
    assert not observed["membership_evidence"]["full_registered_universe_covered"]
    assessed = evaluate_forward_store(folder, protocol)
    assert assessed["verified_forward_decision_dates"] == 0
    assert assessed["gaps"]["outside_registered_window"] == 1
    assert "registered_forward_window_not_started" in assessed["pending_reasons"]


def test_immature_forward_append_is_rejected(tmp_path):
    protocol, observation = forward_store(tmp_path)
    save_json(tmp_path / "shadow" / "outcomes_future.json", {"protocol_id": protocol["protocol_id"],
        "observed_at": "2020-03-04T00:00:00Z", "information_cutoff": "2020-03-04T00:00:00Z",
        "resolved": [{"observation_id": observation["observation_id"], "symbol": "A/USDT",
            "as_of": "2020-03-02T00:00:00Z", "predicted_value": .01,
            "label_basis": "independent_shadow_proxy_not_portfolio", "label_net_return": .1,
            "entry_time": "2020-03-03T00:00:00Z", "label_available_at": "2020-03-10T00:00:00Z"}]})
    with pytest.raises(ValueError, match="immature"):
        evaluate_forward_store(tmp_path, protocol, as_of="2020-03-05T00:00:00Z")


def test_shadow_ranker_uses_net_return_calibration_for_trade_gate(tmp_path, monkeypatch):
    from research.ml_selection import pipeline
    result = public_receipt(tmp_path / "intake")
    folder = tmp_path / "run"
    save_json(folder / "candidate.json", {"selected_candidate": "lambdarank", "primary_model": "lambdarank", "model_id": "ranker"})
    model = ScoreModel(value=100.)
    model.kind = "lambdarank"
    model.model_id = "ranker"
    model.predict_net_return = lambda frame: np.full(len(frame), -.03)
    monkeypatch.setattr(pipeline, "load_model", lambda path: model)
    protocol = {"created_at": "2020-01-01", "protocol_id": "protocol", "settings": {"dataset": {}, "selection": {}},
                "data_evidence": {"symbols": ["BTC/USDT", "ETH/USDT"]}}
    observed = pipeline.shadow(folder, protocol, market_data_dir=result["folder"])
    assert all(row["eligible"] for row in observed["observations"])
    assert all(row["ranking_score"] == 100. and row["expected_net_return"] == -.03
               and not row["selected_by_score_gate"] for row in observed["observations"])
    assert all(row["score_semantics"].startswith("lambdarank_ranking") for row in observed["observations"])
