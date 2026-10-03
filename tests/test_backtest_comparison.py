import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from analysis.backtest_comparison import (
    add_closed_directions, compare_trade_records, generate_comparison,
    read_report_csv, report_trade_history, verify_main_reports, verify_paper_reports,
)
from core.reproducibility import canonical_json, sha256_frame


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def closed(**changes):
    return {"position_id": "POS-1", "symbol": "BTC-USDT", "strategy": "TrendBreakout",
        "direction": "long", "entry_time": "2024-01-01", "exit_time": "2024-01-03",
        "gross_pnl": -10.0, "gross_pnl_theoretical": -8.0, "commission": 1.0,
        "slippage": 2.0, "qty": .1, "net_pnl": -11.0, "exit_reason": "stop", **changes}


def fill(**changes):
    return {"order_id": "order-1", "symbol": "BTC-USDT", "strategy_id": "TrendBreakout",
        "side": "buy", "signal_time": "2023-12-31", "fill_time": "2024-01-01",
        "qty": .1, "fill_price": 100.0, "commission": 1.0, "close_event_ids": "[]",
        "costs": "{'slippage': 2.0, 'impact': 0.5}", **changes}


def main_report(root):
    root.mkdir()
    data = root / "data_inputs/BTC.csv"
    data.parent.mkdir()
    pd.DataFrame({"timestamp": ["2024-01-01"], "close": [100.]}).to_csv(data, index=False)
    equity = root / "equity.csv"
    pd.DataFrame({"timestamp": ["2024-01-01"], "equity": [10000.]}).to_csv(equity, index=False)
    h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    manifest = {"config": {"content": "unchanged", "sha256": "config"},
        "period": {"requested": {"start": "2024-01-01", "end": "2024-01-01"}},
        "artifacts": {"equity.csv": {"sha256": h(equity), "size": equity.stat().st_size}},
        "data": {"symbols": {"BTC": {"sha256": h(data), "rows": 1}}},
        "data_snapshots": {"BTC": {"path": "BTC.csv", "sha256": h(data), "rows": 1}},
        "execution": {"capital": 10000.}, "code": {"git_sha": "baseline"}}
    dump(root / "run_manifest.json", manifest)
    return manifest


def paper_report(root, cache):
    root.mkdir()
    folder = root / "runs/job"
    folder.mkdir(parents=True)
    frame = pd.read_csv(cache, index_col="timestamp", parse_dates=True, float_precision="round_trip")
    reg = {"config_sha256": "config", "frame_hashes": {"BTC": sha256_frame(frame)},
        "data": {"symbols": {"BTC": {"path": str(cache), "sha256": hashlib.sha256(cache.read_bytes()).hexdigest()}}},
        "jobs": [{"name": "job", "parameters": {"risk": 1}, "start": "2024-01-01", "end": "2024-01-02"}],
        "maximum_runs": 2, "candidate_family": ["job"], "label_protocol": {"horizon": 5}}
    dump(root / "registration.json", reg)
    dump(folder / "resolved_config.json", reg["jobs"][0]["parameters"])
    dump(folder / "summary.json", {"start": "2024-01-01", "end": "2024-01-02"})
    dump(root / "runs/coverage_full_history/resolved_config.json", {"same": True})
    dump(root / "results.json", {"completed_runs": 2,
        "registration_sha256": hashlib.sha256(canonical_json(reg).encode()).hexdigest()})
    return reg


def test_closed_semantic_matching_keeps_tolerance_and_exact_time():
    old = pd.DataFrame([closed()])
    changed = closed(net_pnl=-11.0 + 1e-9)
    assert compare_trade_records(old, pd.DataFrame([changed])).status.tolist() == ["matched"]
    changed = closed(net_pnl=-10.9)
    diff = compare_trade_records(old, pd.DataFrame([changed]))
    assert diff.status.tolist() == ["changed"]
    assert diff.changed_fields.iloc[0] == "net_pnl"
    changed = closed(entry_time="2024-01-01T00:00:00.000001Z")
    assert set(compare_trade_records(old, pd.DataFrame([changed])).status) == {"added", "removed"}


@pytest.mark.parametrize("change", [{"symbol": "ETH-USDT"}, {"direction": "short"},
    {"strategy": "RangeMeanReversion"}, {"entry_time": "2024-02-01"}])
def test_position_id_reuse_never_forces_match(change):
    diff = compare_trade_records(pd.DataFrame([closed()]), pd.DataFrame([closed(**change)]))
    assert set(diff.status) == {"added", "removed"}
    assert "changed" not in diff.status.tolist()


def test_partial_fill_multiset_pairs_identical_fragments_only_and_price_tolerance_is_tighter():
    a = pd.DataFrame([fill(qty=.1), fill(qty=.2)])
    b = pd.DataFrame([fill(qty=.2), fill(qty=.1)])
    assert compare_trade_records(a, b, kind="fill").status.tolist() == ["matched", "matched"]
    b = pd.DataFrame([fill(qty=.2), fill(qty=.11)])
    diff = compare_trade_records(a, b, kind="fill")
    assert set(diff.status) == {"matched", "added", "removed"}
    diff = compare_trade_records(pd.DataFrame([fill()]), pd.DataFrame([fill(fill_price=100 + 1e-8)]), kind="fill")
    assert diff.status.tolist() == ["changed"]
    assert "fill_price" in diff.changed_fields.iloc[0]


def test_missing_direction_is_unpaired_and_lot_direction_conflict_remains_unknown():
    no_direction = pd.DataFrame([closed(direction=None)])
    assert set(compare_trade_records(no_direction, no_direction).status) == {"removed", "added"}
    fills = pd.DataFrame([fill(lot_closes="[{'position_id': 'POS-1', 'side': 'long'}]"),
                          fill(lot_closes="[{'position_id': 'POS-1', 'side': 'short'}]")])
    result = add_closed_directions(no_direction, fills)
    assert result.direction.iloc[0] is None


def test_main_inputs_and_artifacts_tamper_fail_before_comparison(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    main_report(old)
    manifest = main_report(new)
    assert verify_main_reports(old, new)["status"] == "verified_same_inputs"
    (old / "equity.csv").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_main_reports(old, new)
    with pytest.raises(ValueError, match="hash mismatch"):
        generate_comparison(old, new, tmp_path / "paper", tmp_path / "paper", tmp_path / "output", "merge")
    assert not (tmp_path / "output").exists()
    main_report(tmp_path / "different")
    manifest["period"]["requested"]["end"] = "2024-01-02"
    dump(new / "run_manifest.json", manifest)
    with pytest.raises(ValueError, match="period identity differs"):
        verify_main_reports(tmp_path / "different", new)


def test_main_input_snapshot_tamper_and_output_reuse_rejected(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    main_report(old)
    main_report(new)
    (new / "data_inputs/BTC.csv").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_main_reports(old, new)
    output = tmp_path / "output"
    output.mkdir()
    with pytest.raises(FileExistsError):
        generate_comparison(old, new, old, new, output, "merge")


def test_market_identity_and_output_inside_frozen_reports_are_rejected(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    main_report(old)
    manifest = main_report(new)
    manifest["data"]["market_type"] = "perpetual"
    dump(new / "run_manifest.json", manifest)
    with pytest.raises(ValueError, match="market data identity differs"):
        verify_main_reports(old, new)
    with pytest.raises(ValueError, match="inside a frozen input report"):
        generate_comparison(old, new, old, new, old / "nested_output", "merge")
    assert not (old / "nested_output").exists()


def test_paper_parameter_byte_and_frame_identity_are_verified(tmp_path):
    cache = tmp_path / "cache.csv"
    pd.DataFrame({"timestamp": ["2024-01-01"], "close": [100.]}).to_csv(cache, index=False)
    old, new = tmp_path / "old", tmp_path / "new"
    paper_report(old, cache)
    reg = paper_report(new, cache)
    assert verify_paper_reports(old, new)["jobs_parameters_equal"]
    reg["jobs"][0]["parameters"]["risk"] = 2
    dump(new / "registration.json", reg)
    with pytest.raises(ValueError, match="registration/result hash mismatch"):
        verify_paper_reports(old, new)
    cache.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_paper_reports(old, old)


def test_empty_artifacts_no_trade_and_missing_financing_have_distinct_evidence(tmp_path):
    for filename in ("trades.csv", "closed_trades.csv", "financing_ledger.csv"):
        (tmp_path / filename).write_text("\n", encoding="utf-8")
    assert read_report_csv(tmp_path / "trades.csv").empty
    history, result = report_trade_history(tmp_path)
    assert history.empty
    assert result["closed_trades"] == result["fills"] == 0
    assert result["costs"]["separate_financing_net_expense"] == 0
    assert result["2026_no_trade_evidence"]["2026_fills"] == 0
    assert result["2026_no_trade_evidence"]["entry_audit_present"] is False
    (tmp_path / "financing_ledger.csv").unlink()
    assert report_trade_history(tmp_path)[1]["costs"]["separate_financing_net_expense"] is None


def test_cost_bridge_never_subtracts_embedded_slippage_twice_and_financing_is_separate(tmp_path):
    pd.DataFrame([closed()]).to_csv(tmp_path / "closed_trades.csv", index=False)
    pd.DataFrame([fill()]).to_csv(tmp_path / "trades.csv", index=False)
    pd.DataFrame({"kind": ["borrow", "funding"], "amount": [3., -1.]}).to_csv(tmp_path / "financing_ledger.csv", index=False)
    history, result = report_trade_history(tmp_path)
    assert history.net_pnl.iloc[0] == -11
    assert history.pnl_bridge_error.iloc[0] == 0
    assert history.embedded_execution_deviation.iloc[0] == 2
    assert result["costs"]["reported_fill_slippage"] == 2
    assert result["costs"]["reported_fill_impact"] == .5
    assert result["costs"]["separate_financing_net_expense"] == 2
    assert result["net_closed_pnl_before_separate_financing"] == -11
    assert history.financing_allocation_status.iloc[0] == "unallocated"
    assert pd.isna(history.allocated_financing_expense.iloc[0])
    assert history.holding_days.iloc[0] == 2


def test_trade_prices_require_full_quantity_lot_evidence_and_zero_ledger_can_allocate(tmp_path):
    pd.DataFrame([closed()]).to_csv(tmp_path / "closed_trades.csv", index=False)
    (tmp_path / "financing_ledger.csv").write_text("\n", encoding="utf-8")
    lots = [{"position_id": "POS-1", "side": "long", "strategy_id": "TrendBreakout",
             "qty_closed": .1, "entry_price": 110., "entry_order_id": "entry"}]
    pd.DataFrame([fill(side="sell", fill_price=100., lot_closes=str(lots))]).to_csv(tmp_path / "trades.csv", index=False)
    history, _ = report_trade_history(tmp_path)
    assert history.entry_fill_price.iloc[0] == 110
    assert history.exit_fill_price.iloc[0] == 100
    assert history.financing_allocation_status.iloc[0] == "zero_posted"
    assert history.allocated_financing_expense.iloc[0] == 0
    lots[0]["qty_closed"] = .05
    pd.DataFrame([fill(side="sell", fill_price=100., lot_closes=str(lots))]).to_csv(tmp_path / "trades.csv", index=False)
    history, _ = report_trade_history(tmp_path)
    assert pd.isna(history.entry_fill_price.iloc[0])
    assert "unknown" in history.fill_price_evidence.iloc[0]


def test_missing_fill_evidence_is_unknown_and_nonfinite_financing_is_rejected(tmp_path):
    assert report_trade_history(tmp_path)[1]["2026_no_trade_evidence"]["2026_fills"] is None
    pd.DataFrame({"amount": [float("nan")]}).to_csv(tmp_path / "financing_ledger.csv", index=False)
    with pytest.raises(ValueError, match="missing/nonfinite expense"):
        report_trade_history(tmp_path)


def test_add_on_state_never_overwrites_first_entry_state(tmp_path):
    pd.DataFrame([closed(qty=.2)]).to_csv(tmp_path / "closed_trades.csv", index=False)
    lots = [{"position_id": "POS-1", "side": "long", "entry_order_id": "entry"},
            {"position_id": "POS-1", "side": "long", "entry_order_id": "addon"}]
    pd.DataFrame([fill(order_id="entry"), fill(order_id="addon", signal_time="2024-01-01", fill_time="2024-01-02"),
                  fill(side="sell", fill_time="2024-01-03", lot_closes=str(lots))]).to_csv(tmp_path / "trades.csv", index=False)
    pd.DataFrame({"timestamp": ["2023-12-31", "2024-01-01"], "symbol": ["BTC-USDT"] * 2,
                  "regime": ["TREND_UP", "VOLATILE"]}).to_csv(tmp_path / "routing_log.csv", index=False)
    history, _ = report_trade_history(tmp_path)
    assert history.entry_state.iloc[0] == "TREND_UP"
    assert "later add-ons excluded" in history.entry_state_source.iloc[0]


def test_loss_streak_uses_real_exit_chronology_instead_of_symbol_input_order(tmp_path):
    trades = [closed(position_id="A", symbol="A", exit_time="2024-01-03", net_pnl=-3),
              closed(position_id="B", symbol="B", exit_time="2024-01-01", net_pnl=-2),
              closed(position_id="C", symbol="C", exit_time="2024-01-02", net_pnl=1),
              closed(position_id="D", symbol="D", exit_time="2024-01-04", net_pnl=-4)]
    pd.DataFrame(trades).to_csv(tmp_path / "closed_trades.csv", index=False)
    history, result = report_trade_history(tmp_path)
    assert history.symbol.tolist() == ["B", "C", "A", "D"]
    assert result["longest_consecutive_losing_closes"] == 2
    assert result["longest_loss_streak_total"] == -7


def test_state_uses_exact_signal_routing_only_and_health_zero_suppression_is_not_invented(tmp_path):
    pd.DataFrame([closed()]).to_csv(tmp_path / "closed_trades.csv", index=False)
    fills = pd.DataFrame([fill(order_id="entry"), fill(side="sell", fill_time="2024-01-03",
        lot_closes="[{'position_id': 'POS-1', 'side': 'long', 'entry_order_id': 'entry'}]")])
    fills.to_csv(tmp_path / "trades.csv", index=False)
    pd.DataFrame({"timestamp": ["2023-12-30", "2023-12-31"], "symbol": ["BTC-USDT", "BTC-USDT"],
        "regime": ["SIDEWAYS", "TREND_UP"], "strategy": ["Cash", "TrendBreakout"]}).to_csv(tmp_path / "routing_log.csv", index=False)
    dump(tmp_path / "strategy_health.json", {"TrendBreakout": {"raw_setup_count": 10,
        "last_raw_setup_at": "2025-08-28", "suppressed_raw_setups": 0, "allows_new_entries": True}})
    history, analysis = report_trade_history(tmp_path)
    assert history.entry_state.iloc[0] == "TREND_UP"
    assert analysis["2026_no_trade_evidence"]["health_setup_facts"]["TrendBreakout"]["suppressed_raw_setups"] == 0
    pd.DataFrame({"timestamp": ["2023-12-30"], "symbol": ["BTC-USDT"], "regime": ["SIDEWAYS"]}).to_csv(tmp_path / "routing_log.csv", index=False)
    assert report_trade_history(tmp_path)[0].entry_state.iloc[0] is None
