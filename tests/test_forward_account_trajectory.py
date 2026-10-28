"""Original Broker replay, independent funds bridge and forward provenance guards."""

from copy import deepcopy

import pytest

from core.foundation.reproducibility import canonical_json
from research.ml_selection import forward_account_trajectory as account
from research.ml_selection.forward_evidence import evaluate_forward_store
from tests.test_ml_selection_next_forward import forward_store


def signed(row, key):
    row = deepcopy(row)
    row.pop(key, None)
    row[key] = account._digest(row)
    return row


def fixture_inputs(
    *, observations=None, source="synthetic_fixture", trades=True, initial_capital=10000.0
):
    observations = observations or [
        signed(
            {
                "observed_at": "2024-01-01T12:00:00Z",
                "information_cutoff": "2024-01-01T12:00:00Z",
                "protocol_id": "protocol",
                "frozen_candidate_id": "candidate",
                "source_kind": "synthetic_fixture",
                "retrospective_observation": False,
                "observations": [{"symbol": "A/USDT", "eligible": True}],
            },
            "observation_id",
        )
    ]
    first = observations[0]
    start = account._time(first["observed_at"], "fixture time").normalize()
    events = []
    for n, (price, volume) in enumerate(
        [(100.0, 1.0), (100.0, 1.0), (110.0, 100.0), (105.0, 100.0)]
    ):
        stamp = (start + __import__("pandas").Timedelta(days=n + 1)).isoformat()
        bars = {
            "A/USDT": {
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": volume,
            }
        }
        raw = {"received_at": stamp, "bars": bars}
        events.append(
            {
                "event_id": f"E{n}",
                "timestamp": stamp,
                "received_at": stamp,
                "bars": bars,
                "raw_receipt": raw,
                "raw_receipt_sha256": account._digest(raw),
            }
        )
    decisions = [
        {
            "decision_id": "D1",
            "observation_id": first["observation_id"],
            "generated_at": first["observed_at"],
            "status": "submitted" if trades else "abstained",
        }
    ]
    # A second order belongs to the same recorded decision for this engineering
    # fixture, with a later explicit intent clock; it never claims live trading.
    intents = (
        [
            {
                "intent_id": "I1",
                "decision_id": "D1",
                "symbol": "A/USDT",
                "side": "buy",
                "qty": 2.0,
                "reference_price": 100.0,
                "submitted_at": first["observed_at"],
            },
            {
                "intent_id": "I2",
                "decision_id": "D1",
                "symbol": "A/USDT",
                "side": "sell",
                "qty": 1.0,
                "reference_price": 110.0,
                "submitted_at": (
                    start + __import__("pandas").Timedelta(days=2, hours=12)
                ).isoformat(),
            },
        ]
        if trades
        else []
    )
    return dict(
        protocol_id=first["protocol_id"],
        frozen_candidate_id=first["frozen_candidate_id"],
        session_id="session",
        session_started_at=start.isoformat(),
        observations=observations,
        market_events=events,
        decisions=decisions,
        intents=intents,
        initial_capital=initial_capital,
        input_source_kind=source,
    )


def verify(receipt, **kwargs):
    value = receipt.to_dict() if isinstance(receipt, account.ForwardAccountReceipt) else receipt
    return account.verify_forward_account_receipt(
        receipt,
        protocol_id=value["protocol_id"],
        frozen_candidate_id=value["frozen_candidate_id"],
        **kwargs,
    )


def test_original_broker_partial_fills_fee_lot_and_cash_equity_bridge():
    receipt = account.build_forward_account_receipt(**fixture_inputs())
    report = verify(receipt)
    value = receipt.to_dict()
    assert [f["qty"] for f in value["fills"]] == [1.0, 1.0, 1.0]
    assert value["account_path"][-1]["cash"] == pytest.approx(9909.69)
    assert value["account_path"][-1]["equity"] == pytest.approx(10014.69)
    assert value["account_path"][-1]["fees"] == pytest.approx(0.31)
    assert len(value["lot_closes"]) == 1
    assert value["lot_closes"][0]["realized_pnl"] == pytest.approx(9.79)
    assert report["engineering"] == "pass"
    assert report["max_account_bridge_error"] < 1e-8
    assert report["simulated_replay_return"] == pytest.approx(0.001469)
    assert report["portfolio_return"] is None
    assert not report["forward_evidence_eligible"]
    assert not report["real_orders"] and not report["formal_admission"]


def test_zero_trade_account_needs_explicit_abstention_and_remains_valid():
    inputs = fixture_inputs(trades=False)
    receipt = account.build_forward_account_receipt(**inputs)
    assert verify(receipt)["simulated_replay_return"] == 0.0
    assert receipt.payload["orders"] == []
    inputs["decisions"] = []
    with pytest.raises(ValueError, match="explicit submitted or abstained"):
        account.build_forward_account_receipt(**inputs)


def test_insufficient_cash_is_not_assumed_filled_and_rejected_order_retained():
    inputs = fixture_inputs(initial_capital=50.0)
    receipt = account.build_forward_account_receipt(**inputs)
    assert verify(receipt)["engineering"] == "pass"
    orders = receipt.payload["orders"]
    assert len(orders) == 2
    # The original Broker may reduce to affordable fractional quantities; the
    # adapter preserves the actual engine facts rather than injecting 2 units.
    assert orders[0]["filled_qty"] < 2.0
    assert receipt.payload["account_path"][-1]["cash"] >= -1e-8


@pytest.mark.parametrize(
    "mutate,pattern",
    [
        (lambda r: r.update(session_id=""), "session_id"),
        (
            lambda r: r["account_path"][-1].update(equity=999999.0),
            "original-engine deterministic replay",
        ),
        (
            lambda r: r["fills"].append(deepcopy(r["fills"][0])),
            "original-engine deterministic replay",
        ),
        (lambda r: r["market_events"][0]["bars"]["A/USDT"].update(close=100.5), "raw receipt"),
        (lambda r: r.update(source_kind="decision_only_bridge"), "not contemporaneous"),
        (lambda r: r.update(real_orders=True), "real orders or admission"),
    ],
)
def test_resigned_invalid_facts_and_missing_linkage_are_still_rejected(mutate, pattern):
    receipt = account.build_forward_account_receipt(**fixture_inputs()).to_dict()
    mutate(receipt)
    receipt = signed(receipt, "receipt_id")
    with pytest.raises(ValueError, match=pattern):
        verify(receipt)


def test_bad_hash_clock_unknown_decision_and_source_mismatch_are_distinct():
    receipt = account.build_forward_account_receipt(**fixture_inputs()).to_dict()
    modified = deepcopy(receipt)
    modified["session_id"] = "another"
    with pytest.raises(ValueError, match="identity"):
        verify(modified)
    with pytest.raises(ValueError, match="not available"):
        verify(receipt, as_of="2024-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="frozen expected"):
        verify(receipt, expected_source_manifest_sha256="a" * 64)
    unknown = fixture_inputs(trades=False, source="recorded_forward")
    unknown["decisions"][0]["status"] = "unknown"
    receipt = account.build_forward_account_receipt(**unknown)
    obs = {row["observation_id"]: row for row in unknown["observations"]}
    report = verify(receipt, qualifying_observation_ids=obs, observation_receipts=obs)
    assert "unknown_order_decision_status" in report["pending_reasons"]
    assert report["portfolio_return"] is None


@pytest.mark.parametrize("prefix_bars", [1, 2])
def test_resume_reconstructs_exact_prefix_and_never_duplicates_partial_fills(prefix_bars):
    inputs = fixture_inputs()
    first_inputs = deepcopy(inputs)
    first_inputs["market_events"] = first_inputs["market_events"][:prefix_bars]
    first_inputs["intents"] = first_inputs["intents"][:1]
    first = account.build_forward_account_receipt(**first_inputs)
    restored = account.resume_forward_account_receipt(
        first,
        market_events=inputs["market_events"],
        intents=inputs["intents"],
        decisions=inputs["decisions"],
    )
    assert restored.payload["resumed_from"] == first.receipt_id
    assert len(restored.payload["fills"]) == 3
    assert restored.payload["fills"][:prefix_bars] == first.payload["fills"]
    assert restored.payload["account_path"][:prefix_bars] == first.payload["account_path"]
    assert (
        account.resume_forward_account_receipt(
            restored, market_events=inputs["market_events"]
        ).receipt_id
        == restored.receipt_id
    )
    assert verify(restored)["engineering"] == "pass"
    bad = deepcopy(inputs["market_events"])
    bad[0]["timestamp"] = "2024-01-02T01:00:00Z"
    with pytest.raises(ValueError, match="prefix"):
        account.resume_forward_account_receipt(first, market_events=bad)


def test_recorded_observations_without_original_order_binding_stay_account_pending(tmp_path):
    protocol, observation = forward_store(tmp_path)
    inputs = fixture_inputs(observations=[observation], source="recorded_forward")
    receipt = account.build_forward_account_receipt(**inputs)
    report = verify(
        receipt,
        qualifying_observation_ids=[observation["observation_id"]],
        observation_receipts={observation["observation_id"]: observation},
    )
    assert report["status"] == "engineering_verified_forward_pending"
    assert report["portfolio_return"] is None
    assert report["simulated_replay_return"] == pytest.approx(0.001469)
    assert report["source_kind"] == "recorded_forward_input_simulated_replay"
    result = evaluate_forward_store(tmp_path, protocol, account_receipts=[receipt])
    assert result["simulated_account_evidence"]["status"] == "pending"
    assert result["simulated_account_evidence"]["portfolio_return"] is None
    assert "original_engine_simulated_fill_and_equity_evidence_pending" in result["pending_reasons"]
    assert result["status"] == "pending_forward_evidence"  # date/label coverage still pending
    assert result["proxy_evidence"]["pending_labels"] == 1
    assert not result["formal_admission"]


def test_synthetic_proxy_or_corrupt_account_never_clears_forward_gap(tmp_path):
    protocol, observation = forward_store(tmp_path)
    inputs = fixture_inputs(observations=[observation])
    receipt = account.build_forward_account_receipt(**inputs)
    result = evaluate_forward_store(tmp_path, protocol, account_receipts=[receipt])
    assert result["simulated_account_evidence"]["portfolio_return"] is None
    assert "original_engine_simulated_fill_and_equity_evidence_pending" in result["pending_reasons"]
    bad = receipt.to_dict()
    bad["fills"][0]["qty"] += 1.0
    result = evaluate_forward_store(tmp_path, protocol, account_receipts=[bad])
    assert result["simulated_account_evidence"]["status"] == "invalid"
    assert "invalid_original_engine_account_receipts" in result["pending_reasons"]


def test_receipt_loader_round_trip_rejects_nonfinite_and_duplicate_json_keys(tmp_path):
    receipt = account.build_forward_account_receipt(**fixture_inputs())
    path = tmp_path / "account.json"
    path.write_text(canonical_json(receipt.to_dict()), encoding="utf-8")
    assert account.load_forward_account_receipt(path).receipt_id == receipt.receipt_id
    path.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        account.load_forward_account_receipt(path)
    bad = receipt.to_dict()
    bad["account_path"][0]["cash"] = float("nan")
    with pytest.raises(ValueError, match="finite JSON"):
        account.load_forward_account_receipt(bad)


def test_zero_quantity_rejected_order_is_preserved_without_fictitious_fill():
    inputs = fixture_inputs()
    inputs["intents"] = inputs["intents"][:1]
    inputs["intents"][0]["qty"] = 0.0
    receipt = account.build_forward_account_receipt(**inputs)
    assert receipt.payload["orders"][0]["status"] == "rejected"
    assert receipt.payload["fills"] == []
    assert verify(receipt)["simulated_replay_return"] == 0.0


def test_decision_clock_cannot_precede_original_observation_and_root_cannot_be_invented(tmp_path):
    inputs = fixture_inputs()
    inputs["decisions"][0]["generated_at"] = "2024-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="predates"):
        account.build_forward_account_receipt(**inputs)
    with pytest.raises(ValueError, match="actual source root"):
        account.build_forward_account_receipt(**fixture_inputs(), source_root=tmp_path)


def test_recovery_fork_and_independent_sessions_cannot_be_pooled_as_one_return():
    inputs = fixture_inputs()
    prefix = deepcopy(inputs)
    prefix["market_events"] = prefix["market_events"][:1]
    prefix["intents"] = prefix["intents"][:1]
    old = account.build_forward_account_receipt(**prefix)
    new_a = account.resume_forward_account_receipt(
        old, market_events=inputs["market_events"], intents=inputs["intents"]
    )
    new_b = account.resume_forward_account_receipt(
        old, market_events=inputs["market_events"], intents=inputs["intents"]
    )
    result = account.summarize_forward_account_receipts(
        [old, new_a, new_b],
        protocol_id="protocol",
        frozen_candidate_id="candidate",
        qualifying_observation_ids=[],
        observation_receipts={},
    )
    assert result["status"] == "invalid"
    assert any("fork" in error for error in result["errors"])
    separate = deepcopy(inputs)
    separate["session_id"] = "separate-book"
    other = account.build_forward_account_receipt(**separate)
    result = account.summarize_forward_account_receipts(
        [new_a, other],
        protocol_id="protocol",
        frozen_candidate_id="candidate",
        qualifying_observation_ids=[],
        observation_receipts={},
    )
    assert result["portfolio_return"] is None
    assert "single_continuous_account_trajectory_pending" in result["pending_reasons"]


def test_resume_cannot_backfill_an_old_abstention_even_when_account_cash_would_match():
    inputs = fixture_inputs(trades=False)
    prefix = deepcopy(inputs)
    prefix["market_events"] = prefix["market_events"][:1]
    old = account.build_forward_account_receipt(**prefix)
    observation = signed(
        {**inputs["observations"][0], "extra_receipt_sequence": 2}, "observation_id"
    )
    decisions = inputs["decisions"] + [
        {
            "decision_id": "late",
            "observation_id": observation["observation_id"],
            "generated_at": observation["observed_at"],
            "status": "abstained",
        }
    ]
    with pytest.raises(ValueError, match="backfills"):
        account.resume_forward_account_receipt(
            old,
            market_events=inputs["market_events"],
            observations=inputs["observations"] + [observation],
            decisions=decisions,
        )
