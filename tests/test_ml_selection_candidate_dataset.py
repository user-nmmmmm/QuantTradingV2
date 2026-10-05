"""Original-candidate coverage, cost/maturity contracts and paired identities."""
from copy import deepcopy
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from core.reproducibility import canonical_json, sha256_file
from research.ml_selection.candidate_dataset import (
    PAIR_IDENTITY_FIELDS, build_candidate_dataset, build_from_episode, compare_paired_accounts,
    load_candidate_training_rows,
)
from research.ml_selection.dataset import FEATURE_COLUMNS


def fixture():
    identity = {"account_id": "native", "data_identity": "data1", "protocol_id": "p1",
                "account_mode": "spot", "initial_capital": 100., "accounting_ok": True, "pending_orders": []}
    decisions = [{"decision_id": "A1", "order_id": "O1", "symbol": "A", "strategy": "Trend",
                  "as_of": "2024-01-02", "original_strategy_signal": True, "native_score": 3.,
                  "original_signal": {"action": "buy", "price": 10., "stop_loss": 9.},
                  "health_multiplier": .5, "gate_facts": [{"reason": "entry", "health_multiplier": .5}]},
                 {"decision_id": "B1", "symbol": "B", "strategy": "Trend", "as_of": "2024-01-02",
                  "original_strategy_signal": True, "native_score": 2., "selected": False, "action": "buy"},
                 {"decision_id": "C1", "order_id": "O3", "symbol": "C", "strategy": "Trend",
                  "as_of": "2024-01-02", "original_strategy_signal": True},
                 {"decision_id": "noise", "symbol": "D", "as_of": "2024-01-02", "reason": "no_signal"}]
    fills = [{"order_id": "O1", "side": "buy", "symbol": "A", "fill_time": "2024-01-02",
              "qty": 2., "fill_price": 10., "commission": .2},
             {"order_id": "X1", "side": "sell", "symbol": "A", "fill_time": "2024-01-03",
              "qty": 1., "fill_price": 12., "commission": .12, "exit_reason": "stop",
              "lot_closes": [{"entry_order_id": "O1", "qty_closed": 1., "entry_price": 10.,
                              "entry_cost_share": .1, "close_event_id": "LC1"}]},
             {"order_id": "X2", "side": "sell", "symbol": "A", "fill_time": "2024-01-04",
              "qty": 1., "fill_price": 11., "commission": .11, "exit_reason": "trailing",
              "lot_closes": [{"entry_order_id": "O1", "qty_closed": 1., "entry_price": 10.,
                              "entry_cost_share": .1, "close_event_id": "LC2"}]},
             {"order_id": "O3", "side": "buy", "symbol": "C", "fill_time": "2024-01-02",
              "qty": 1., "fill_price": 20., "commission": .2}]
    snapshots = pd.DataFrame([{"symbol": symbol, "as_of": "2024-01-02", "eligible": True,
        **dict.fromkeys(FEATURE_COLUMNS, .2), "label_net_return": -.1,
        "label_available_at": "2024-01-25", "label_exit_reason": "proxy_horizon"} for symbol in ("A", "B", "C")])
    return identity, decisions, fills, snapshots


def build(*, cutoff="2024-02-01", **kwargs):
    identity, decisions, fills, snapshots = fixture()
    return build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of=cutoff, **kwargs)


def test_actual_costs_all_candidates_and_original_daily_group_preserved():
    frame, contract = build()
    assert list(frame.symbol) == ["A", "B", "C"]
    assert frame.same_day_candidate_count.tolist() == [3, 3, 3]
    assert frame.same_day_labeled_count.tolist() == [1, 1, 1]
    assert not frame.daily_group_complete.any()
    known = frame.iloc[0]
    assert known.label_net_pnl == pytest.approx(2.57)
    assert known.label_net_return == pytest.approx(2.57 / 20.)
    assert known.label_available_at == pd.Timestamp("2024-01-05", tz="UTC")
    assert known.label_status == "mature_actual_exit"
    assert known.stop_distance_fraction == pytest.approx(.1)
    assert known.audit_health_multiplier == .5
    assert frame.health_multiplier.isna().all()  # The gate was visited after selection.
    assert np.isnan(frame.iloc[1].label_net_return)
    assert frame.iloc[2].label_status == "unknown_unclosed_or_partial_exit"
    assert contract["market_feature_columns"] == list(FEATURE_COLUMNS)
    assert contract["training_performed"] is False
    assert frame.portfolio_marginal_net_return.isna().all()


def test_zero_candidates_preserve_export_schema():
    identity, _, fills, snapshots = fixture()
    frame, contract = build_candidate_dataset([], fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.empty
    assert {"candidate_id", "candidate_group_id", "label_status", *FEATURE_COLUMNS} <= set(frame)
    assert contract["rows"] == 0
    assert contract["groups"] == []


def test_actual_exit_maturity_and_delayed_information():
    frame, _ = build(cutoff="2024-01-04")
    assert frame.iloc[0].label_status == "unknown_not_yet_mature"
    assert not frame.training_eligible.any()
    assert frame.label_net_return.isna().all()
    identity, decisions, fills, snapshots = fixture()
    fills[2]["available_at"] = "2024-02-10"
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == "unknown_not_yet_mature"


def test_health_context_requires_explicit_decision_time_snapshot():
    identity, decisions, fills, snapshots = fixture()
    decisions[0]["decision_context"] = {"health_multiplier": .8, "strategy_health": "active"}
    decisions[0]["decision_context_available_at"] = "2024-01-02"
    decisions[1]["decision_context"] = {"health_multiplier": .5}
    decisions[1]["decision_context_available_at"] = "2024-01-03"
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].health_multiplier == .8
    assert frame.iloc[0].strategy_health == "active"
    assert frame.iloc[1:3].health_multiplier.isna().all()


@pytest.mark.parametrize("mutation,status", [
    ("partial", "unknown_unclosed_or_partial_exit"),
    ("terminal", "unknown_terminal_liquidation_not_strategy_exit"),
    ("cost", "unknown_entry_cost_reconciliation"),
    ("missing_cost", "unknown_incomplete_lot_costs"),
    ("no_identity", "unknown_unclosed_or_partial_exit"),
    ("before_decision", "unknown_fill_precedes_decision"),
])
def test_incomplete_or_non_strategy_outcomes_never_become_zero_targets(mutation, status):
    identity, decisions, fills, snapshots = fixture()
    if mutation == "partial":
        del fills[2]
    elif mutation == "terminal":
        fills[2]["exit_reason"] = "EndOfBacktest"
    elif mutation == "cost":
        fills[2]["lot_closes"][0]["entry_cost_share"] = .01
    elif mutation == "missing_cost":
        del fills[2]["lot_closes"][0]["entry_cost_share"]
    elif mutation == "no_identity":
        del fills[2]["lot_closes"][0]["entry_order_id"]
    else:
        fills[0]["fill_time"] = "2024-01-01"
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == status
    assert frame.label_net_return.isna().all()


def test_two_candidates_sharing_exit_fill_allocate_cost_by_authoritative_quantities():
    identity, decisions, fills, snapshots = fixture()
    decisions[1]["order_id"] = "O2"
    fills[1:3] = [{"order_id": "O2", "side": "buy", "symbol": "B", "fill_time": "2024-01-02",
                   "qty": 1., "fill_price": 10., "commission": .1},
                  {"order_id": "XA", "side": "sell", "symbol": "A", "fill_time": "2024-01-03",
                   "qty": 3., "fill_price": 12., "commission": .3, "exit_reason": "strategy",
                   "lot_closes": [{"entry_order_id": "O1", "qty_closed": 2., "entry_price": 10., "entry_cost_share": .2},
                                  {"entry_order_id": "unrelated", "qty_closed": 1., "entry_price": 10., "entry_cost_share": .1}]}]
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_net_pnl == pytest.approx(3.6)
    assert frame.iloc[1].label_status == "unknown_unclosed_or_partial_exit"


def test_cash_and_margin_accounts_keep_financing_contract_distinct():
    identity, decisions, fills, snapshots = fixture()
    identity["account_mode"] = "spot_margin"
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == "unknown_unattributed_financing"
    assert frame.iloc[0].actual_fill_net_pnl == pytest.approx(2.57)
    identity["financing_attribution_complete"] = True
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01", financing_costs={"A1": .57})
    assert frame.iloc[0].label_net_pnl == pytest.approx(2.)


def test_unknown_account_mode_and_bad_accounting_fail_closed():
    identity, decisions, fills, snapshots = fixture()
    del identity["account_mode"]
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == "unknown_unattributed_financing"
    identity["accounting_ok"] = False
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == "unknown_accounting_or_risk_termination"


def test_missing_causal_features_and_identifiers_keep_rows_and_unknown_groups():
    identity, decisions, fills, snapshots = fixture()
    decisions[1].pop("decision_id")
    decisions[1].pop("as_of")
    snapshots.loc[0, "available_at"] = "2024-02-01"
    snapshots.loc[1:, "available_at"] = "2024-01-02"
    frame, contract = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert len(frame) == 3
    assert frame.iloc[0].feature_status == "unknown_or_incomplete_snapshot"
    assert frame.iloc[0][list(FEATURE_COLUMNS)].isna().all()
    assert frame.iloc[-1].label_status == "unknown_decision_identity_or_time"
    assert not frame.training_eligible.any()
    assert any(g["candidate_group_id"].endswith("unknown_day") for g in contract["groups"])


def test_proxy_is_explicit_and_cannot_replace_actual_execution():
    actual, _ = build()
    proxy, contract = build(target_type="proxy")
    assert actual.iloc[0].label_net_return > 0
    assert proxy.iloc[0].label_net_return == -.1
    assert proxy.label_basis.str.contains("proxy").all()
    assert contract["target_type"] == "proxy"
    unknown, _ = build(target_type="proxy", cutoff="2024-01-24")
    assert unknown.label_net_return.isna().all()
    with pytest.raises(ValueError, match="paired"):
        build(target_type="paired_portfolio_marginal")


def test_proxy_short_or_wrong_estimand_remains_unknown():
    identity, decisions, fills, snapshots = fixture()
    decisions[0]["original_signal"]["action"] = "short"
    snapshots.loc[1, "label_basis"] = "observed_actual_exit"
    # DataFrame NaNs in the newly added basis column are explicit unknowns.
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity,
        labels_as_of="2024-02-01", target_type="proxy")
    assert frame.label_net_return.isna().all()
    assert frame.label_status.str.contains("direction_or_basis").all()


@pytest.mark.parametrize("mutation,status", [
    ("pending", "unknown_pending_order_completion"),
    ("entry_price", "unknown_entry_price_reconciliation"),
    ("risk", "unknown_accounting_or_risk_termination"),
])
def test_actual_label_admission_requires_final_reconciled_account_facts(mutation, status):
    identity, decisions, fills, snapshots = fixture()
    if mutation == "pending":
        identity["pending_orders"] = 1
    elif mutation == "entry_price":
        fills[1]["lot_closes"][0]["entry_price"] = 9.
    else:
        identity["terminated_by_risk"] = True
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == status
    assert np.isnan(frame.iloc[0].label_net_return)


@pytest.mark.parametrize("pending,status", [
    ([{"order_id": "O1", "status": "partially_filled", "remaining_qty": 1.}], "unknown_pending_order_completion"),
    ({"O1": {"status": "accepted"}}, "unknown_pending_order_completion"),
    (({"order_id": "O1"},), "unknown_pending_order_completion"),
    (None, "unknown_pending_order_state"),
    (True, "unknown_pending_order_state"),
    (-1, "unknown_pending_order_state"),
    (.5, "unknown_pending_order_state"),
    ("unknown", "unknown_pending_order_state"),
])
def test_pending_order_collections_and_missing_state_never_admit_actual_exit_targets(pending, status):
    identity, decisions, fills, snapshots = fixture()
    # FullEngineEnvironment.summary emits terminal_valuation.pending_orders
    # as a list of order records, rather than a numeric count.
    identity["pending_orders"] = pending
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == status
    assert np.isnan(frame.iloc[0].label_net_return)
    assert not frame.iloc[0].training_eligible


@pytest.mark.parametrize("pending", [[], (), {}, 0, 0., "0"])
def test_explicitly_empty_pending_order_state_admits_closed_actual_exit(pending):
    identity, decisions, fills, snapshots = fixture()
    identity["pending_orders"] = pending
    frame, _ = build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")
    assert frame.iloc[0].label_status == "mature_actual_exit"
    assert frame.iloc[0].label_net_pnl == pytest.approx(2.57)


@pytest.mark.parametrize("duplicate", ["decision", "order", "snapshot", "close_event"])
def test_ambiguous_identities_rejected(duplicate):
    identity, decisions, fills, snapshots = fixture()
    if duplicate == "decision":
        decisions.append(deepcopy(decisions[0]))
    elif duplicate == "order":
        decisions[1]["order_id"] = "O1"
    elif duplicate == "snapshot":
        snapshots = pd.concat([snapshots, snapshots.iloc[:1]])
    else:
        fills[2]["lot_closes"][0]["close_event_id"] = "LC1"
    with pytest.raises(ValueError, match="duplicate|multiple"):
        build_candidate_dataset(decisions, fills, snapshots, identity=identity, labels_as_of="2024-02-01")


def pairs():
    contract = dict.fromkeys(PAIR_IDENTITY_FIELDS, "identity")
    contract.update(initial_capital=100., evaluation_start="2024-01-01", evaluation_end="2024-01-04",
                    account_mode="spot", timeframe="1d", intervention={"candidate_id": "A1", "as_of": "2024-01-02", "include_candidate": False})
    control = {"contract": contract, "equity_curve": pd.DataFrame({"equity": [100., 100., 101., 102.]}, index=pd.date_range("2024-01-01", periods=4)),
               "accounting_ok": True, "status": "complete"}
    treatment = deepcopy(control)
    treatment["contract"]["intervention"]["include_candidate"] = True
    treatment["equity_curve"]["equity"] = [100., 99., 102., 106.]
    return control, treatment


def test_paired_value_requires_actual_account_comparison():
    control, treatment = pairs()
    result = compare_paired_accounts(control, treatment, labels_as_of="2024-02-01")
    assert result["portfolio_marginal_net_return"] == pytest.approx(.04)
    assert result["portfolio_marginal_net_pnl"] == 4.
    assert result["individual_trade_return"] is None
    assert "Declared" in result["causal_scope"]
    treatment["status"] = "incomplete"
    result = compare_paired_accounts(control, treatment, labels_as_of="2024-02-01")
    assert result["portfolio_marginal_net_return"] is None


def test_paired_value_must_mature_before_use():
    control, treatment = pairs()
    result = compare_paired_accounts(control, treatment, labels_as_of="2024-01-04")
    assert result["portfolio_marginal_net_return"] is None
    assert result["label_available_at"] == "2024-01-05T00:00:00+00:00"


@pytest.mark.parametrize("mutation", ["identity", "missing", "grid", "prefix", "intervention", "capital"])
def test_paired_account_mismatches_are_not_silently_intersected(mutation):
    control, treatment = pairs()
    if mutation == "identity":
        treatment["contract"]["account_mode"] = "spot_margin"
    elif mutation == "missing":
        control["contract"].pop("data_identity")
    elif mutation == "grid":
        treatment["equity_curve"] = treatment["equity_curve"].iloc[1:]
    elif mutation == "prefix":
        treatment["equity_curve"].iloc[0, 0] = 101.
    elif mutation == "capital":
        control["contract"]["initial_capital"] = treatment["contract"]["initial_capital"] = float("nan")
    else:
        treatment["contract"]["intervention"]["candidate_id"] = "different"
    with pytest.raises(ValueError):
        compare_paired_accounts(control, treatment, labels_as_of="2024-02-01")


def archive(tmp_path):
    source = tmp_path / "source"
    episode = source / "episodes" / "native"
    episode.mkdir(parents=True)
    identity, decisions, fills, snapshots = fixture()
    protocol = {"data_evidence": {"input_file_hashes": {"A": "hash"}}, "engine_options": {"account_mode": "spot"}, "settings": {}, "parameters": {}}
    protocol["protocol_id"] = hashlib.sha256(canonical_json(protocol).encode()).hexdigest()
    (source / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")
    (episode / "summary.json").write_text(json.dumps(identity), encoding="utf-8")
    pd.DataFrame(decisions).to_csv(episode / "decision_ledger.csv", index=False)
    pd.DataFrame(fills).to_csv(episode / "fill_ledger.csv", index=False)
    snapshots.to_csv(source / "dataset.csv", index=False)
    manifest = {path.relative_to(source).as_posix(): sha256_file(path) for path in source.rglob("*") if path.is_file()}
    (source / "artifacts.json").write_text(json.dumps(manifest), encoding="utf-8")
    return source


def test_archived_export_preserves_sources_and_verifies_receipts(tmp_path):
    source = archive(tmp_path)
    before = {p: sha256_file(p) for p in source.rglob("*") if p.is_file()}
    output = tmp_path / "candidates"
    frame, contract = build_from_episode(source, "episodes/native", output, labels_as_of="2024-02-01")
    assert len(frame) == 3
    assert contract["account_identity"]["source_identity_status"] == "manifest_verified"
    assert contract["source_receipts"]
    assert (output / "candidates.csv").is_file()
    assert before == {p: sha256_file(p) for p in source.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        build_from_episode(source, "episodes/native", output, labels_as_of="2024-02-01")
    with pytest.raises(ValueError, match="outside"):
        build_from_episode(source, "episodes/native", source / "new", labels_as_of="2024-02-01")


def test_archived_missing_inputs_or_tampering_are_actionable(tmp_path):
    source = archive(tmp_path)
    dataset = source / "dataset.csv"
    dataset.write_text(dataset.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        build_from_episode(source, "episodes/native", tmp_path / "new", labels_as_of="2024-02-01")
    dataset.unlink()
    with pytest.raises(FileNotFoundError, match="required archived"):
        build_from_episode(source, "episodes/native", tmp_path / "new", labels_as_of="2024-02-01")
    assert not (tmp_path / "new").exists()


def test_archived_default_inputs_cannot_escape_via_symlink(tmp_path):
    source = archive(tmp_path)
    original = source / "dataset.csv"
    external = tmp_path / "external.csv"
    original.rename(external)
    original.symlink_to(external)
    with pytest.raises(ValueError, match="escapes"):
        build_from_episode(source, "episodes/native", tmp_path / "new", labels_as_of="2024-02-01")
    assert not (tmp_path / "new").exists()


def test_cli_reads_ledgers_without_replaying_or_training(tmp_path, capsys):
    from scripts.build_selector_candidates import main
    source = archive(tmp_path)
    assert main(["--run-dir", str(source), "--episode", "episodes/native", "--output", str(tmp_path / "new"),
                 "--labels-as-of", "2024-02-01"]) == 0
    assert "未进行训练" in capsys.readouterr().out


def candidate_artifact(tmp_path, *, target_type="actual_exit"):
    source = archive(tmp_path)
    _, decisions, _, _ = fixture()
    # One fully closed day plus a separate unknown day. Do not delete unknown
    # candidates within a group to manufacture complete ranking labels.
    decisions = decisions[:2]
    decisions[1]["as_of"] = "2024-01-03"
    pd.DataFrame(decisions).to_csv(source / "episodes/native/decision_ledger.csv", index=False)
    manifest = {p.relative_to(source).as_posix(): sha256_file(p) for p in source.rglob("*")
                if p.is_file() and p.name != "artifacts.json"}
    (source / "artifacts.json").write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "candidates"
    _, contract = build_from_episode(source, "episodes/native", output, labels_as_of="2024-02-01", target_type=target_type)
    return source, output, contract


@pytest.mark.parametrize("target_type", ["actual_exit", "proxy"])
def test_training_adapter_validates_targets_and_preserves_whole_cohorts(tmp_path, target_type):
    _, output, contract = candidate_artifact(tmp_path, target_type=target_type)
    frame, receipt = load_candidate_training_rows(output, target_type=target_type, account_mode="spot",
        data_identity=contract["account_identity"]["data_identity"])
    assert frame.decision_id.tolist() == ["A1"]
    assert frame.eligible.all()
    assert frame.label_available_at.dt.tz is not None
    assert receipt["source_rows"] == 2
    assert receipt["admitted_rows"] == 1
    assert receipt["excluded_rows"] == 1
    assert len(receipt["excluded_incomplete_group_ids"]) == 1
    assert receipt["source_protocol_verified"] is True
    assert receipt["all_source_receipts_verified"] is True
    assert receipt["training_performed"] is False
    assert receipt["target_type"] == target_type
    assert frame.attrs["training_data_receipt"] == receipt


@pytest.mark.parametrize("mismatch", ["target", "mode", "data", "cutoff"])
def test_training_adapter_rejects_wrong_contract_and_immature_cohorts(tmp_path, mismatch):
    _, output, contract = candidate_artifact(tmp_path)
    kwargs = {"target_type": "actual_exit", "account_mode": "spot", "data_identity": contract["account_identity"]["data_identity"]}
    if mismatch == "target":
        kwargs["target_type"] = "proxy"
    elif mismatch == "mode":
        kwargs["account_mode"] = "spot_margin"
    elif mismatch == "data":
        kwargs["data_identity"] = "wrong"
    else:
        kwargs["training_as_of"] = "2024-01-04"
    with pytest.raises(ValueError, match="mismatch|insufficient"):
        load_candidate_training_rows(output, **kwargs)


@pytest.mark.parametrize("tamper", ["contract", "candidates", "source"])
def test_training_adapter_rejects_any_changed_source_or_output(tmp_path, tamper):
    source, output, contract = candidate_artifact(tmp_path)
    path = {"contract": output / "contract.json", "candidates": output / "candidates.csv",
            "source": source / "episodes/native/summary.json"}[tamper]
    if tamper == "contract":
        value = json.loads(path.read_text())
        value["labels_as_of"] = "2030-01-01"
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        path.write_text(path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        load_candidate_training_rows(output, target_type="actual_exit", account_mode="spot",
            data_identity=contract["account_identity"]["data_identity"])


def test_training_adapter_rejects_partial_day_instead_of_training_on_only_winners(tmp_path):
    source = archive(tmp_path)
    output = tmp_path / "candidate"
    _, contract = build_from_episode(source, "episodes/native", output, labels_as_of="2024-02-01")
    with pytest.raises(ValueError, match="complete mature account-day cohort"):
        load_candidate_training_rows(output, target_type="actual_exit", account_mode="spot",
            data_identity=contract["account_identity"]["data_identity"])


def test_training_adapter_requires_frozen_source_manifest(tmp_path):
    source = archive(tmp_path)
    (source / "artifacts.json").unlink()
    output = tmp_path / "candidate"
    _, contract = build_from_episode(source, "episodes/native", output, labels_as_of="2024-02-01")
    with pytest.raises(ValueError, match="verified frozen protocol and source manifest"):
        load_candidate_training_rows(output, target_type="actual_exit", account_mode="spot",
            data_identity=contract["account_identity"]["data_identity"])
