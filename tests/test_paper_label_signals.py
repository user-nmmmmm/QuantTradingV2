import pandas as pd

from analysis.paper_label_signals import chronological_label_targets, fixed_horizon_cost_labels
from analysis.paper_labels import LabelCosts


def test_labels_available_at_decision_or_later_cannot_train_current_prediction():
    timeline = pd.date_range("2020-01-01", periods=10, tz="UTC")
    candidates = [{"candidate_id": "c", "symbol": "BTC/USDT", "direction": "long",
        "timestamp": timeline[3].isoformat(), "context": {"available_at": timeline[4].isoformat()}}]
    labels = [{"candidate_id": str(i), "training_eligible": True, "direction": "long",
        "available_at": timeline[i].isoformat(), "net_return_bps": 10. if i < 4 else -999.} for i in range(7)]
    targets, report = chronological_label_targets(candidates, labels, timeline, ["BTC/USDT"],
                                                 minimum_train=2, holding_bars=2)
    p = report["predictions"][0]
    assert p["training_count"] == 4 and p["estimate_bps"] == 10.
    assert targets.iloc[2, 0] == 0
    assert targets.iloc[3, 0] == .9 and targets.iloc[4, 0] == .9
    assert targets.iloc[5, 0] == 0
    labels[-1]["net_return_bps"] = 9999.
    again, _ = chronological_label_targets(candidates, labels, timeline, ["BTC/USDT"], minimum_train=2, holding_bars=2)
    pd.testing.assert_frame_equal(targets, again)


def test_fixed_labels_use_same_reference_cost_basis():
    rows = fixed_horizon_cost_labels([{"candidate_id": "x", "horizon_bars": 5, "status": "matured",
        "entry_reference": 100., "exit_reference": 102., "direction": "long", "net_pnl": 999., "commission": 123.}],
        costs=LabelCosts(commission_bps_per_side=5))
    assert abs(rows[0]["net_return_bps"] - 189.9) < 1e-10
    assert rows[0]["net_return_bps"] == rows[0]["gross_return_bps"] - sum(rows[0]["cost_components_bps"].values())
    assert "net_pnl" not in rows[0] and "commission" not in rows[0]
    assert rows[0]["source_outcome"]["net_pnl"] == 999.
