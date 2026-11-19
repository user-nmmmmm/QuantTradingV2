"""Independent hand arithmetic, chronology, provenance and optimizer cost wiring."""

from copy import deepcopy

import pandas as pd
import pytest

from analysis.execution_calibration import execution_sample_report, strict_execution_evidence_report
from analysis.execution_cost_research import (
    implementation_shortfall,
    fit_frozen_cost_model,
    evaluate_cost_models,
)
from core.planning.cost_aware_allocation import CostAwareAllocationPolicy, allocate_cost_aware
from core.planning.execution_cost_provider import (
    FixedExecutionCostProvider,
    FrozenExecutionCostProvider,
    conservative_trading_cost,
    cost_digest,
    aware_time,
)


HASH = "a" * 64
CUTOFF = "2024-01-20T00:00:00Z"


def samples(days=12):
    rows = []
    for day in range(days):
        for direction, side in enumerate(("buy", "sell")):
            at = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=day, hours=1)
            # The same bounded feature cycle repeats later; no out-of-domain
            # sample is presented as calibrated execution capacity.
            spread = float(1 + day % 3)
            notional = float(100 + (day % 3) * 100)
            rows.append(
                {
                    "sample_id": f"s{day}-{side}",
                    "parent_order_id": f"p{day}-{side}",
                    "client_order_id": f"c{day}-{side}",
                    "symbol": "BTC/USDT",
                    "side": side,
                    "market_identity": "fixture:spot:simulation:USDT",
                    "decision_at": at.isoformat(),
                    "features_available_at": at.isoformat(),
                    "target_available_at": (at + pd.Timedelta(minutes=1)).isoformat(),
                    "execution_cost_bps": 2 + 3 * spread + direction,
                    "requested_notional": notional,
                    "features": {
                        "spread_bps": spread,
                        "volatility": 0.01,
                        "requested_notional": notional,
                    },
                    "evidence_verification_level": "simulation_fixture",
                }
            )
    return rows


def model(method="ridge", **changes):
    arguments = dict(
        method=method,
        training_cutoff="2024-01-09T00:00:00Z",
        expires_at=CUTOFF,
        protocol_sha256=HASH,
        ridge_alpha=0.01,
    )
    arguments.update(changes)
    return fit_frozen_cost_model(samples(8), **arguments)


def context():
    return {**samples()[18], "horizon_days": 7}


def intent(**changes):
    return {
        "client_order_id": "o1",
        "symbol": "BTC/USD",
        "side": "buy",
        "qty": 10.0,
        "reference_price": 100.0,
        "quote_currency": "USD",
        "decision_at": "2024-01-01T00:00:00Z",
        **changes,
    }


def fill(**changes):
    return {
        "fill_id": "f1",
        "client_order_id": "o1",
        "symbol": "BTC/USD",
        "qty": 4.0,
        "price": 101.0,
        "occurred_at": "2024-01-01T00:01:00Z",
        "available_at": "2024-01-01T00:02:00Z",
        "fee": 1.0,
        "fee_currency": "USD",
        "fee_status": "recorded",
        "fee_available_at": "2024-01-01T00:02:00Z",
        **changes,
    }


def terminal(**changes):
    return {
        "symbol": "BTC/USD",
        "quote_currency": "USD",
        "price": 103.0,
        "occurred_at": "2024-01-02T00:00:00Z",
        "available_at": "2024-01-02T00:00:00Z",
        "independent": True,
        "source_id": "fixture-terminal",
        **changes,
    }


def test_partial_and_zero_fill_full_intent_arithmetic_and_no_account_charge():
    # Executed adverse amount =4*(101-100)=4; unfilled opportunity=6*3=18.
    r = implementation_shortfall(intent(), [fill()], as_of=CUTOFF, terminal=terminal())
    assert r["executed_price_shortfall_bps"] == 100.0
    assert r["order_price_shortfall_bps"] == 220.0
    assert r["order_execution_cost_bps"] == 230.0
    assert r["financing_inclusive_cost_bps"] is None
    assert not r["account_mutation"]
    zero = implementation_shortfall(
        intent(status="rejected"), [], as_of=CUTOFF, terminal=terminal()
    )
    assert zero["filled_qty"] == 0 and zero["order_price_shortfall_bps"] == 300.0
    assert zero["final_order_status"] == "rejected"


def test_sell_rebate_and_third_currency_fee_conversion_are_separate():
    fx = {
        "fill_id": "f1",
        "currency": "BNB",
        "quote_currency": "USD",
        "rate": 20.0,
        "occurred_at": "2024-01-01T00:01:00Z",
        "available_at": "2024-01-01T00:03:00Z",
        "source_id": "fixture-fx",
    }
    r = implementation_shortfall(
        intent(side="sell", qty=4), [fill(fee=-0.1, fee_currency="BNB")], as_of=CUTOFF, fee_fx=[fx]
    )
    assert r["executed_price_amount"] == -4
    assert r["fee_quote_amount"] == -2
    assert r["order_execution_cost_bps"] == -150.0
    assert r["opportunity_amount"] == 0  # No terminal needed for completed intent.
    missing = implementation_shortfall(intent(qty=4), [fill(fee_currency="BNB")], as_of=CUTOFF)
    assert missing["status"] == "insufficient" and missing["fee_quote_amount"] is None
    assert missing["executed_price_shortfall_bps"] == 100.0


def test_missing_residual_terminal_and_late_fee_do_not_turn_into_zero():
    r = implementation_shortfall(intent(), [fill()], as_of=CUTOFF)
    assert r["status"] == "insufficient" and r["order_execution_cost_bps"] is None
    late = implementation_shortfall(
        intent(qty=4), [fill(fee_available_at="2024-02-01T00:00:00Z")], as_of=CUTOFF
    )
    assert late["fee_quote_amount"] is None and late["status"] == "insufficient"
    mature = implementation_shortfall(
        intent(qty=4),
        [fill()],
        as_of=CUTOFF,
        financing={
            "borrow": {"applicable": False, "reason": "unlevered spot"},
            "funding": {"applicable": False, "reason": "spot"},
        },
    )
    assert mature["financing_inclusive_cost_bps"] == 125.0


@pytest.mark.parametrize(
    "mutation", [{"qty": 11.0}, {"price": float("nan")}, {"client_order_id": "other"}]
)
def test_bad_fill_amount_or_identity_rejected(mutation):
    with pytest.raises(ValueError):
        implementation_shortfall(intent(), [fill(**mutation)], as_of=CUTOFF)


def test_replayed_fills_deduplicate_and_conflicting_ids_reject():
    assert (
        implementation_shortfall(intent(qty=4), [fill(), fill()], as_of=CUTOFF)["filled_qty"] == 4
    )
    with pytest.raises(ValueError, match="conflicting"):
        implementation_shortfall(intent(), [fill(), fill(price=102)], as_of=CUTOFF)


def test_fixed_provider_exact_legacy_optimizer_equivalence_and_no_double_fee():
    policy = CostAwareAllocationPolicy(enabled=True, trading_cost_bps=20)
    request = {
        "decision_at": "2024-01-10T00:00:00Z",
        "horizon_days": 7,
        "symbol": "BTC/USDT",
        "side": "buy",
    }
    predicted = FixedExecutionCostProvider(20).estimate(request)
    args = ([0.03], [[0.1]], [0.1], [0.5])
    old, audit = allocate_cost_aware(*args, policy=policy)
    new, predicted_audit = allocate_cost_aware(
        *args, policy=policy, cost_predictions=[predicted], decision_at=request["decision_at"]
    )
    assert old == pytest.approx(new, abs=1e-12)
    assert audit["estimated_transaction_cost_weight"] == pytest.approx(
        predicted_audit["estimated_transaction_cost_weight"]
    )
    assert predicted_audit["cost_provider"]["actual_account_costs_changed"] is False


def test_bad_provider_keeps_mandatory_risk_exit_and_blocks_discretionary_turnover():
    policy = CostAwareAllocationPolicy(enabled=True)
    w, audit = allocate_cost_aware(
        [0.1],
        [[0.01]],
        [0.6],
        [0.2],
        policy=policy,
        cost_predictions=[],
        decision_at="2024-01-10T00:00:00Z",
    )
    assert w == pytest.approx([0.2])
    assert audit["mandatory_turnover_weight"] == pytest.approx(0.4)
    assert audit["discretionary_turnover_weight"] == 0
    assert audit["status"] == "unsupported_cost_provider_mandatory_only"


def test_conservative_scalar_uses_upper_and_nonnegative_rebate_floor():
    request = {
        "decision_at": "2024-01-10T00:00:00Z",
        "horizon_days": 7,
        "symbol": "BTC/USDT",
        "side": "sell",
    }
    a, b = (
        FixedExecutionCostProvider(-2).estimate(request),
        FixedExecutionCostProvider(30).estimate(request),
    )
    assert (
        conservative_trading_cost([a], as_of=request["decision_at"], horizon_days=7)[
            "trading_cost_bps"
        ]
        == 0
    )
    assert (
        conservative_trading_cost([a, b], as_of=request["decision_at"], horizon_days=7)[
            "trading_cost_bps"
        ]
        == 30
    )
    a["total_cost_bps"] = 99
    assert (
        conservative_trading_cost([a], as_of=request["decision_at"], horizon_days=7)["reason"]
        == "prediction_hash_mismatch"
    )


def test_model_is_frozen_and_test_targets_never_change_training_coefficients():
    rows = samples()
    args = dict(
        test_start="2024-01-09T00:00:00Z",
        expires_at=CUTOFF,
        protocol_sha256=HASH,
        as_of=CUTOFF,
        ridge_alpha=0.01,
    )
    a = evaluate_cost_models(rows, **args)
    for r in rows[16:]:
        r["execution_cost_bps"] += 100
    b = evaluate_cost_models(rows, **args)
    assert set(a["models"]) == {"constant", "daily_robust", "ridge"}
    for key in a["models"]:
        assert (
            a["models"][key]["model"]["model_sha256"] == b["models"][key]["model"]["model_sha256"]
        )
    assert a["models"]["ridge"]["metrics"]["equal_day_mae_bps"] < 1.0
    assert b["models"]["ridge"]["metrics"]["equal_day_mae_bps"] > 99.0
    assert not a["real_venue_calibration"] and not a["production_approved"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"decision_at": "2024-01-08T00:00:00Z"}, "model_unavailable_at_decision"),
        ({"decision_at": "2024-02-01T00:00:00Z"}, "expired_model"),
        ({"features_available_at": "2024-01-19T00:00:00Z"}, "future_feature"),
        ({"symbol": "UNKNOWN/USDT"}, "unsupported_symbol"),
        ({"horizon_days": 1}, "target_anchor_or_horizon_mismatch"),
    ],
)
def test_model_prediction_rejects_future_expired_and_domain_mismatches(change, reason):
    r = FrozenExecutionCostProvider(model()).estimate({**context(), **change})
    assert r["status"] == "unsupported" and r["reason"] == reason


def test_unknown_size_support_bad_hash_and_nanosecond_availability_boundary():
    artifact = model()
    provider = FrozenExecutionCostProvider(artifact)
    c = context()
    c["features"] = {**c["features"], "requested_notional": 1e9}
    assert provider.estimate(c)["reason"] == "feature_outside_support:requested_notional"
    artifact["intercept_bps"] += 1
    with pytest.raises(ValueError, match="hash mismatch"):
        FrozenExecutionCostProvider(artifact)
    assert aware_time("2024-01-01T00:00:00.000000001Z") > aware_time("2024-01-01T00:00:00Z")


def test_parent_group_never_splits_and_immature_targets_remain_excluded():
    rows = samples()
    rows[0]["parent_order_id"] = rows[18]["parent_order_id"]
    rows[2]["target_available_at"] = "2024-01-10T00:00:00Z"
    out = evaluate_cost_models(
        rows,
        test_start="2024-01-09T00:00:00Z",
        expires_at=CUTOFF,
        protocol_sha256=HASH,
        as_of=CUTOFF,
    )
    excluded = {r["sample_id"] for r in out["excluded"]}
    assert {rows[0]["sample_id"], rows[18]["sample_id"], rows[2]["sample_id"]} <= excluded
    assert not set(out["train_sample_ids"]) & set(out["test_sample_ids"])
    assert all(
        set(m["model"]["training_sample_ids"]) <= set(out["train_sample_ids"])
        for m in out["models"].values()
    )


def test_late_test_fees_and_sparse_strata_are_not_calibration_success():
    rows = samples()
    rows[18]["target_available_at"] = "2024-02-01T00:00:00Z"
    out = evaluate_cost_models(
        rows,
        test_start="2024-01-09T00:00:00Z",
        expires_at=CUTOFF,
        protocol_sha256=HASH,
        as_of=CUTOFF,
        minimum_stratum_samples=99,
    )
    assert all(m["status"] == "insufficient" for m in out["models"].values())
    assert all(
        any(r["reason"] == "test_target_not_yet_mature" for r in m["unsupported"])
        for m in out["models"].values()
    )


def test_legacy_live_boolean_does_not_become_strict_complete_cost_evidence():
    from tests.test_execution_calibration import facts

    events, requests, quotes = facts()
    result = execution_sample_report(
        events, requests, independent_quotes=quotes, source="live", as_of="2020-02-01T00:00:00Z"
    )
    assert result["real_venue_calibration"]  # Old report contract remains readable.
    assert result["evidence_verification_level"] == "legacy_source_assertion_only"
    assert not result["strict_real_venue_calibration"] and not result["full_cost_evidence_verified"]


def test_strict_adapter_keeps_price_vs_fee_and_parent_coverage_separate(tmp_path):
    from tests.test_execution_readiness import ledger, request, quote
    from analysis.execution_calibration import read_execution_ledger
    import sqlite3
    import json

    path = ledger(tmp_path, environment="live")
    with sqlite3.connect(path) as con:
        raw = json.loads(con.execute("SELECT payload FROM fills").fetchone()[0])
        raw["parent_order_id"] = "parent-1"
        con.execute("UPDATE fills SET payload=?", (json.dumps(raw),))
    rows = read_execution_ledger(path, as_of="2020-02-01T00:00:00Z")
    out = strict_execution_evidence_report(
        [rows], [request()], [quote()], as_of="2020-02-01T00:00:00Z"
    )
    assert out["evidence_verification_level"] == "verified_live_complete_cost"
    assert out["parent_child_evidence"]["status"] == "complete"
    assert not out["strict_real_venue_calibration"]
    rows["fills"][0]["fee_qualification_error"] = "missing_fee"
    degraded = strict_execution_evidence_report(
        [rows], [request()], [quote()], as_of="2020-02-01T00:00:00Z"
    )
    assert degraded["evidence_verification_level"] == "verified_live_price"
    assert degraded["full_cost_evidence_qualified"] == 0


def test_child_fill_burst_does_not_manufacture_independent_stratum_support():
    rows = samples(6)
    for r in rows:
        r["parent_order_id"] = "one-parent-" + r["side"]
    artifact = fit_frozen_cost_model(
        rows,
        method="constant",
        training_cutoff="2024-01-09T00:00:00Z",
        expires_at=CUTOFF,
        protocol_sha256=HASH,
    )
    assert artifact["training_parent_orders"] == 2
    assert (
        FrozenExecutionCostProvider(artifact).estimate(context())["reason"]
        == "insufficient_stratum"
    )


def test_artifact_cannot_relabel_asof_training_as_actual_historical_model_publication():
    artifact = model()
    assert artifact["evaluation_kind"] == "retrospective"
    r = FrozenExecutionCostProvider(artifact).estimate(context())
    assert "not_actual_model_publication" in r["availability_semantics"]
    assert not r["production_approved"]


def test_malformed_but_rehashed_model_and_wrong_notional_feature_reject():
    artifact = model()
    artifact["feature_scale"][0] = 0
    artifact.pop("model_sha256")
    artifact["model_sha256"] = cost_digest(artifact)
    with pytest.raises(ValueError, match="positive"):
        FrozenExecutionCostProvider(artifact)
    rows = samples(8)
    rows[0]["features"]["requested_notional"] += 1
    with pytest.raises(ValueError, match="notional support"):
        fit_frozen_cost_model(
            rows,
            method="ridge",
            training_cutoff="2024-01-09T00:00:00Z",
            expires_at=CUTOFF,
            protocol_sha256=HASH,
        )


def test_terminal_time_preceding_actual_partial_fill_cannot_supply_opportunity_cost():
    with pytest.raises(ValueError, match="terminal clock"):
        implementation_shortfall(
            intent(), [fill()], as_of=CUTOFF, terminal=terminal(occurred_at="2024-01-01T00:00:30Z")
        )


def test_repeated_identical_children_cannot_change_day_parent_weighted_fit_or_scaler():
    rows = samples(8)
    arguments = dict(
        method="ridge",
        training_cutoff="2024-01-09T00:00:00Z",
        expires_at=CUTOFF,
        protocol_sha256=HASH,
    )
    a = fit_frozen_cost_model(rows, **arguments)
    for i in range(20):
        rows.append(
            {**deepcopy(rows[0]), "sample_id": f"child-extra-{i}", "client_order_id": f"extra-{i}"}
        )
    b = fit_frozen_cost_model(rows, **arguments)
    assert a["feature_mean"] == pytest.approx(b["feature_mean"], abs=1e-10)
    assert a["feature_scale"] == pytest.approx(b["feature_scale"], abs=1e-10)
    assert a["coefficients"] == pytest.approx(b["coefficients"], abs=1e-10)
    assert a["intercept_bps"] == pytest.approx(b["intercept_bps"], abs=1e-10)


def test_cost_sample_materialization_requires_actual_decision_and_parent_receipts(tmp_path):
    from tests.test_execution_readiness import ledger, request, quote
    from analysis.execution_calibration import read_execution_ledger
    from analysis.execution_cost_research import cost_samples_from_qualified_execution
    import sqlite3
    import json

    path = ledger(tmp_path, environment="live")
    with sqlite3.connect(path) as con:
        raw = json.loads(con.execute("SELECT payload FROM fills").fetchone()[0])
        raw["parent_order_id"] = "parent-1"
        con.execute("UPDATE fills SET payload=?", (json.dumps(raw),))
    ledgers = [read_execution_ledger(path, as_of="2020-02-01T00:00:00Z")]
    q = quote()
    report = strict_execution_evidence_report(
        ledgers, [request()], [q], as_of="2020-02-01T00:00:00Z"
    )
    decisions = [
        {
            "account_id": "spot",
            "client_order_id": "o1",
            "source_id": "frozen-decision-receipt",
            "decision_at": "2020-01-01T00:00:00Z",
            "features_available_at": "2020-01-01T00:00:00Z",
            "features": {"volatility": 0.01},
        }
    ]
    out = cost_samples_from_qualified_execution(
        report, ledgers, [q], decisions, as_of=report["as_of"]
    )
    assert len(out["samples"]) == 1
    assert out["samples"][0]["execution_cost_bps"] == 0
    assert out["samples"][0]["parent_order_id"] == "binance/spot/parent-1"
    assert out["samples"][0]["features"]["spread_bps"] == 200
    changed = cost_samples_from_qualified_execution(
        report, ledgers, [{**q, "ask": 102.0}], decisions, as_of=report["as_of"]
    )
    assert (
        not changed["samples"]
        and changed["excluded"][0]["reason"] == "qualified_execution_facts_changed"
    )
    decisions[0]["decision_at"] = "2019-12-31T23:59:59Z"
    gap = cost_samples_from_qualified_execution(
        report, ledgers, [q], decisions, as_of=report["as_of"]
    )
    assert not gap["samples"]
    assert gap["excluded"][0]["reason"] == "quote_or_features_unavailable_at_actual_decision"
    ledgers[0]["fills"][0]["parent_order_id"] = None
    gap = cost_samples_from_qualified_execution(report, ledgers, [q], [], as_of=report["as_of"])
    assert not gap["samples"]  # Unknown parent cannot fabricate a singleton group.


@pytest.mark.parametrize("kind", ["strict", "prospective"])
def test_retrospective_cost_model_and_cached_prediction_cannot_enter_strict_consumers(kind):
    provider = FrozenExecutionCostProvider(model())
    strict = {
        **context(),
        "evaluation_kind": kind,
        "information_policy": {"mode": "strict", "knowledge": "local", "unknown": "exclude"},
    }
    assert provider.estimate(strict)["status"] == "unsupported"
    cached = provider.estimate(context())
    assert cached["status"] == "supported"
    result = conservative_trading_cost(
        [cached],
        as_of=context()["decision_at"],
        horizon_days=7,
        evaluation_kind=kind,
        information_policy=strict["information_policy"],
    )
    assert result["status"] == "unsupported"
    # Self-relabeling and recomputing a digest still provides no publication receipt.
    forged = deepcopy(cached)
    forged.pop("prediction_sha256")
    forged.update(evaluation_kind=kind, availability_semantics="actual_model_publication")
    forged["prediction_sha256"] = cost_digest(forged)
    assert (
        conservative_trading_cost(
            [forged], as_of=context()["decision_at"], horizon_days=7, evaluation_kind=kind
        )["status"]
        == "unsupported"
    )
    weights, audit = allocate_cost_aware(
        [0.1],
        [[0.01]],
        [0.6],
        [0.2],
        policy=CostAwareAllocationPolicy(enabled=True),
        cost_predictions=[cached],
        decision_at=context()["decision_at"],
        evaluation_kind=kind,
        information_policy=strict["information_policy"],
    )
    assert weights == pytest.approx([0.2])
    assert audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert audit["discretionary_turnover_weight"] == 0


def test_strict_information_policy_alone_cannot_silently_default_to_retrospective():
    provider = FrozenExecutionCostProvider(model())
    strict = {**context(), "information_policy": {"mode": "strict", "knowledge": "published"}}
    assert provider.estimate(strict)["status"] == "unsupported"
    cached = provider.estimate(context())
    assert (
        conservative_trading_cost(
            [cached],
            as_of=context()["decision_at"],
            horizon_days=7,
            information_policy=strict["information_policy"],
        )["status"]
        == "unsupported"
    )
    fixed = FixedExecutionCostProvider(20)
    assert fixed.estimate(strict)["status"] == "unsupported"
    assert fixed.estimate(context())["status"] == "supported"


def test_cost_mode_conflict_and_rehashed_artifact_upgrade_are_refused():
    from core.planning.execution_cost_provider import cost_evaluation_mode

    with pytest.raises(ValueError, match="conflicts"):
        cost_evaluation_mode("retrospective", {"mode": "strict"})
    artifact = model()
    artifact.pop("model_sha256")
    artifact.update(evaluation_kind="strict", availability_semantics="actual_model_publication")
    artifact["model_sha256"] = cost_digest(artifact)
    with pytest.raises(ValueError, match="actual publication adapter"):
        FrozenExecutionCostProvider(artifact)


@pytest.mark.parametrize("kind", [False, ""])
def test_invalid_cost_evaluation_mode_cannot_use_a_truthiness_retrospective_default(kind):
    from core.planning.execution_cost_provider import cost_evaluation_mode

    with pytest.raises(ValueError, match="evaluation kind"):
        cost_evaluation_mode(kind)


@pytest.mark.parametrize("offset,qualified", [(-1, True), (0, True), (1, False)])
def test_exact_quote_observation_and_availability_boundary_cannot_be_truncated(
    tmp_path, offset, qualified
):
    from tests.test_execution_readiness import ledger, request, quote
    from analysis.execution_calibration import read_execution_ledger, validate_execution_quote
    from analysis.execution_cost_research import cost_samples_from_qualified_execution
    import sqlite3
    import json

    path = ledger(tmp_path, environment="live")
    with sqlite3.connect(path) as con:
        raw = json.loads(con.execute("SELECT payload FROM fills").fetchone()[0])
        raw["parent_order_id"] = "parent-1"
        con.execute("UPDATE fills SET payload=?", (json.dumps(raw),))
    at = pd.Timestamp("2020-01-01T00:00:00Z")
    exact = at + pd.Timedelta(nanoseconds=offset)
    q = quote(
        occurred_at=exact.isoformat(), observed_at=exact.isoformat(), available_at=exact.isoformat()
    )
    normalized = validate_execution_quote(q)
    assert pd.Timestamp(normalized["available_at"]) == exact
    ledgers = [read_execution_ledger(path, as_of="2020-02-01T00:00:00Z")]
    report = strict_execution_evidence_report(
        ledgers, [request()], [q], as_of="2020-02-01T00:00:00Z"
    )
    assert (report["full_cost_evidence_qualified"] == 1) is qualified
    decision = {
        "account_id": "spot",
        "client_order_id": "o1",
        "source_id": "frozen-decision-receipt",
        "decision_at": at.isoformat(),
        "features_available_at": at.isoformat(),
        "features": {"volatility": 0.01},
    }
    samples = cost_samples_from_qualified_execution(
        report, ledgers, [q], [decision], as_of=report["as_of"]
    )
    assert bool(samples["samples"]) is qualified
