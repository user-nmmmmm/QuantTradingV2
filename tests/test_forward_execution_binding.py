"""Original-store commands and independent collection bytes cannot be self-signed away."""

from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from core.broker import Broker
from core.trading.portfolio import Portfolio
from research.ml_selection import forward_account_trajectory as account
from research.ml_selection.forward_execution_binding import capture_forward_execution_binding
from tests.test_forward_account_trajectory import signed
from tests.test_ml_selection_next_forward import public_receipt


def bound_fixture(tmp_path):
    collection = public_receipt(tmp_path / "public_inputs")
    manifest = collection["manifest"]
    folder = Path(collection["folder"])
    frame = pd.read_csv(folder / "BTC_USDT.csv", index_col="timestamp", parse_dates=True).tail(4)
    first = pd.Timestamp(frame.index[0])
    observed = (first - pd.Timedelta(hours=12)).isoformat()
    observation = signed(
        {
            "observed_at": observed,
            "information_cutoff": observed,
            "protocol_id": "protocol",
            "frozen_candidate_id": "candidate",
            "model_id": "model",
            "source_kind": "synthetic_fixture",
            "retrospective_observation": False,
            "observations": [{"symbol": "BTC/USDT", "eligible": True}],
        },
        "observation_id",
    )
    decisions = [
        {
            "decision_id": "D1",
            "observation_id": observation["observation_id"],
            "generated_at": observed,
            "status": "submitted",
        }
    ]
    events = []
    received = manifest["market_files"]["BTC/USDT"]["received_at"]
    for index, bar in frame.iterrows():
        bars = {
            "BTC/USDT": {
                name: float(bar[name]) for name in ("open", "high", "low", "close", "volume")
            }
        }
        raw = {"received_at": received, "bars": bars}
        events.append(
            {
                "event_id": str(index),
                "timestamp": pd.Timestamp(index).isoformat(),
                "received_at": received,
                "bars": bars,
                "raw_receipt": raw,
                "raw_receipt_sha256": account._digest(raw),
            }
        )
    broker = Broker(
        Portfolio(10000),
        commission_rate=0.001,
        commission_rate_maker=0.001,
        slippage=0,
        account_id="session",
        exchange_id="recorded_forward_simulation",
    )
    order = broker.submit_order(
        "BTC/USDT",
        "buy",
        2,
        price=100.0,
        timestamp=pd.Timestamp(observed),
        strategy_id="frozen_forward_candidate",
        sequence=0,
    )
    binding = capture_forward_execution_binding(
        broker=broker,
        decisions=decisions,
        decision_order_ids={"D1": [order.id]},
        observations=[observation],
        market_events=events,
        market_collections=[
            {
                "path": folder.relative_to(tmp_path).as_posix(),
                "collection_id": manifest["collection_id"],
            }
        ],
        protocol_id="protocol",
        frozen_candidate_id="candidate",
        model_id="model",
        session_id="session",
    )
    inputs = {
        "protocol_id": "protocol",
        "frozen_candidate_id": "candidate",
        "session_id": "session",
        "session_started_at": (first - pd.Timedelta(days=1)).isoformat(),
        "observations": [observation],
        "market_events": events,
        "decisions": decisions,
        "intents": deepcopy(binding["intents"]),
        "input_source_kind": "synthetic_fixture",
        "execution_binding_id": binding["binding_id"],
    }
    receipt = account.build_forward_account_receipt(**inputs)
    return inputs, receipt, binding, {observation["observation_id"]: observation}, folder


def verify(receipt, binding, observations, root):
    return account.verify_forward_account_receipt(
        receipt,
        protocol_id="protocol",
        frozen_candidate_id="candidate",
        qualifying_observation_ids=observations,
        observation_receipts=observations,
        execution_bindings={binding["binding_id"]: binding},
        binding_store_root=root,
    )


def test_full_binding_uses_actual_broker_commands_and_independent_collection_but_stays_synthetic(
    tmp_path,
):
    inputs, receipt, binding, observations, _ = bound_fixture(tmp_path)
    result = verify(receipt, binding, observations, tmp_path)
    assert result["engineering"] == "pass"
    assert result["original_execution_binding"]["status"] == "verified"
    assert result["simulated_replay_return"] == pytest.approx(-0.00002)
    assert result["portfolio_return"] is None
    assert not result["forward_evidence_eligible"]
    assert "synthetic_input_is_not_actual_forward_evidence" in result["pending_reasons"]


@pytest.mark.parametrize("field,value", [("qty", 1.5), ("side", "sell")])
def test_same_observation_ids_cannot_attach_new_quantity_or_side(field, value, tmp_path):
    inputs, _, binding, observations, _ = bound_fixture(tmp_path)
    inputs["intents"][0][field] = value
    changed = account.build_forward_account_receipt(**inputs)
    with pytest.raises(ValueError, match="intents differ"):
        verify(changed, binding, observations, tmp_path)


def test_same_observation_ids_cannot_attach_self_signed_future_bars(tmp_path):
    inputs, _, binding, observations, _ = bound_fixture(tmp_path)
    event = inputs["market_events"][0]
    event["bars"]["BTC/USDT"].update(open=101.0, close=101.0, high=102.0, low=100.0)
    event["raw_receipt"]["bars"] = deepcopy(event["bars"])
    event["raw_receipt_sha256"] = account._digest(event["raw_receipt"])
    changed = account.build_forward_account_receipt(**inputs)
    with pytest.raises(ValueError, match="market_events differ"):
        verify(changed, binding, observations, tmp_path)
    # Even forging/resigning a second local binding cannot bypass the original
    # independently captured CSV/collection hashes and prices.
    forged = deepcopy(binding)
    forged["market_events"] = inputs["market_events"]
    forged = signed(forged, "binding_id")
    inputs["execution_binding_id"] = forged["binding_id"]
    changed = account.build_forward_account_receipt(**inputs)
    with pytest.raises(ValueError, match="original public collection bytes"):
        verify(changed, forged, observations, tmp_path)


def test_original_observation_and_frozen_model_bytes_are_checked_not_just_ids(tmp_path):
    inputs, receipt, binding, observations, _ = bound_fixture(tmp_path)
    wrong = deepcopy(observations)
    next(iter(wrong.values()))["observations"][0]["eligible"] = False
    with pytest.raises(ValueError, match="actual store"):
        verify(receipt, binding, wrong, tmp_path)
    wrong_model = deepcopy(binding)
    wrong_model["model_id"] = "another-model"
    wrong_model = signed(wrong_model, "binding_id")
    inputs["execution_binding_id"] = wrong_model["binding_id"]
    changed = account.build_forward_account_receipt(**inputs)
    with pytest.raises(ValueError, match="model identity"):
        verify(changed, wrong_model, observations, tmp_path)


def test_independent_public_market_bytes_cannot_be_replaced_after_binding(tmp_path):
    _, receipt, binding, observations, folder = bound_fixture(tmp_path)
    source = folder / "BTC_USDT.csv"
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="market bytes changed"):
        verify(receipt, binding, observations, tmp_path)


def test_capture_refuses_non_native_order_and_missing_emitted_command(tmp_path):
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="actual session Broker"):
        capture_forward_execution_binding(
            broker=SimpleNamespace(account_id="session"),
            decisions=[],
            decision_order_ids={},
            observations=[],
            market_events=[],
            market_collections=[],
            protocol_id="p",
            frozen_candidate_id="c",
            model_id="m",
            session_id="session",
        )


def test_binding_map_key_is_not_a_substitute_for_actual_binding_identity(tmp_path):
    inputs, _, binding, observations, _ = bound_fixture(tmp_path)
    inputs["execution_binding_id"] = "a" * 64
    changed = account.build_forward_account_receipt(**inputs)
    with pytest.raises(ValueError, match="mapping keys"):
        account.verify_forward_account_receipt(
            changed,
            protocol_id="protocol",
            frozen_candidate_id="candidate",
            qualifying_observation_ids=observations,
            observation_receipts=observations,
            execution_bindings={"a" * 64: binding},
            binding_store_root=tmp_path,
        )


def test_resigning_same_canonical_command_cannot_replace_original_same_session_quantity(tmp_path):
    from research.ml_selection.forward_execution_binding import index_forward_execution_bindings

    _, _, binding, _, _ = bound_fixture(tmp_path)
    forged = deepcopy(binding)
    identifier = forged["intents"][0]["intent_id"]
    forged["intents"][0]["qty"] = 1.5
    forged["native_commands"][identifier]["requested_qty"] = 1.5
    forged = signed(forged, "binding_id")
    with pytest.raises(ValueError, match="conflicting original same-session"):
        index_forward_execution_bindings([binding, forged])


def test_empty_native_order_set_does_not_vacuously_prove_a_frozen_policy_abstained(tmp_path):
    inputs, _, prior, observations, _ = bound_fixture(tmp_path)
    inputs["decisions"][0]["status"] = "abstained"
    inputs["intents"] = []
    empty = Broker(Portfolio(10000), account_id="session")
    binding = capture_forward_execution_binding(
        broker=empty,
        decisions=inputs["decisions"],
        decision_order_ids={"D1": []},
        observations=inputs["observations"],
        market_events=inputs["market_events"],
        market_collections=prior["market_collections"],
        protocol_id="protocol",
        frozen_candidate_id="candidate",
        model_id="model",
        session_id="session",
    )
    inputs["execution_binding_id"] = binding["binding_id"]
    receipt = account.build_forward_account_receipt(**inputs)
    result = verify(receipt, binding, observations, tmp_path)
    assert result["original_execution_binding"]["status"] == "verified"
    assert not result["original_execution_binding"]["original_policy_command_origin_linked"]
    assert (
        "explicit_local_command_replay_original_frozen_policy_origin_pending"
        in result["pending_reasons"]
    )
    assert result["portfolio_return"] is None
