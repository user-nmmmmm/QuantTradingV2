"""PR25-19 small real chain: visible inputs -> optimizer -> original account.

All market receipts are synthetic engineering fixtures. Their reproducible
simulated account return must never be promoted to real forward evidence.
"""

import numpy as np
import pandas as pd
import pytest

from core.planning.cost_aware_allocation import CostAwareAllocationPolicy, allocate_cost_aware
from core.planning.covariance_estimators import estimate_covariance
from core.planning.execution_cost_provider import FixedExecutionCostProvider
from research.ml_selection.dataset import build_dataset
from research.ml_selection.temporal_context import SelectionTemporalContext
from research.ml_selection import forward_account_trajectory as accounts


SYMBOLS = ("A/USDT", "B/USDT", "C/USDT")
CUTOFF = "2024-01-01T12:00:00Z"


def _visible_inputs(*, future_revision=False, return_frames=False):
    dates = pd.date_range(end="2023-12-31", periods=95, freq="D")
    frames, members = {}, {}
    for number, symbol in enumerate(SYMBOLS):
        returns = (
            0.004 + number * 0.0003 + 0.0007 * np.sin(np.arange(len(dates)) * (0.3 + number * 0.2))
        )
        path = np.cumprod(1 + returns)
        close = 100 * path / path[-1]
        opening = close / (1 + returns)
        frame = pd.DataFrame(
            {
                "open": opening,
                "high": close * 1.01,
                "low": opening * 0.99,
                "close": close,
                "volume": 100000.0,
            },
            index=dates,
        )
        frame["available_at"] = dates.tz_localize("UTC") + pd.Timedelta(days=1)
        frame["observed_at"] = frame.available_at
        frame["revision_id"] = "original"
        frame.attrs["availability_evidence"] = {
            "kind": "source_publication",
            "reference": "synthetic:pr25-chain",
        }
        if future_revision:
            amendment = frame.iloc[[-1]].copy()
            amendment[["open", "high", "low", "close"]] = [400.0, 405.0, 395.0, 400.0]
            amendment["revision_id"] = "future-amendment"
            amendment["available_at"] = amendment["observed_at"] = pd.Timestamp(
                "2024-01-10T00:00:00Z"
            )
            frame = pd.concat([frame, amendment])
        frames[symbol] = frame
        members[symbol] = [
            {
                "listed_at": "2019-01-01",
                "available_at": "2019-01-01",
                "observed_at": "2019-01-01",
                "source": "first-listing",
                "delisted_at": "2021-01-01",
                "delisting_available_at": "2020-12-01",
                "delisting_observed_at": "2020-12-01",
            },
            {
                "listed_at": "2023-01-01",
                "available_at": "2022-12-01",
                "observed_at": "2022-12-01",
                "source": "relisting",
            },
        ]
    context = SelectionTemporalContext(CUTOFF)
    dataset = build_dataset(frames, temporal_contexts=[context], membership=members)
    assert dataset.eligible.all() and len(dataset) == 3
    assert dataset.as_of.eq(context.decision_cutoff).all()
    assert dataset.label_net_return.isna().all()  # Features alone supply no outcome.
    visible = {symbol: context.visible(frame, symbol=symbol) for symbol, frame in frames.items()}
    panel = (
        pd.DataFrame({symbol: visible[symbol].close for symbol in SYMBOLS})
        .pct_change(fill_method=None)
        .iloc[1:]
        .tail(60)
    )
    assert not panel.isna().any().any()
    if return_frames:
        return context, dataset, panel, frames, members
    return context, dataset, panel


def _account_chain(*, cost_bps, future_revision=False):
    context, features, visible_returns = _visible_inputs(future_revision=future_revision)
    estimate = estimate_covariance(visible_returns, estimator_id="fixed_diag_020")
    policy = CostAwareAllocationPolicy(
        enabled=True,
        horizon_days=7,
        risk_aversion=2.0,
        impact_penalty=1.0,
        max_turnover_weight=0.25,
        max_gross_weight=0.9,
    )
    provider = FixedExecutionCostProvider(cost_bps, horizon_days=policy.horizon_days)
    predictions = [
        provider.estimate(
            {
                "decision_at": CUTOFF,
                "horizon_days": policy.horizon_days,
                "symbol": symbol,
                "side": "buy",
                "market_identity": "synthetic:spot",
                "evaluation_kind": "retrospective",
                "information_policy": {"mode": "retrospective"},
            }
        )
        for symbol in SYMBOLS
    ]
    assert all(prediction["status"] == "supported" for prediction in predictions)
    weights, allocation = allocate_cost_aware(
        visible_returns.mean().to_numpy() * policy.horizon_days,
        estimate.covariance * policy.horizon_days,
        np.zeros(3),
        np.full(3, 0.2),
        policy=policy,
        cost_predictions=predictions,
        decision_at=CUTOFF,
        evaluation_kind="retrospective",
        information_policy={"mode": "retrospective"},
    )
    assert allocation["status"] == "optimal" and (weights > 0).all()
    assert allocation["cost_provider"]["trading_cost_bps"] == cost_bps
    initial = 10000.0
    quantities = {
        symbol: float(weights[index] * initial / 100.0) for index, symbol in enumerate(SYMBOLS)
    }
    observation = {
        "observed_at": CUTOFF,
        "information_cutoff": CUTOFF,
        "protocol_id": "cross-module-protocol",
        "frozen_candidate_id": "cross-module-candidate",
        "source_kind": "synthetic_fixture",
        "retrospective_observation": False,
        "observations": [
            {
                "symbol": symbol,
                "eligible": True,
                "target_weight": float(weights[index]),
                "source_versions_sha256": features.loc[
                    features.symbol == symbol, "source_versions_sha256"
                ].iloc[0],
            }
            for index, symbol in enumerate(SYMBOLS)
        ],
    }
    observation["observation_id"] = accounts._digest(observation)
    decision = {
        "decision_id": "allocated-decision",
        "observation_id": observation["observation_id"],
        "generated_at": CUTOFF,
        "status": "submitted",
    }
    intents = [
        {
            "intent_id": f"allocated-{index}",
            "decision_id": decision["decision_id"],
            "symbol": symbol,
            "side": "buy",
            "qty": quantities[symbol],
            "reference_price": 100.0,
            "submitted_at": CUTOFF,
        }
        for index, symbol in enumerate(SYMBOLS)
    ]
    events = []
    for index, (price, volume) in enumerate(
        ((100.0, 1.0), (100.0, 1.0), (110.0, 100.0), (105.0, 100.0))
    ):
        at = (context.decision_cutoff.normalize() + pd.Timedelta(days=index + 1)).isoformat()
        bars = {
            symbol: {
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": volume,
            }
            for symbol in SYMBOLS
        }
        raw = {"received_at": at, "bars": bars}
        events.append(
            {
                "event_id": f"event-{index}",
                "timestamp": at,
                "received_at": at,
                "bars": bars,
                "raw_receipt": raw,
                "raw_receipt_sha256": accounts._digest(raw),
            }
        )
    receipt = accounts.build_forward_account_receipt(
        protocol_id="cross-module-protocol",
        frozen_candidate_id="cross-module-candidate",
        session_id=f"cross-module-{cost_bps}",
        session_started_at="2024-01-01T00:00:00Z",
        observations=[observation],
        market_events=events,
        decisions=[decision],
        intents=intents,
        initial_capital=initial,
        commission_rate=0.001,
        max_participation_rate=1.0,
        input_source_kind="synthetic_fixture",
    )
    verification = accounts.verify_forward_account_receipt(
        receipt, protocol_id="cross-module-protocol", frozen_candidate_id="cross-module-candidate"
    )
    return {
        "features": features,
        "weights": weights,
        "quantities": quantities,
        "covariance": estimate.covariance,
        "receipt": receipt.to_dict(),
        "verification": verification,
        "cost_predictions": predictions,
        "initial_capital": initial,
        "policy": policy,
        "mu_horizon": visible_returns.mean().to_numpy() * policy.horizon_days,
    }


def _assert_original_account_bridge(result):
    receipt, verified = result["receipt"], result["verification"]
    fills, final = receipt["fills"], receipt["account_path"][-1]
    notional = sum(fill["qty"] * fill["fill_price"] for fill in fills)
    actual_fees = sum(fill["commission"] for fill in fills)
    assert actual_fees == pytest.approx(0.001 * notional)
    assert final["fees"] == pytest.approx(actual_fees)
    assert final["cash"] == pytest.approx(result["initial_capital"] - notional - actual_fees)
    for symbol, requested in result["quantities"].items():
        assert final["positions"][symbol]["qty"] == pytest.approx(requested)
        assert sum(fill["qty"] for fill in fills if fill["symbol"] == symbol) == pytest.approx(
            requested
        )
    assert final["equity"] == pytest.approx(
        final["cash"] + sum(result["quantities"].values()) * 105.0
    )
    assert verified["engineering"] == "pass" and verified["max_account_bridge_error"] < 1e-8
    # Even a complete Broker replay on synthetic inputs cannot upgrade the
    # features/proxy label study into a proven real forward portfolio return.
    assert verified["portfolio_return"] is None and not verified["forward_evidence_eligible"]
    assert not verified["real_orders"] and not verified["formal_admission"]


def test_visible_three_asset_covariance_cost_target_drives_real_partial_fills_and_equity():
    result = _account_chain(cost_bps=20.0)
    first = result["receipt"]["account_path"][0]
    assert any(
        first["positions"][symbol]["qty"] < result["quantities"][symbol] for symbol in SYMBOLS
    )
    assert all(
        order["remaining_qty"] == pytest.approx(0.0) for order in result["receipt"]["orders"]
    )
    assert len(result["receipt"]["fills"]) > len(SYMBOLS)
    _assert_original_account_bridge(result)


def test_future_revisions_keep_past_targets_and_predicted_costs_do_not_debit_account_twice():
    ordinary = _account_chain(cost_bps=20.0)
    future = _account_chain(cost_bps=20.0, future_revision=True)
    expensive = _account_chain(cost_bps=35.0)
    np.testing.assert_allclose(ordinary["covariance"], future["covariance"], rtol=0, atol=0)
    np.testing.assert_allclose(ordinary["weights"], future["weights"], rtol=0, atol=1e-14)
    assert (
        ordinary["features"].source_versions_sha256.tolist()
        == future["features"].source_versions_sha256.tolist()
    )
    assert expensive["weights"].sum() < ordinary["weights"].sum()
    for result in (ordinary, future, expensive):
        _assert_original_account_bridge(result)
    assert ordinary["receipt"]["account_path"] == future["receipt"]["account_path"]
    # Strict data mechanics in a synthetic retrospective engineering replay do
    # not grant the v1 cost artifact real historical publication eligibility.
    # An actual strict consumer may only perform its native mandatory reduction.
    reduced, audit = allocate_cost_aware(
        ordinary["mu_horizon"],
        ordinary["covariance"] * ordinary["policy"].horizon_days,
        np.full(3, 0.1),
        np.full(3, 0.05),
        policy=ordinary["policy"],
        cost_predictions=ordinary["cost_predictions"],
        decision_at=CUTOFF,
        evaluation_kind="strict",
        information_policy={"mode": "strict", "knowledge": "local"},
    )
    np.testing.assert_array_equal(reduced, np.full(3, 0.05))
    assert audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert audit["discretionary_turnover_weight"] == 0.0


def _synthetic_final_account_score(data):
    # Deliberately no closures, model changes or arbitrary source reads.
    return {
        "research": "not_evaluated",
        "input_source_kind": "synthetic_fixture",
        "independent_holdout": False,
        "metric": float(data.holdout["cost20"].sum() - data.holdout["cost35"].sum()),
        "warmup_has_returns": "cost20" in data.warmup.columns or "cost35" in data.warmup.columns,
        "scored_timestamps": [stamp.isoformat() for stamp in data.holdout.index],
    }


def test_mature_versions_frozen_ridge_targets_account_family_statistics_and_controlled_synthetic_access(
    tmp_path,
):
    """One data flow; no hand-made returns, real forward claim or final tuning."""
    from dataclasses import asdict
    from datetime import datetime, timezone
    from pathlib import Path
    from types import SimpleNamespace
    from uuid import uuid4
    from analysis.controlled_holdout import AtomicHoldoutVault
    from analysis.research_evidence import (
        source_identity,
        panel_result_binding,
        registered_candidate_panel_evidence,
    )
    from analysis.research_validation import HoldoutProtocol
    from analysis.search_scope import SearchScope, data_manifest
    from core.planning.allocation import EntryCandidate
    from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file
    from core.foundation.runtime import MarketDataSlice
    from signal_meta.signal_label_versions import OutcomeRevisionBook
    from signal_meta.signal_observation_types import (
        ContextSnapshot,
        ObservationPolicy,
        SignalCandidateEvent,
        canonical,
        close_time,
        iso,
    )
    from signal_meta.signal_outcomes import ObservationCosts
    from core.market.temporal_data import TemporalOHLCV
    from signal_meta.temporal_labels import VersionedOutcomeTracker
    from research.ml_selection.dataset import FEATURE_COLUMNS, chronological_split
    from research.ml_selection.models import fit_model, load_model
    from research.ml_selection.label_materialization import materialize_training_labels
    from research.ml_selection.selector import ResearchSelector

    context, current_rows, _, raw, members = _visible_inputs(return_frames=True)
    readers = {
        symbol: TemporalOHLCV("full-chain:" + symbol, timeframe="1d", policy={"mode": "strict"})
        for symbol in SYMBOLS
    }
    for symbol, reader in readers.items():
        reader.ingest(raw[symbol])
    dates = raw[SYMBOLS[0]].index
    positions = tuple(range(65, 80, 2))
    tracker = VersionedOutcomeTracker(
        ObservationPolicy(enabled=True, horizons=(1,)), ObservationCosts(commission_rate=0.001)
    )
    candidates = []

    def observed_event(cutoff):
        moment = pd.Timestamp(cutoff)
        histories = {symbol: reader.as_of(moment) for symbol, reader in readers.items()}
        stamp = (moment - pd.Timedelta(days=1)).tz_localize(None)
        bars = {symbol: history.iloc[-1] for symbol, history in histories.items()}
        return MarketDataSlice(stamp, bars, histories, source="historical_strict", timeframe="1d")

    for position in positions:
        signal_time = pd.Timestamp(dates[position], tz="UTC")
        tracker.advance(observed_event(signal_time + pd.Timedelta(days=1)))
        for symbol in SYMBOLS:
            price = float(raw[symbol].iloc[position].close)
            event = SignalCandidateEvent(
                "label-" + symbol + "-" + str(position),
                iso(signal_time),
                symbol,
                "FixedTrainingSignal",
                "long",
                1.0,
                price,
                "fixed/v1",
                canonical({"action": "buy", "price": price}),
                ContextSnapshot(
                    iso(signal_time),
                    close_time(signal_time, "1d"),
                    "1d",
                    "TREND_UP",
                    "active",
                    1.0,
                    "normal",
                    1.0,
                    0.0,
                    False,
                    "{}",
                    "full-chain/v1",
                ),
                0.0,
            )
            tracker.add(event)
            candidates.append(event)
        tracker.advance(observed_event(signal_time + pd.Timedelta(days=2)))
    candidate_map = {event.candidate_id: event.to_dict() for event in candidates}
    outcomes = {(row["candidate_id"], row["horizon_bars"]): row for row in tracker.results}
    label_payload = {
        "temporal_label_protocol": tracker.protocol,
        "costs": asdict(tracker.costs),
        "outcome_revisions": tracker.revisions,
        "temporal_scope": {"decision_mode": "strict"},
    }
    revisions = OutcomeRevisionBook(label_payload, candidate_map, (1,), outcomes)
    train_contexts = [
        SelectionTemporalContext(pd.Timestamp(dates[position], tz="UTC") + pd.Timedelta(days=1))
        for position in positions
    ]
    history = build_dataset(readers, temporal_contexts=train_contexts, membership=members)
    join = {
        (event.symbol, pd.Timestamp(event.context.available_at)): event.candidate_id
        for event in candidates
    }
    history["candidate_id"] = [join[(row.symbol, row.as_of)] for row in history.itertuples()]
    history["horizon_bars"] = 1
    materialized = materialize_training_labels(history, cutoff=CUTOFF, revision_book=revisions)
    assert (
        materialized.label_training_eligible.all() and materialized.label_revision_id.notna().all()
    )
    train_end = pd.Timestamp(dates[76], tz="UTC")
    validation_end = pd.Timestamp(dates[82], tz="UTC")
    splits = chronological_split(
        history,
        train_end=train_end,
        validation_end=validation_end,
        test_end=CUTOFF,
        label_book=revisions,
    )
    assert not splits["train"].empty and not splits["validation"].empty
    assert (splits["train"].label_available_at < train_end).all()
    ridge = fit_model(
        splits["train"],
        splits["validation"],
        features=FEATURE_COLUMNS,
        kind="ridge",
        seed=17,
        params={"alpha": 1.0},
    )
    root = Path(__file__).resolve().parents[1]
    artifacts = root / "outputs" / "pr25_full_chain" / uuid4().hex
    artifacts.mkdir(parents=True, exist_ok=False)
    model_path, config_path = artifacts / "ridge.json", artifacts / "config.json"
    ridge.save(model_path)
    frozen = load_model(model_path)
    assert frozen.model_id == ridge.model_id
    configuration = {
        "schema": "synthetic-complete-chain/v1",
        "input_source_kind": "synthetic_fixture",
        "evaluation_kind": "retrospective",
        "ridge_model_id": frozen.model_id,
        "candidate_ids": ["cost20", "cost35"],
        "benchmark_ids": ["cost35"],
        "attempt_budget": 2,
        "commission_rate": 0.001,
        "horizon_days": 7,
        "selection_threshold": 0.0,
        "statistic_scope": "first eight actual account dates only; no final tuning",
    }
    config_path.write_text(canonical_json(configuration), encoding="utf-8")
    paths = [
        "analysis",
        "backtest",
        "composition",
        "config",
        "core",
        "live_trading",
        "research",
        "router",
        "scripts",
        "strategies",
        "tests/test_pr25_cross_module_integration.py",
        "requirements.txt",
        "requirements.lock.txt",
        "requirements-ml.txt",
        "pyproject.toml",
        model_path.relative_to(root).as_posix(),
        config_path.relative_to(root).as_posix(),
    ]

    def digest(value):
        return sha256_bytes(canonical_json(value).encode())

    data = {
        **{"bars:" + symbol: frame for symbol, frame in raw.items()},
        "membership": members,
        "label_versions": label_payload,
        "materialized_training": materialized,
    }
    protocol = {
        "schema": "paper-research-protocol/v1",
        "study_id": "complete-chain-" + artifacts.name,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L01", "L04", "L05", "L08"],
        "source_manifest_sha256": digest(source_identity(root, paths=paths)),
        "config_sha256": sha256_file(config_path),
        "data_manifest_sha256": digest(data_manifest(data)),
        "data_identity_version": "exact/v2",
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": ["cost20", "cost35"],
        "benchmark_ids": ["cost35"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": "net_account_equity_return_difference",
        "economic_or_precision_target": {
            "minimum_effect": 0.0,
            "rationale": "synthetic engineering diagnostic; no claim of economic power",
        },
        "training_selection_rule": "one fixed ridge fitted on versioned mature training labels; no account result selection",
        "evaluation_windows": [
            {
                "id": "development-account",
                "role": "development",
                "start": "2024-01-02",
                "end": "2024-01-10",
            },
            {"id": "synthetic-final", "role": "final", "start": "2024-01-10", "end": "2024-01-14"},
        ],
        "cost_policy_id": "broker-commission-10bps/v1",
        "risk_policy_id": "fixed-cash-envelope/v1",
        "resampling": {"method": "stationary", "block_lengths": [1], "iterations": 100, "seed": 17},
        "statistics": {"pbo_groups": 4, "periods_per_year": 365},
        "attempt_budget": 2,
        "resource_budget": {"wall_time_seconds": 180, "memory_mb": 512, "max_workers": 1},
        "stop_rules": [
            "retain every actual replay; stop on identity mismatch; never tune using synthetic final"
        ],
        "final_access_policy_id": "synthetic-single-use/v1",
    }
    scope = SearchScope.register(
        protocol=protocol,
        data_map=data,
        output=tmp_path / "family",
        source_root=root,
        source_paths=paths,
    )
    current_rows = build_dataset(readers, temporal_contexts=[context], membership=members)
    views = {symbol: context.visible(reader, symbol=symbol) for symbol, reader in readers.items()}
    prices = {symbol: float(view.close.iloc[-1]) for symbol, view in views.items()}
    event = MarketDataSlice(
        pd.Timestamp(dates[-1]),
        {symbol: view.iloc[-1] for symbol, view in views.items()},
        views,
        source="historical_strict",
        timeframe="1d",
    )
    entries = [
        EntryCandidate(
            symbol,
            SimpleNamespace(name="FrozenRidge"),
            len(views[symbol]) - 1,
            views[symbol],
            "trend",
            {"action": "buy", "requested_qty": 1.0},
            1.0,
            {},
        )
        for symbol in SYMBOLS
    ]
    selector = ResearchSelector(current_rows, model=frozen, min_expected_return=0.0)
    selected = selector.select(
        entries,
        event=event,
        portfolio=SimpleNamespace(
            cash=10000.0,
            initial_capital=10000.0,
            positions={},
            get_total_value=lambda marks: 10000.0,
        ),
        broker=SimpleNamespace(pending_orders=[], active_orders=[]),
        risk_manager=SimpleNamespace(high_water_equity=10000.0, last_drawdown=0.0),
        current_prices=prices,
    )
    assert len(selected) == 3
    predictions = {
        row["symbol"]: row["expected_net_return"] for row in selector.audit if row["selected"]
    }
    visible_returns = (
        pd.DataFrame({symbol: view.close for symbol, view in views.items()})
        .pct_change(fill_method=None)
        .iloc[1:]
        .tail(60)
    )
    covariance = estimate_covariance(visible_returns, estimator_id="fixed_diag_020").covariance
    policy = CostAwareAllocationPolicy(
        enabled=True,
        horizon_days=7,
        risk_aversion=2.0,
        impact_penalty=0.1,
        max_turnover_weight=0.25,
        max_gross_weight=0.9,
    )
    arms, bases, full_returns = {}, {}, {}
    for name, assumption in (("cost20", 20.0), ("cost35", 35.0)):
        scope.record(candidate=name, status="started")
        provider = FixedExecutionCostProvider(assumption, horizon_days=7)
        cost_predictions = [
            provider.estimate(
                {
                    "decision_at": CUTOFF,
                    "horizon_days": 7,
                    "symbol": symbol,
                    "side": "buy",
                    "market_identity": "synthetic:spot",
                    "evaluation_kind": "retrospective",
                }
            )
            for symbol in SYMBOLS
        ]
        weights, audit = allocate_cost_aware(
            np.array([predictions[symbol] for symbol in SYMBOLS]) * 7,
            covariance * 7,
            np.zeros(3),
            np.full(3, 0.2),
            policy=policy,
            cost_predictions=cost_predictions,
            decision_at=CUTOFF,
            evaluation_kind="retrospective",
            information_policy={"mode": "retrospective"},
        )
        assert audit["status"] == "optimal" and weights.sum() > 0
        observation = {
            "observed_at": CUTOFF,
            "information_cutoff": CUTOFF,
            "protocol_id": protocol["study_id"],
            "frozen_candidate_id": frozen.model_id,
            "source_kind": "synthetic_fixture",
            "retrospective_observation": False,
            "observations": [
                {
                    "symbol": symbol,
                    "eligible": True,
                    "model_id": frozen.model_id,
                    "prediction": predictions[symbol],
                }
                for symbol in SYMBOLS
            ],
        }
        observation["observation_id"] = accounts._digest(observation)
        decisions = [
            {
                "decision_id": name,
                "observation_id": observation["observation_id"],
                "generated_at": CUTOFF,
                "status": "submitted",
            }
        ]
        intents = [
            {
                "intent_id": name + str(index),
                "decision_id": name,
                "symbol": symbol,
                "side": "buy",
                "qty": float(weights[index] * 10000.0 / prices[symbol]),
                "reference_price": prices[symbol],
                "submitted_at": CUTOFF,
            }
            for index, symbol in enumerate(SYMBOLS)
            if weights[index] > 0
        ]
        events = []
        for index, price in enumerate(
            (100.0, 100.0, 110.0, 105.0, 103.0, 107.0, 101.0, 109.0, 105.0, 104.0, 108.0, 106.0)
        ):
            stamp = (context.decision_cutoff.normalize() + pd.Timedelta(days=index + 1)).isoformat()
            bars = {
                symbol: {
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price,
                    "volume": 1.0 if index < 2 else 100.0,
                }
                for symbol in SYMBOLS
            }
            raw_event = {"received_at": stamp, "bars": bars}
            events.append(
                {
                    "event_id": str(index),
                    "timestamp": stamp,
                    "received_at": stamp,
                    "bars": bars,
                    "raw_receipt": raw_event,
                    "raw_receipt_sha256": accounts._digest(raw_event),
                }
            )
        receipt = accounts.build_forward_account_receipt(
            protocol_id=protocol["study_id"],
            frozen_candidate_id=frozen.model_id,
            session_id=name,
            session_started_at="2024-01-01T00:00:00Z",
            observations=[observation],
            market_events=events,
            decisions=decisions,
            intents=intents,
            initial_capital=10000.0,
            commission_rate=0.001,
            input_source_kind="synthetic_fixture",
        )
        verified = accounts.verify_forward_account_receipt(
            receipt, protocol_id=protocol["study_id"], frozen_candidate_id=frozen.model_id
        )
        value = receipt.to_dict()
        assert verified["engineering"] == "pass" and verified["max_account_bridge_error"] < 1e-8
        assert verified["portfolio_return"] is None and not verified["forward_evidence_eligible"]
        actual_fees = sum(fill["commission"] for fill in value["fills"])
        assert actual_fees == pytest.approx(
            0.001 * sum(fill["qty"] * fill["fill_price"] for fill in value["fills"])
        )
        equity = pd.Series(
            [row["equity"] for row in value["account_path"]],
            index=pd.to_datetime([row["timestamp"] for row in value["account_path"]], utc=True),
        )
        returns = equity.pct_change(fill_method=None)
        returns.iloc[0] = equity.iloc[0] / 10000.0 - 1
        full_returns[name] = returns
        arms[name] = returns.loc[returns.index < pd.Timestamp("2024-01-10", tz="UTC")]
        bases[name] = {
            "return_basis": "net_account_equity_returns",
            "capital_policy_id": "fixed-initial-10000/v1",
            "cost_policy_id": protocol["cost_policy_id"],
            "risk_policy_id": protocol["risk_policy_id"],
            "timeframe": "1d",
            "currency": "USDT",
            "account_trace_sha256": digest(value["account_path"]),
        }
        scope.record(
            candidate=name, status="completed", **panel_result_binding(arms[name], bases[name])
        )
    stats = registered_candidate_panel_evidence(scope, arms, accounting_by_arm=bases)
    assert (
        stats["status"] == "diagnostic"
        and stats["local_family_complete"]
        and stats["results_complete"]
    )
    assert stats["data_identity_version"] == "exact/v2" and stats["unique_time_observations"] == 8
    assert not stats["historical_global_search_complete"] and not stats["admission_eligible"]
    assert scope.audit()["attempts_used"] == 2
    final_path = artifacts / "actual_account_returns.csv"
    pd.DataFrame(full_returns).to_csv(final_path)
    holdout_protocol = HoldoutProtocol(
        "synthetic-final-" + artifacts.name,
        "2024-01-02",
        "2024-01-05",
        "2024-01-06",
        "2024-01-09",
        "2024-01-10",
        "2024-01-13",
    )
    vault = AtomicHoldoutVault(tmp_path / "synthetic-final.sqlite3")
    vault.register(
        holdout_protocol,
        data_path=final_path,
        source_root=root,
        source_paths=paths,
        evaluator_id=__name__ + ":_synthetic_final_account_score",
        label_columns=["cost20", "cost35"],
        warmup_rows=2,
    )
    vault.freeze()
    final = vault.evaluate(
        evaluation_id="one-synthetic-evaluation", evaluator=_synthetic_final_account_score
    )
    assert final["status"] == "completed" and len(final["result"]["scored_timestamps"]) == 4
    assert not final["result"]["warmup_has_returns"] and not final["result"]["independent_holdout"]
    assert not final["live_authorization"] and final["result"]["research"] == "not_evaluated"
    with pytest.raises(RuntimeError, match="consumed"):
        vault.evaluate(evaluation_id="another", evaluator=_synthetic_final_account_score)
