"""Legacy ownership/cost gaps remain visible through report and research adapters."""
from copy import deepcopy
import json

import pandas as pd
import pytest

from analysis.research_validation import evaluate_holdout_admission
from backtest.reporting.trades import TradeReconstructionMixin
from backtest.reporting import ReportGenerator


def records(symbol="A"):
    return [dict(symbol=symbol, side="buy", qty=2, fill_price=100, commission=1, strategy="ArchiveOwner", order_id="entry"),
            dict(symbol=symbol, side="sell", qty=2, fill_price=110, commission=1, order_id="exit")]


@pytest.mark.parametrize("field,value,reason", [
    ("strategy", None, "missing_opening_ownership"),
    ("commission", None, "invalid_or_missing_commission"),
    ("commission", float("nan"), "invalid_or_missing_commission"),
    ("qty", 0, "invalid_or_missing_qty"),
    ("fill_price", float("inf"), "invalid_or_missing_fill_price"),
])
def test_bad_archive_retains_raw_rows_and_never_becomes_normal_sample(field, value, reason):
    rows = records()
    rows[0][field] = value
    frame = pd.DataFrame(rows)
    before = frame.copy(deep=True)
    adapter = TradeReconstructionMixin()
    legs = adapter._reconstruct_closed_trades(frame)
    trips = adapter._aggregate_round_trips(legs)
    assert len(trips) == 1 and trips[0]["status"] == "invalid_input"
    assert any(reason in message for message in trips[0]["invalid_reasons"])
    assert trips[0]["net_pnl"] is None and len(trips[0]["raw_rows"]) == 2
    assert trips[0]["source_row_indices"] == [0, 1]
    assert adapter._trade_metrics_from_closed(trips)["NetPnL"] is None
    pd.testing.assert_frame_equal(frame, before)


def test_mixed_archive_keeps_bad_symbol_visible_to_research_admission():
    rows = records("A") + records("B")
    rows[0].pop("strategy")
    adapter = TradeReconstructionMixin()
    trips = adapter._aggregate_round_trips(adapter._reconstruct_closed_trades(pd.DataFrame(rows)))
    assert len(trips) == 2
    assert trips[1]["net_pnl"] == 18
    equity = pd.Series([100, 110, 120], index=pd.date_range("2024-01-01", periods=3))
    admission = evaluate_holdout_admission(trades=trips, equity=equity, benchmark=equity)
    assert admission["decision"] == "reject"
    assert len(admission["input_integrity"]["invalid_trades"]) == 1


def test_legacy_strategy_alias_and_primary_exact_order_ownership_are_preserved():
    adapter = TradeReconstructionMixin()
    rows = records()
    ordinary = adapter._reconstruct_closed_trades(pd.DataFrame(rows))
    assert ordinary[0]["strategy"] == "ArchiveOwner" and ordinary[0]["net_pnl"] == 18
    rows[0].pop("strategy")
    rows[1]["lot_closes"] = [dict(lot_id="lot", position_id="position", entry_order_id="entry",
                                       strategy_id="PrimaryOwner", entry_price=100, entry_cost_share=1,
                                       qty_closed=2, initial_risk=20)]
    primary = adapter._reconstruct_closed_trades(pd.DataFrame(rows))
    assert primary[0]["strategy"] == "PrimaryOwner" and primary[0]["net_pnl"] == 18
    broken = deepcopy(rows)
    broken[1]["lot_closes"][0]["entry_cost_share"] = float("nan")
    assert adapter._reconstruct_closed_trades(pd.DataFrame(broken))[0]["status"] == "invalid_input"


def test_report_exports_invalid_raw_facts_and_never_prints_a_valid_headline(tmp_path, monkeypatch):
    for name in ("_plot_equity", "_plot_monthly_heatmap", "_plot_rolling_metrics", "_plot_pnl_distribution"):
        monkeypatch.setattr(ReportGenerator, name, lambda *args, **kwargs: None)
    monkeypatch.setattr("backtest.reporting.render.pdf.write_pdf_report", lambda *args, **kwargs: None)
    rows = records()
    rows[0].pop("commission")
    curve = pd.DataFrame({"equity": [100, 110, 120]}, index=pd.date_range("2024-01-01", periods=3))
    metrics = ReportGenerator(str(tmp_path)).generate(rows, curve)
    assert metrics["TradeInputIntegrity"]["status"] == "invalid_input"
    assert metrics["NetPnL"] is None and metrics["ProfitFactorStatus"] == "invalid_input"
    exported = json.loads((tmp_path / "invalid_closed_trades.json").read_text())["metrics"]
    assert exported["status"] == "invalid_input" and len(exported["records"][0]["raw_rows"]) == 2
    closed = pd.read_csv(tmp_path / "closed_trades.csv")
    assert closed.iloc[0]["status"] == "invalid_input" and pd.isna(closed.iloc[0]["net_pnl"])
    reconciliation = json.loads((tmp_path / "reconciliation.json").read_text())["metrics"]
    assert reconciliation["status"] == "invalid_input"
    assert reconciliation["report_net_pnl"] is None
