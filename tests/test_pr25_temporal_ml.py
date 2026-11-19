"""PR25-03/04/05: actual cutoff, immutable label joins and relisting gaps."""

from copy import deepcopy
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from core.market.data_versions import DataVersionStore
from core.market.membership import membership_as_of
from signal_meta.signal_label_versions import OutcomeRevisionBook
from core.market.temporal_data import TemporalDataError, TemporalOHLCV
from research.ml_selection.dataset import (
    FEATURE_COLUMNS,
    build_dataset,
    chronological_split,
    feature_snapshot,
)
from research.ml_selection.forward_bridge import (
    capture_hook_state,
    bridge_decision,
    validate_hook_state,
)
from research.ml_selection.label_materialization import materialize_training_labels
from research.ml_selection.membership import audit_membership
from research.ml_selection.selector import ResearchSelector
from research.ml_selection.temporal_context import SelectionTemporalContext
from tests.test_ml_selection_pipeline import candidate, market, ScoreModel
from tests.test_ml_selection_next_forward import full_context, FixedPolicy, resign
from tests.test_signal_label_versions import versioned_facts, correct, seal
from signal_meta.signal_meta_layer import _validate_input


class StrictScoreModel(ScoreModel):
    metadata = {"train_latest_label_available_at": "2020-01-01T00:00:00Z"}


def documented(n=95):
    frame = market(n)["A/USDT"]
    frame["available_at"] = frame.index.tz_localize("UTC") + pd.Timedelta(days=1)
    frame["observed_at"] = frame["available_at"]
    frame["revision_id"] = "original"
    frame.attrs["availability_evidence"] = {
        "kind": "source_publication",
        "reference": "synthetic:published-archive",
    }
    return frame


def membership(symbols=("A/USDT", "B/USDT")):
    return {
        s: {
            "listed_at": "2019-01-01T00:00:00Z",
            "available_at": "2019-01-01T00:00:00Z",
            "observed_at": "2019-01-01T00:00:00Z",
            "source": "synthetic:exchange-listing",
        }
        for s in symbols
    }


def test_strict_local_and_published_do_not_share_late_observation_identity():
    frame = documented()
    cutoff = pd.Timestamp(frame.index[-1], tz="UTC") + pd.Timedelta(days=1)
    frame.iloc[-1, frame.columns.get_loc("observed_at")] = cutoff + pd.Timedelta(days=2)
    local = SelectionTemporalContext(cutoff, {"mode": "strict", "knowledge": "local"})
    published = SelectionTemporalContext(cutoff, {"mode": "strict", "knowledge": "published"})
    a = feature_snapshot(frame, temporal_context=local)
    b = feature_snapshot(frame, temporal_context=published)
    assert a["bar_time"] == pd.Timestamp(frame.index[-2], tz="UTC")
    assert b["bar_time"] == pd.Timestamp(frame.index[-1], tz="UTC")
    assert a["temporal_context_id"] != b["temporal_context_id"]
    assert a["source_versions_sha256"] != b["source_versions_sha256"]


def test_real_cutoff_nanosecond_boundary_and_revision_do_not_rewrite_prefix():
    frame = documented()
    close = pd.Timestamp(frame.index[-1], tz="UTC") + pd.Timedelta(days=1)
    late = close + pd.Timedelta(microseconds=1)
    frame.iloc[-1, frame.columns.get_loc("available_at")] = late
    frame.iloc[-1, frame.columns.get_loc("observed_at")] = late
    source = TemporalOHLCV("test:A", timeframe="1d", policy={"mode": "strict"})
    source.ingest(frame)
    before = SelectionTemporalContext(late - pd.Timedelta(nanoseconds=1))
    on = SelectionTemporalContext(late)
    assert before.identity != on.identity
    assert on.identity != SelectionTemporalContext(late + pd.Timedelta(nanoseconds=1)).identity
    assert feature_snapshot(source, temporal_context=before)["bar_time"] == pd.Timestamp(
        frame.index[-2], tz="UTC"
    )
    assert feature_snapshot(source, temporal_context=on)["bar_time"] == pd.Timestamp(
        frame.index[-1], tz="UTC"
    )
    old = feature_snapshot(source, temporal_context=on)
    amendment = frame.iloc[[-1]].copy()
    amendment[["open", "high", "low", "close"]] = [200.0, 202.0, 198.0, 200.0]
    amendment["revision_id"] = "later-correction"
    amendment["available_at"] = amendment["observed_at"] = late + pd.Timedelta(days=1)
    source.ingest(amendment)
    assert feature_snapshot(source, temporal_context=on) == old
    future = feature_snapshot(
        source, temporal_context=SelectionTemporalContext(late + pd.Timedelta(days=1))
    )
    assert future["return_1d"] != old["return_1d"]


def test_strict_unknown_availability_excludes_or_raises_without_nominal_close_proof():
    raw = market(95)["A/USDT"]
    cutoff = pd.Timestamp(raw.index[-1], tz="UTC") + pd.Timedelta(days=1)
    row = feature_snapshot(raw, temporal_context=SelectionTemporalContext(cutoff))
    assert not row["history_available"] and np.isnan(row["return_20d"])
    with pytest.raises(TemporalDataError, match="availability unknown"):
        feature_snapshot(
            raw,
            temporal_context=SelectionTemporalContext(
                cutoff, {"mode": "strict", "unknown": "raise"}
            ),
        )


def strict_serving():
    frames = {symbol: documented() for symbol in ("A/USDT", "B/USDT")}
    bar = frames["A/USDT"].index[80]
    cutoff = pd.Timestamp(bar, tz="UTC") + pd.Timedelta(days=1, seconds=7)
    temporal = SelectionTemporalContext(cutoff)
    dataset = build_dataset(frames, temporal_contexts=[temporal], membership=membership())
    # The full stored frame contains later candles; strict view has exactly the visible prefix.
    views = {symbol: temporal.visible(frame, symbol=symbol) for symbol, frame in frames.items()}
    ctx = full_context()
    event = ctx["event"]
    ctx["event"] = replace(event, source="historical_strict", histories=views)
    return temporal, dataset, ctx


def test_temporal_dataset_has_no_proxy_training_label_and_selector_uses_actual_cutoff():
    temporal, dataset, ctx = strict_serving()
    assert dataset.as_of.eq(temporal.decision_cutoff).all()
    assert dataset.eligible.all() and dataset.history_available.all()
    assert dataset.label_net_return.isna().all()
    assert dataset.label_basis.eq("strict_features_no_proxy_label").all()
    selector = ResearchSelector(dataset, mode="momentum")
    result = selector.select([candidate(), candidate("B/USDT")], **ctx)
    assert len(result) == 2
    assert all(row["as_of"] == temporal.decision_cutoff.isoformat() for row in selector.audit)
    assert all(row["temporal_mode"] == "strict" for row in selector.audit)
    assert not any(name.startswith("label_") for name in selector.table)


def test_strict_selector_refuses_feature_tampering_and_source_substitution():
    _, dataset, ctx = strict_serving()
    altered = dataset.copy()
    altered.loc[0, "return_20d"] = 12345.0
    selector = ResearchSelector(altered, mode="momentum")
    assert len(selector.select([candidate(), candidate("B/USDT")], **ctx)) == 1
    assert selector.audit[0]["reason"] == "strict_feature_or_source_identity_mismatch"
    bad = dict(ctx)
    event = ctx["event"]
    histories = {symbol: frame.copy() for symbol, frame in event.histories.items()}
    histories["A/USDT"].attrs["temporal_source_versions"] = {}
    bad["event"] = replace(event, histories=histories)
    selector = ResearchSelector(dataset, mode="momentum")
    with pytest.raises(ValueError, match="causal source close"):
        selector.select([candidate()], **bad)


def test_v2_bridge_binds_delayed_cutoff_and_replays_same_strict_features():
    temporal, dataset, ctx = strict_serving()
    snapshot = capture_hook_state(
        [candidate(), candidate("B/USDT")],
        protocol_id="protocol",
        policy_id=FixedPolicy.model_id,
        parent_model_id=ScoreModel.model_id,
        **ctx,
    )
    assert snapshot["schema"] == "ml-rl-hook-state/v2"
    assert pd.Timestamp(snapshot["as_of"]) == temporal.decision_cutoff
    output = bridge_decision(
        snapshot,
        dataset,
        StrictScoreModel(),
        FixedPolicy(),
        protocol_id="protocol",
        information_cutoff=temporal.decision_cutoff,
        selection_options={"policy_threshold": 0.49},
    )
    assert len(output["selected"]) == 2 and output["decision_only"]
    assert all(
        row["decision_cutoff"] == temporal.decision_cutoff.isoformat()
        for row in output["decisions"]
    )
    assert not output["simulated_account_performance_available"]
    broken = deepcopy(snapshot)
    first = next(iter(broken["source_versions"]["A/USDT"].values()))
    first["record"]["observed_at"] = "2099-01-01T00:00:00Z"
    resign(broken)
    with pytest.raises(ValueError, match="source content mismatch"):
        validate_hook_state(
            broken,
            protocol_id="protocol",
            policy_id=FixedPolicy.model_id,
            parent_model_id=ScoreModel.model_id,
            information_cutoff=temporal.decision_cutoff,
        )


def test_retrospective_default_remains_unchanged_and_v1_bridge_is_retained():
    frames = market(95)
    left = build_dataset(frames, horizon_bars=3)
    right = build_dataset(frames, horizon_bars=3, temporal_contexts=None)
    pd.testing.assert_frame_equal(left, right)
    from tests.test_ml_selection_next_forward import captured

    assert captured()["schema"] == "ml-rl-hook-state/v1"


def book(payload):
    horizons, candidates, _, outcomes = _validate_input(payload)
    return OutcomeRevisionBook(payload, candidates, horizons, outcomes)


def training_rows(revisions, ids):
    return pd.DataFrame(
        [
            {
                **revisions.candidate_context(cid),
                "candidate_id": cid,
                "horizon_bars": 1,
                "eligible": True,
                **dict.fromkeys(FEATURE_COLUMNS, 0.0),
            }
            for cid in ids
        ]
    )


def test_latest_label_invalidation_never_falls_back_and_exact_cutoff_excluded():
    payload = versioned_facts(range(20))
    correct(payload, "c001", "2020-01-15T00:00:00+00:00", invalid=True)
    revisions = book(payload)
    rows = training_rows(revisions, ["c001"])
    before = materialize_training_labels(
        rows, cutoff="2020-01-15T00:00:00Z", revision_book=revisions
    )
    after = materialize_training_labels(
        rows, cutoff="2020-01-15T00:00:00.000000001Z", revision_book=revisions
    )
    assert before.label_training_eligible.iloc[0] and before.label_net_return.notna().all()
    assert not after.label_training_eligible.iloc[0] and after.label_net_return.isna().all()
    assert before.label_revision_id.iloc[0] != after.label_revision_id.iloc[0]
    assert after.label_eligibility_reason.iloc[0] == "censored_invalid_bar"


def test_label_materialization_rejects_identity_date_shift_and_preserves_cost_revision():
    payload = versioned_facts(range(20))
    correct(payload, "c001", "2020-01-15T00:00:00Z")
    revisions = book(payload)
    rows = training_rows(revisions, ["c001"])
    materialized = materialize_training_labels(
        rows, cutoff="2020-01-16T00:00:00Z", revision_book=revisions
    )
    assert materialized.label_net_return.iloc[0] == -0.04
    assert (
        materialized.label_cost_policy_sha256.iloc[0]
        == payload["temporal_label_protocol"]["cost_policy_sha256"]
    )
    shifted = rows.copy()
    shifted["as_of"] = pd.Timestamp("2019-01-01", tz="UTC")
    with pytest.raises(ValueError, match="decision cutoff differs"):
        materialize_training_labels(shifted, cutoff="2020-01-16T00:00:00Z", revision_book=revisions)
    with pytest.raises(ValueError, match="candidate_id"):
        materialize_training_labels(
            rows.drop(columns="candidate_id"),
            cutoff="2020-01-16T00:00:00Z",
            revision_book=revisions,
        )


def test_versioned_chronological_split_selects_label_version_per_fold_boundary():
    payload = versioned_facts(range(20))
    correct(payload, "c001", "2020-01-15T00:00:00Z", invalid=True)
    revisions = book(payload)
    rows = training_rows(revisions, ["c001", "c010", "c017"])
    parts = chronological_split(
        rows,
        train_end="2020-01-10",
        validation_end="2020-01-17",
        test_end="2020-01-25",
        label_book=revisions,
    )
    assert parts["train"].candidate_id.tolist() == ["c001"]
    assert parts["train"].label_training_eligible.all()
    with pytest.raises(ValueError, match="explicit test_end"):
        chronological_split(
            rows, train_end="2020-01-10", validation_end="2020-01-17", label_book=revisions
        )


def intervals():
    return {
        "A/USDT": [
            {
                "listed_at": "2020-01-01",
                "available_at": "2020-01-01",
                "source": "first-listing",
                "delisted_at": "2020-03-15",
                "delisting_available_at": "2020-03-01",
            },
            {"listed_at": "2020-03-25", "available_at": "2020-03-24", "source": "relisting"},
        ]
    }


def test_relisting_gaps_shared_between_dataset_and_membership_audit():
    facts = intervals()
    result = build_dataset(
        {"A/USDT": market(100)["A/USDT"]},
        horizon_bars=3,
        membership=facts,
        membership_require_sources=True,
    )
    audit, report = audit_membership(facts, result[["symbol", "as_of"]])
    # Eligibility also includes feature/liquidity prerequisites; no row can
    # escape the exact interval result consumed by the separate report.
    for day, active in (
        ("2020-03-14", True),
        ("2020-03-15", False),
        ("2020-03-24", False),
        ("2020-03-25", True),
    ):
        chosen = result.as_of.eq(pd.Timestamp(day, tz="UTC"))
        assert bool(result.loc[chosen, "eligible"].iloc[0]) is active
        assert bool(audit.loc[chosen, "membership_eligible"].iloc[0]) is active
    assert report["full_pit_coverage"] is False
    assert result.membership_basis.tolist() == audit.membership_basis.tolist()


def test_overlap_rejected_before_features_and_late_announcement_is_not_backdated():
    facts = intervals()
    facts["A/USDT"][1]["listed_at"] = "2020-03-10"
    with pytest.raises(ValueError, match="overlapping"):
        build_dataset({"A/USDT": market(100)["A/USDT"]}, membership=facts)
    late = {"A": {"listed_at": "2020-01-01", "available_at": "2020-01-03", "source": "listing"}}
    assert not membership_as_of(late, "A", "2020-01-02")["eligible"]
    assert membership_as_of(late, "A", "2020-01-03")["eligible"]
    late["A"].update(delisted_at="2020-01-05", delisting_available_at="2020-01-07")
    assert membership_as_of(late, "A", "2020-01-06")["eligible"]
    assert not membership_as_of(late, "A", "2020-01-07")["eligible"]


def factor_record(revision, available, price=100.0, *, cap=1000.0):
    return {
        "record_id": "cap:A:2024-01-01",
        "revision_id": revision,
        "event_time": "2024-01-02T00:00:00Z",
        "observed_at": available,
        "available_at": available,
        "availability_evidence": {"kind": "source_publication", "reference": "synthetic:metrics"},
        "data": {
            "asset": "A",
            "price_usd": price,
            "market_cap_usd": cap,
            "current_supply": 10.0,
            "market_cap_convention": "current supply * price USD",
        },
    }


def test_factor_input_panel_ignores_future_revisions_and_retains_unknowns(tmp_path):
    from analysis.historical_factor_inputs import historical_factor_input_panel

    store = DataVersionStore(tmp_path / "factor-store")
    original = factor_record("r1", "2024-01-02T00:00:00Z")
    later = factor_record("r2", "2024-01-05T00:00:00Z", 200.0, cap=2000.0)
    member = {
        "A": {
            "listed_at": "2023-01-01",
            "available_at": "2023-01-01",
            "observed_at": "2023-01-01",
            "source": "listing",
        }
    }
    first = store.create_snapshot("test-factor", [original])["snapshot_id"]
    second = store.create_snapshot("test-factor", [original, later])["snapshot_id"]
    a, _ = historical_factor_input_panel(store, first, ["2024-01-03"], membership=member)
    b, report = historical_factor_input_panel(store, second, ["2024-01-03"], membership=member)
    pd.testing.assert_frame_equal(a, b)
    assert a.market_cap_usd.iloc[0] == 1000 and a.input_qualified.iloc[0]
    assert not report["full_market_replication"]
    unknown = deepcopy(later)
    unknown["revision_id"] = "missing-cap"
    unknown["data"]["market_cap_usd"] = None
    unknown["data"]["volume"] = 999999.0
    third = store.create_snapshot("test-factor", [original, unknown])["snapshot_id"]
    missing, _ = historical_factor_input_panel(store, third, ["2024-01-06"], membership=member)
    assert not missing.input_qualified.iloc[0] and pd.isna(missing.market_cap_usd.iloc[0])


def test_strict_fine_bar_refinement_cannot_use_undocumented_candles():
    from signal_meta.paper_label_execution import refine_barrier_labels
    from signal_meta.paper_labels import BarrierConfig, LabelCosts
    from tests.test_paper_labels import bars, candidate as signal

    coarse = bars()
    coarse.iloc[1] = [100.0, 103.0, 97.0, 100.0, 1000.0]
    fine = pd.DataFrame(
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1000.0},
        index=pd.date_range("2020-01-02", periods=24, freq="h", tz="UTC"),
    )
    fine.iloc[0] = [100.0, 103.0, 99.5, 100.0, 1000.0]
    fine.iloc[-1] = [100.0, 100.5, 97.0, 100.0, 1000.0]
    coarse["available_at"] = coarse.index + pd.Timedelta(days=1)
    coarse["observed_at"] = coarse.available_at
    coarse.attrs["availability_evidence"] = {
        "kind": "source_publication",
        "reference": "coarse:archive",
    }
    context = SelectionTemporalContext("2020-01-08T00:00:00Z")
    result = refine_barrier_labels(
        {"BTC/USDT": coarse},
        [signal()],
        config=BarrierConfig(max_holding_bars=2),
        costs=LabelCosts(),
        as_of=context.decision_cutoff,
        fine_frames={"BTC/USDT": fine},
        temporal_context=context,
    )
    assert result["outcomes"][0]["ambiguous"]
    assert not result["outcomes"][0]["training_eligible"]
    fine["available_at"] = fine.index + pd.Timedelta(hours=1)
    fine["observed_at"] = fine.available_at
    fine.attrs["availability_evidence"] = {
        "kind": "source_publication",
        "reference": "fine:archive",
    }
    verified = refine_barrier_labels(
        {"BTC/USDT": coarse},
        [signal()],
        config=BarrierConfig(max_holding_bars=2),
        costs=LabelCosts(),
        as_of=context.decision_cutoff,
        fine_frames={"BTC/USDT": fine},
        temporal_context=context,
    )
    assert verified["outcomes"][0]["refinement_status"] == "resolved"
    assert verified["outcomes"][0]["training_eligible"]


def test_store_precision_limit_is_explicit_instead_of_silently_truncating_nanoseconds():
    frame = documented(1)
    frame["observed_at"] = frame["available_at"] = pd.Timestamp("2020-01-02T00:00:00.000000001Z")
    with pytest.raises(ValueError, match="supported microsecond precision"):
        SelectionTemporalContext("2020-01-03T00:00:00Z").visible(frame, symbol="A")


def test_strict_selector_refuses_known_future_training_metadata():
    _, dataset, ctx = strict_serving()
    selector = ResearchSelector(dataset, model=ScoreModel())
    with pytest.raises(ValueError, match="training/availability exceeds"):
        selector.select([candidate()], **ctx)


def test_one_unmatured_versioned_peer_withholds_whole_decision_date():
    from signal_meta.signal_observation_types import fingerprint

    payload = versioned_facts(range(4))
    original_candidate = deepcopy(payload["candidates"][1])
    peer_candidate = deepcopy(original_candidate)
    peer_candidate["candidate_id"] = "peer"
    payload["candidates"].append(peer_candidate)
    peer_decision = deepcopy(payload["decisions"][1])
    peer_decision["candidate_id"] = "peer"
    payload["decisions"].append(peer_decision)
    peer_label = deepcopy(payload["outcomes"][1])
    peer_label.update(
        candidate_id="peer", available_at="2020-01-08T00:00:00Z", observed_at="2020-01-08T00:00:00Z"
    )
    record = peer_label["source_versions"][0]["record"]
    record.update(
        revision_id="late-peer",
        observed_at="2020-01-08T00:00:00Z",
        available_at="2020-01-08T00:00:00Z",
    )
    peer_label["source_versions"][0]["record_sha256"] = fingerprint(record)
    seal(peer_label)
    payload["outcomes"].append(deepcopy(peer_label))
    payload["outcome_revisions"].append(deepcopy(peer_label))
    revisions = book(payload)
    rows = training_rows(revisions, ["c001", "peer"])
    visible = materialize_training_labels(rows, cutoff="2020-01-06", revision_book=revisions)
    assert visible.label_training_eligible.tolist() == [True, False]
    partition = chronological_split(
        rows,
        train_end="2020-01-06",
        validation_end="2020-01-10",
        test_end="2020-01-12",
        label_book=revisions,
    )
    assert partition["train"].empty


def test_factor_input_unknown_cap_definition_and_false_supply_identity_stay_unqualified(tmp_path):
    from analysis.historical_factor_inputs import historical_factor_input_panel

    store = DataVersionStore(tmp_path / "factor")
    member = {
        "A": {
            "listed_at": "2023-01-01",
            "available_at": "2023-01-01",
            "observed_at": "2023-01-01",
            "source": "listing",
        }
    }
    for name, convention, cap in (
        ("unknown", "unregistered convention", 1000.0),
        ("bad-identity", "current supply * price USD", 2000.0),
    ):
        record = factor_record(name, "2024-01-02T00:00:00Z", cap=cap)
        record["data"]["market_cap_convention"] = convention
        snapshot = store.create_snapshot(name, [record])["snapshot_id"]
        rows, _ = historical_factor_input_panel(store, snapshot, ["2024-01-03"], membership=member)
        assert not rows.input_qualified.iloc[0]
        assert rows.market_cap_usd.isna().all()


def test_strict_account_features_reject_realized_future_mark_prices():
    _, dataset, ctx = strict_serving()
    ctx["current_prices"] = {**ctx["current_prices"], "A/USDT": 999.0}
    selector = ResearchSelector(dataset, mode="momentum")
    with pytest.raises(ValueError, match="causal source close"):
        selector.select([candidate()], **ctx)


def test_later_exit_notice_does_not_revoke_earlier_known_listing_in_audit():
    listing = {"A": {"listed_at": "2020-01-01", "available_at": "2020-01-01", "source": "listing"}}
    decisions = pd.DataFrame({"symbol": ["A"], "as_of": ["2020-01-06"]})
    first, _ = audit_membership(listing, decisions)
    revised = deepcopy(listing)
    revised["A"].update(delisted_at="2020-01-05", delisting_available_at="2020-01-07")
    later, report = audit_membership(revised, decisions)
    assert first.membership_eligible.tolist() == later.membership_eligible.tolist() == [True]
    assert not report["full_pit_coverage"]
    after, _ = audit_membership(revised, pd.DataFrame({"symbol": ["A"], "as_of": ["2020-01-07"]}))
    assert after.membership_eligible.tolist() == [False]
    with pytest.raises(ValueError, match="contradicts"):
        membership_as_of({"A": {**listing["A"], "symbol": "B"}}, "A", "2020-01-06")


def test_resealing_tampered_strict_features_cannot_replace_source_derivation():
    from research.ml_selection.temporal_context import feature_snapshot_identity

    _, rows, ctx = strict_serving()
    altered = rows.copy()
    altered.loc[0, "return_20d"] = 12345.0
    altered.loc[0, "feature_snapshot_sha256"] = feature_snapshot_identity(altered.iloc[0].to_dict())
    selector = ResearchSelector(altered, mode="momentum")
    assert len(selector.select([candidate(), candidate("B/USDT")], **ctx)) == 1
    assert selector.audit[0]["reason"] == "strict_feature_or_source_identity_mismatch"


def test_benchmark_revision_invalidates_other_asset_cached_features_at_same_cutoff():
    raw = {symbol: documented() for symbol in ("A/USDT", "BTC/USDT")}
    bar = raw["A/USDT"].index[80]
    cutoff = pd.Timestamp(bar, tz="UTC") + pd.Timedelta(days=1, seconds=7)
    context = SelectionTemporalContext(cutoff)
    sources = {}
    for symbol, frame in raw.items():
        sources[symbol] = TemporalOHLCV(symbol, timeframe="1d", policy={"mode": "strict"})
        sources[symbol].ingest(frame)
    before = build_dataset(sources, temporal_contexts=[context], membership=membership(tuple(raw)))
    amendment = raw["BTC/USDT"].iloc[[60]].copy()
    amendment[["open", "high", "low", "close"]] = [150.0, 151.0, 149.0, 150.0]
    amendment["revision_id"] = "known-benchmark-correction"
    amendment["observed_at"] = amendment["available_at"] = cutoff - pd.Timedelta(seconds=1)
    sources["BTC/USDT"].ingest(amendment)
    after = build_dataset(sources, temporal_contexts=[context], membership=membership(tuple(raw)))
    old = before.loc[before.symbol == "A/USDT"].iloc[0]
    new = after.loc[after.symbol == "A/USDT"].iloc[0]
    assert old.source_versions_sha256 == new.source_versions_sha256
    assert old.feature_source_versions_sha256 != new.feature_source_versions_sha256
    assert np.isfinite(new.btc_return_20d) and old.btc_return_20d != new.btc_return_20d
    views = {symbol: context.visible(source, symbol=symbol) for symbol, source in sources.items()}
    ctx = full_context()
    ctx["event"] = replace(ctx["event"], histories=views, source="historical_strict")
    ctx["current_prices"] = {symbol: float(view.close.iloc[-1]) for symbol, view in views.items()}
    stale = ResearchSelector(before, model=StrictScoreModel())
    assert stale.select([candidate()], **ctx) == []
    fresh = ResearchSelector(after, model=StrictScoreModel())
    assert len(fresh.select([candidate()], **ctx)) == 1


@pytest.mark.parametrize("bar_time", [None, "2023-12-31T00:00:00Z", "across-boundary"])
def test_actual_receipt_cutoffs_do_not_split_one_immature_decision_cohort(bar_time):
    starts = ["2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"]
    mature = ["2024-01-02T00:00:00Z", "2024-01-04T00:00:00Z"]
    if bar_time == "across-boundary":
        starts = ["2024-01-02T23:00:00Z", "2024-01-03T01:00:00Z"]
        mature = ["2024-01-02T23:30:00Z", "2024-01-03T02:00:00Z"]
    rows = pd.DataFrame(
        [
            {
                **dict.fromkeys(FEATURE_COLUMNS, 0.0),
                "symbol": symbol,
                "as_of": at,
                "eligible": True,
                "label_net_return": value,
                "label_available_at": end,
            }
            for symbol, at, value, end in zip(
                ["early_loss", "late_peer"], starts, [-0.1, 0.1], mature
            )
        ]
    )
    if bar_time is not None:
        rows["bar_time"] = "2024-01-02T00:00:00Z" if bar_time == "across-boundary" else bar_time
    parts = chronological_split(
        rows, train_end="2024-01-03", validation_end="2024-01-05", test_end="2024-01-07"
    )
    assert parts["train"].empty
    if bar_time == "across-boundary":
        assert parts["validation"].empty


def test_strict_local_exit_without_observed_receipt_does_not_borrow_publication():
    facts = {
        "A": {
            "listed_at": "2020-01-01",
            "available_at": "2020-01-01",
            "observed_at": "2020-01-01",
            "source": "listing",
            "delisted_at": "2024-01-04",
            "delisting_available_at": "2024-01-03",
        }
    }
    local = membership_as_of(
        facts, "A", "2024-01-05", require_sources=True, require_observed=True, knowledge="local"
    )
    published = membership_as_of(
        facts, "A", "2024-01-05", require_sources=True, knowledge="published"
    )
    assert local["eligible"] and not published["eligible"]
    facts["A"]["delisting_observed_at"] = "2024-01-06"
    assert membership_as_of(facts, "A", "2024-01-05", require_sources=True, require_observed=True)[
        "eligible"
    ]
    assert not membership_as_of(
        facts, "A", "2024-01-06", require_sources=True, require_observed=True
    )["eligible"]
