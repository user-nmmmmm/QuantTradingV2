"""Local-publication trust, source/training identity and strict consumer counterexamples."""

from copy import deepcopy
from contextlib import closing
import hashlib
import hmac
import json
import sqlite3

import pandas as pd
import pytest

from analysis.execution_cost_publication import (
    LocalExecutionCostPublicationAuthority,
    LocalExecutionCostPublicationVerifier,
)
from core.planning.execution_cost_provider import (
    PublishedExecutionCostProvider,
    FrozenExecutionCostProvider,
    conservative_trading_cost,
    cost_digest,
)
from tests.test_pr25_execution_costs import samples, model

KEY = b"publication-fixture-only-not-production-key" * 2
AUTHORITY = "fixture-local-research-publisher"
PROTOCOL = {
    "schema": "cost-publication-fixture/v1",
    "method": "ridge",
    "purpose": "engineering-only",
}
POLICY = {"mode": "strict", "knowledge": "local", "unknown": "exclude"}


def expires():
    return (pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=3)).isoformat()


@pytest.fixture
def publication(tmp_path):
    authority = LocalExecutionCostPublicationAuthority(
        tmp_path / "publication.sqlite", authority_id=AUTHORITY, signing_key=KEY
    )
    training = authority.freeze_training(
        samples(8),
        protocol=PROTOCOL,
        fit_parameters={"ridge_alpha": 0.01},
        source_payloads={"fixture": {"external_venue_authentication": False}},
    )
    authority.test_feature_receipt = authority.observe_features(
        {
            "symbol": "BTC/USDT",
            "market_identity": "fixture:spot:simulation:USDT",
            "features": {"spread_bps": 2.0, "volatility": 0.01, "requested_notional": 200.0},
        }
    )
    artifact = authority.publish(training, expires_at=expires())
    return authority, training, artifact


def context(artifact, **changes):
    c = {
        "decision_at": artifact["available_at"],
        "features_available_at": artifact["available_at"],
        "features": {"spread_bps": 2.0, "volatility": 0.01, "requested_notional": 200.0},
        "symbol": "BTC/USDT",
        "side": "buy",
        "market_identity": "fixture:spot:simulation:USDT",
        "horizon_days": 7,
        "anchor": "decision_mid",
        "evaluation_kind": "strict",
        "information_policy": POLICY,
    }
    c.update(changes)
    return c


def estimate(publication, **changes):
    authority, _, artifact = publication
    changes.setdefault("feature_observation", authority.test_feature_receipt)
    return PublishedExecutionCostProvider(
        artifact, publication_verifier=authority.verifier()
    ).estimate(context(artifact, **changes))


def aggregate(row, authority, **changes):
    args = {
        "as_of": row.get("predicted_at", pd.Timestamp.now(tz="UTC").isoformat()),
        "horizon_days": 7,
        "evaluation_kind": "strict",
        "information_policy": POLICY,
        "publication_verifier": changes.pop("publication_verifier")
        if "publication_verifier" in changes
        else authority.verifier(),
    }
    args.update(changes)
    return conservative_trading_cost([row], **args)


def rehash(row, field="prediction_sha256"):
    row.pop(field, None)
    row[field] = cost_digest(row)
    return row


def alter_record(authority, sequence, mutate, *, sign=False):
    with closing(sqlite3.connect(authority.path)) as con, con:
        row = json.loads(
            con.execute(
                "SELECT envelope FROM cost_publications WHERE sequence=?", (sequence,)
            ).fetchone()[0]
        )
        mutate(row)
        if sign:
            row.pop("signature", None)
            row.pop("record_sha256", None)
            digest = cost_digest(row)
            row.update(
                record_sha256=digest,
                signature=hmac.new(KEY, digest.encode("ascii"), hashlib.sha256).hexdigest(),
            )
        con.execute(
            "UPDATE cost_publications SET envelope=? WHERE sequence=?", (json.dumps(row), sequence)
        )


@pytest.mark.parametrize("kind", ["strict", "prospective", "retrospective"])
def test_real_local_publication_is_consumed_after_release_without_promoting_training_evidence(
    publication, kind
):
    authority, training, artifact = publication
    row = estimate(
        publication,
        evaluation_kind=kind,
        information_policy=None if kind == "retrospective" else POLICY,
    )
    assert row["status"] == "supported", row
    result = aggregate(
        row,
        authority,
        evaluation_kind=kind,
        information_policy=None if kind == "retrospective" else POLICY,
    )
    assert result["status"] == "supported", result
    assert result["trading_cost_bps"] >= row["total_cost_bps"]
    assert (
        pd.Timestamp(training["observed_at"])
        <= pd.Timestamp(artifact["fitted_model"]["training_cutoff"])
        <= pd.Timestamp(artifact["available_at"])
    )
    assert artifact["fitted_model"]["protocol_sha256"] == cost_digest(PROTOCOL)
    assert artifact["training_evidence"]["asserted_sample_levels"] == ["simulation_fixture"]
    assert not artifact["real_venue_calibration"] and not row["production_approved"]
    assert b"publication-fixture-only" not in authority.path.read_bytes()


@pytest.mark.parametrize("offset,supported", [(-1, False), (0, True), (1, True)])
def test_actual_release_cutoff_preserves_nanoseconds(publication, offset, supported):
    artifact = publication[2]
    at = (pd.Timestamp(artifact["available_at"]) + pd.Timedelta(nanoseconds=offset)).isoformat()
    row = estimate(publication, decision_at=at, features_available_at=at)
    assert (row["status"] == "supported") is supported
    if not supported:
        assert row["reason"] == "model_unavailable_at_decision"


def test_no_backdating_or_arbitrary_model_input_is_exposed(publication):
    authority, training, artifact = publication
    with pytest.raises(TypeError):
        authority.publish(training, expires_at=expires(), published_at="2024-01-01T00:00:00Z")
    with pytest.raises(TypeError):
        authority.publish(training, expires_at=expires(), model=model())
    for field in ("training_cutoff", "available_at", "protocol_sha256", "expires_at"):
        with pytest.raises(ValueError, match="caller-controlled clocks"):
            authority.freeze_training(
                samples(8), protocol=PROTOCOL, fit_parameters={field: "2024-01-01"}
            )
    past = context(
        artifact, decision_at="2024-01-19T00:00:00Z", features_available_at="2024-01-19T00:00:00Z"
    )
    provider = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())
    assert provider.estimate(past)["reason"] == "model_unavailable_at_decision"


def test_future_training_targets_do_not_become_locally_mature(tmp_path):
    authority = LocalExecutionCostPublicationAuthority(
        tmp_path / "p.sqlite", authority_id=AUTHORITY, signing_key=KEY
    )
    rows = samples(8)
    rows[0]["target_available_at"] = expires()
    with pytest.raises(ValueError, match="not_mature"):
        authority.freeze_training(rows, protocol=PROTOCOL)
    with closing(sqlite3.connect(authority.path)) as con, con:
        assert con.execute("SELECT COUNT(*) FROM cost_publications").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: a.update(available_at="2024-01-01T00:00:00Z"),
        lambda a: a["fitted_model"].update(intercept_bps=999.0),
        lambda a: a["publication"].update(authority_id="self-signed"),
        lambda a: a["training_evidence"].update(real_venue_calibration=True),
    ],
)
def test_rehashing_artifact_cannot_upgrade_publication_or_training_facts(publication, mutation):
    authority, _, artifact = publication
    forged = deepcopy(artifact)
    mutation(forged)
    rehash(forged, "model_sha256")
    # Malformed nested v1 artifacts fail immediately; well-formed tampering
    # survives that check but still fails against the independent receipt.
    try:
        provider = PublishedExecutionCostProvider(forged, publication_verifier=authority.verifier())
    except ValueError:
        return
    assert provider.estimate(context(forged))["status"] == "unsupported"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(total_cost_bps=999.0, upper_bps=1000.0),
        lambda r: r.update(available_at="2024-01-01T00:00:00Z"),
        lambda r: r["support_domain"]["feature_ranges"].update(requested_notional=[0, 1e12]),
        lambda r: r["prediction_context"]["features"].update(requested_notional=1e9),
        lambda r: r.update(model_sha256="f" * 64),
        lambda r: r.update(evidence_verification_level="verified_real_calibration"),
        lambda r: r.update(evaluation_kind="prospective"),
    ],
)
def test_strict_consumer_recomputes_cached_prediction_against_trusted_model(publication, mutation):
    row = estimate(publication)
    mutation(row)
    assert aggregate(rehash(row), publication[0])["status"] == "unsupported"


def test_missing_verifier_wrong_key_or_other_authority_cannot_authenticate(publication, tmp_path):
    authority, _, artifact = publication
    row = estimate(publication)
    assert (
        aggregate(row, authority, publication_verifier=None)["reason"]
        == "independently_configured_publication_verifier_required"
    )
    with pytest.raises(ValueError, match="verifier required"):
        PublishedExecutionCostProvider(artifact, publication_verifier=None)
    with pytest.raises(ValueError, match="signature"):
        LocalExecutionCostPublicationVerifier(
            authority.path, authority_id=AUTHORITY, verification_key=b"x" * 32
        )
    with pytest.raises(ValueError, match="authority"):
        LocalExecutionCostPublicationVerifier(
            authority.path, authority_id="other", verification_key=KEY
        )
    with pytest.raises(ValueError, match="missing"):
        LocalExecutionCostPublicationVerifier(
            tmp_path / "absent", authority_id=AUTHORITY, verification_key=KEY
        )


def test_self_signed_new_authority_is_not_trusted_by_existing_consumer(publication, tmp_path):
    other = LocalExecutionCostPublicationAuthority(
        tmp_path / "other.sqlite", authority_id="self", signing_key=b"x" * 32
    )
    receipt = other.freeze_training(samples(8), protocol=PROTOCOL)
    artifact = other.publish(receipt, expires_at=expires())
    feature = other.observe_features(
        {k: context(artifact)[k] for k in ("symbol", "market_identity", "features")}
    )
    at = pd.Timestamp.now(tz="UTC").isoformat()
    row = PublishedExecutionCostProvider(artifact, publication_verifier=other.verifier()).estimate(
        context(artifact, decision_at=at, features_available_at=at, feature_observation=feature)
    )
    assert row["status"] == "supported"
    assert aggregate(row, publication[0])["reason"] == "untrusted_publication_authority"


def test_journal_tampering_or_missing_training_receipt_rejects(publication):
    authority, _, artifact = publication
    row = estimate(publication)
    alter_record(
        authority, 1, lambda r: r["payload"]["samples"][0].update(execution_cost_bps=1000.0)
    )
    with pytest.raises(ValueError, match="signature"):
        authority.verifier()
    # A consumer already configured before tampering rechecks the journal.
    verifier = object.__new__(LocalExecutionCostPublicationVerifier)
    verifier.path, verifier.authority_id, verifier._key, verifier._verified_models = (
        authority.path,
        AUTHORITY,
        KEY,
        {},
    )
    assert (
        aggregate(row, authority, publication_verifier=verifier)["reason"]
        == "publication_signature_mismatch"
    )


def test_trusted_authority_replay_detects_nonreproducible_model(publication):
    authority, _, _ = publication

    def tamper(record):
        model = record["payload"]["fitted_model"]
        model["intercept_bps"] += 10
        rehash(model, "model_sha256")

    alter_record(authority, 3, tamper, sign=True)
    records = authority.verifier()._records()
    release = next(r for r in records.values() if r["kind"] == "model_release")
    reference = {
        "authority_id": AUTHORITY,
        "record_id": release["record_id"],
        "record_sha256": release["record_sha256"],
    }
    with pytest.raises(ValueError, match="does_not_match_frozen_training"):
        authority.verifier().resolve_model(reference, as_of=pd.Timestamp.now(tz="UTC").isoformat())


def test_frozen_source_or_dependency_changes_refuse_new_publication_and_consumption(
    publication, monkeypatch
):
    import analysis.execution_cost_publication as module

    authority, training, artifact = publication
    baseline = module._runtime()
    monkeypatch.setattr(module, "_runtime", lambda: {**baseline, "numpy": "future-version"})
    with pytest.raises(ValueError, match="runtime_or_source_changed"):
        authority.publish(training, expires_at=expires())
    row = PublishedExecutionCostProvider(
        artifact, publication_verifier=authority.verifier()
    ).estimate(context(artifact))
    assert row["reason"] == "publication_runtime_or_source_changed"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"symbol": "UNKNOWN/USDT"}, "unsupported_symbol"),
        ({"side": "short"}, "unsupported_side"),
        ({"market_identity": "other"}, "unsupported_market_identity"),
        ({"horizon_days": 1}, "target_anchor_or_horizon_mismatch"),
        ({"anchor": "arrival"}, "target_anchor_or_horizon_mismatch"),
        (
            {"features": {"spread_bps": 100.0, "volatility": 0.01, "requested_notional": 200.0}},
            "feature_outside_support:spread_bps",
        ),
    ],
)
def test_v2_preserves_frozen_domain_and_horizon_checks(publication, changes, reason):
    authority, _, artifact = publication
    c = context(artifact, **changes)
    receipt = authority.observe_features(
        {k: c[k] for k in ("symbol", "market_identity", "features")}
    )
    at = pd.Timestamp.now(tz="UTC").isoformat()
    assert (
        estimate(
            publication,
            **{
                **changes,
                "feature_observation": receipt,
                "decision_at": at,
                "features_available_at": at,
            },
        )["reason"]
        == reason
    )


def test_expiry_future_feature_published_semantics_and_legacy_fail_closed(publication):
    authority, _, artifact = publication
    assert estimate(publication, decision_at=artifact["expires_at"])["reason"] == "expired_model"
    future = (pd.Timestamp(artifact["available_at"]) + pd.Timedelta(nanoseconds=1)).isoformat()
    assert estimate(publication, features_available_at=future)["reason"] == "future_feature"
    published = {**POLICY, "knowledge": "published"}
    assert (
        estimate(publication, information_policy=published)["reason"]
        == "external_published_model_evidence_unavailable"
    )
    assert (
        aggregate(estimate(publication), authority, information_policy=published)["reason"]
        == "external_published_model_evidence_unavailable"
    )
    assert (
        FrozenExecutionCostProvider(model()).estimate(context(artifact))["status"] == "unsupported"
    )
    retrospective = estimate(publication, evaluation_kind="retrospective", information_policy=None)
    assert (
        aggregate(retrospective, authority)["reason"]
        == "retrospective_prediction_not_a_strict_publication_request"
    )


def test_live_assertions_remain_locally_frozen_assertions_not_external_authentication(tmp_path):
    rows = samples(8)
    for row in rows:
        row["evidence_verification_level"] = "verified_live_complete_cost"
    authority = LocalExecutionCostPublicationAuthority(
        tmp_path / "p.sqlite", authority_id=AUTHORITY, signing_key=KEY
    )
    training = authority.freeze_training(rows, protocol=PROTOCOL)
    artifact = authority.publish(training, expires_at=expires())
    assert (
        artifact["training_evidence"]["authentication"]
        == "local_snapshot_only_not_external_venue_authentication"
    )
    assert not artifact["real_venue_calibration"]


def test_independent_read_only_verifier_can_consume_actual_persisted_release(publication):
    authority, _, artifact = publication
    verifier = LocalExecutionCostPublicationVerifier(
        authority.path, authority_id=AUTHORITY, verification_key=KEY
    )
    provider = PublishedExecutionCostProvider(artifact, publication_verifier=verifier)
    assert (
        provider.estimate(context(artifact, feature_observation=authority.test_feature_receipt))[
            "status"
        ]
        == "supported"
    )
    assert (
        authority.verifier().resolve_model(artifact["publication"], as_of=artifact["available_at"])
        == artifact
    )
    assert hashlib.sha256(authority.path.read_bytes()).hexdigest()


def test_invalid_credentials_and_input_snapshots_reject(tmp_path):
    for identity, key in (("", KEY), (AUTHORITY, b"short"), (AUTHORITY, "not-bytes")):
        with pytest.raises(ValueError):
            LocalExecutionCostPublicationAuthority(
                tmp_path / "p.sqlite", authority_id=identity, signing_key=key
            )
    authority = LocalExecutionCostPublicationAuthority(
        tmp_path / "valid.sqlite", authority_id=AUTHORITY, signing_key=KEY
    )
    with pytest.raises(ValueError, match="nonempty"):
        authority.freeze_training([], protocol=PROTOCOL)
    with pytest.raises(ValueError, match="nonempty"):
        authority.freeze_training(samples(8), protocol={})
    with pytest.raises(ValueError, match="receipt_missing"):
        authority.publish({}, expires_at=expires())
    receipt = authority.freeze_training(samples(8), protocol=PROTOCOL)
    with pytest.raises(ValueError):
        authority.publish(receipt, expires_at="2000-01-01T00:00:00Z")


def test_clock_regression_does_not_backdate_a_training_freeze_or_release(publication, monkeypatch):
    import analysis.execution_cost_publication as module

    authority, training, _ = publication
    monkeypatch.setattr(module, "_now", lambda: pd.Timestamp("2025-01-01T00:00:00Z"))
    with pytest.raises(ValueError, match="clock_regression"):
        authority.publish(training, expires_at=expires())
    with pytest.raises(ValueError, match="clock_regression"):
        authority.freeze_training(samples(8), protocol=PROTOCOL)


def test_naive_or_invalid_consumer_mode_and_mutation_are_rejected(publication):
    authority, _, artifact = publication
    assert estimate(publication, decision_at="2026-01-01")["status"] == "unsupported"
    assert estimate(publication, evaluation_kind=False)["status"] == "unsupported"
    provider = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())
    provider.artifact["available_at"] = "2024-01-01T00:00:00Z"
    assert provider.estimate(context(artifact))["reason"] == "published_cost_artifact_changed"
    forged = deepcopy(artifact)
    forged["available_at"] = "2024-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="hash mismatch"):
        PublishedExecutionCostProvider(forged, publication_verifier=authority.verifier())


def test_feature_receipt_prevents_self_backdating_or_replacing_input_values(publication):
    authority, _, artifact = publication
    receipt = authority.observe_features(
        {k: context(artifact)[k] for k in ("symbol", "market_identity", "features")}
    )
    row = estimate(publication, feature_observation=receipt)
    assert row["reason"] == "features_not_locally_observed_at_decision"
    forged_values = {"spread_bps": 1.0, "volatility": 0.01, "requested_notional": 100.0}
    assert (
        estimate(publication, features=forged_values)["reason"]
        == "feature_inputs_do_not_match_local_observation"
    )
    assert (
        estimate(publication, feature_observation={**receipt, "record_id": "self-signed"})["status"]
        == "unsupported"
    )
    assert (
        estimate(publication, feature_observation={**receipt, "authority_id": "self-signed"})[
            "status"
        ]
        == "unsupported"
    )
    assert estimate(publication, feature_observation=None)["status"] == "unsupported"


def test_datetime_input_clock_normalizes_without_losing_nanoseconds(publication):
    _, _, artifact = publication
    at = pd.Timestamp(artifact["available_at"]).to_pydatetime()
    row = estimate(publication, decision_at=at, features_available_at=at)
    assert row["status"] == "supported"
    assert isinstance(row["prediction_context"]["decision_at"], str)


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"symbol": "", "market_identity": "x", "features": {"x": 1}},
        {"symbol": "s", "market_identity": "x", "features": {}},
        {"symbol": "s", "market_identity": "x", "features": {"x": float("nan")}},
        {"symbol": "s", "market_identity": "x", "features": {"": 1}},
        {"symbol": "s", "market_identity": "x", "features": {"x": True}},
    ],
)
def test_malformed_input_observation_is_not_signed(publication, inputs):
    with pytest.raises(ValueError):
        publication[0].observe_features(inputs)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m.update(schema="self-signed/v3"),
        lambda m: m.update(protocol_sha256="bad"),
        lambda m: m.update(expires_at=m["available_at"]),
        lambda m: m.update(unit="percent"),
        lambda m: m.update(horizon_days=True),
        lambda m: m.update(minimum_stratum_samples=0),
        lambda m: m.update(feature_names=["duplicate", "duplicate"]),
        lambda m: m.update(coefficients=[1]),
        lambda m: m.update(feature_scale=[0, 1, 1]),
        lambda m: m.update(intercept_bps=float("inf")),
        lambda m: m.update(residual_lower_bps=100, residual_upper_bps=-100),
    ],
)
def test_v2_cannot_wrap_malformed_or_self_signed_fitted_artifacts(publication, mutation):
    artifact = deepcopy(publication[2])
    fitted = artifact["fitted_model"]
    mutation(fitted)
    if not isinstance(fitted.get("intercept_bps"), float) or fitted["intercept_bps"] != float(
        "inf"
    ):
        rehash(fitted, "model_sha256")
        rehash(artifact, "model_sha256")
    with pytest.raises(ValueError):
        PublishedExecutionCostProvider(artifact, publication_verifier=publication[0].verifier())


def test_changed_fitted_model_and_invalid_information_policy_refuse(publication):
    provider = FrozenExecutionCostProvider(model())
    provider.model["intercept_bps"] += 1
    assert provider.estimate(context(publication[2]))["reason"] == "cost model changed after freeze"
    for policy in ({"mode": "strict", "knowledge": "unknown"}, {"knowledge": "local"}):
        assert estimate(publication, information_policy=policy)["status"] == "unsupported"


@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol_sha256", "x"),
        ("model_sha256", "x"),
        ("unit", "percent"),
        ("horizon_days", 1),
        ("available_at", "2099-01-01T00:00:00Z"),
        ("expires_at", "2000-01-01T00:00:00Z"),
        ("lower_bps", 500),
    ],
)
def test_rehashed_legacy_forecast_cannot_skip_consumer_contract(field, value):
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    c = {
        "decision_at": "2024-01-10T00:00:00Z",
        "horizon_days": 7,
        "symbol": "BTC/USDT",
        "side": "buy",
    }
    row = FixedExecutionCostProvider(20).estimate(c)
    row[field] = value
    result = conservative_trading_cost([rehash(row)], as_of=c["decision_at"], horizon_days=7)
    assert result["status"] == "unsupported"


def test_legacy_constant_assumptions_validate_params_and_context():
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    for kwargs in (
        {"horizon_days": 0},
        {"protocol_sha256": "bad"},
        {"available_at": "2024-01-01T00:00:00Z", "expires_at": "2023-01-01T00:00:00Z"},
    ):
        with pytest.raises(ValueError):
            FixedExecutionCostProvider(20, **kwargs)
    c = {
        "decision_at": "2024-01-10T00:00:00Z",
        "horizon_days": 7,
        "symbol": "BTC/USDT",
        "side": "buy",
    }
    for mutation in (
        {"decision_at": "2200-01-01T00:00:00Z"},
        {"horizon_days": 1},
        {"side": "unknown"},
    ):
        assert FixedExecutionCostProvider(20).estimate({**c, **mutation})["status"] == "unsupported"
    provider = FixedExecutionCostProvider(20)
    provider.parameters["bps"] += 1
    assert provider.estimate(c)["status"] == "unsupported"


def test_malformed_cached_context_or_incomplete_verifier_fails_closed(publication):
    from types import SimpleNamespace

    row = estimate(publication)
    row["prediction_context"] = []
    assert (
        aggregate(rehash(row), publication[0])["reason"]
        == "published_prediction_context_must_be_mapping"
    )
    incomplete = SimpleNamespace(resolve_model=lambda *args, **kwargs: publication[2])
    with pytest.raises(ValueError, match="verifier required"):
        PublishedExecutionCostProvider(publication[2], publication_verifier=incomplete)
