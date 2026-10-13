"""B7 (ADR 0025): ReportGenerator's four mixins are collaborators; the old surface keeps working."""
from __future__ import annotations

import inspect

import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from backtest.reporting.metrics import ReportMetrics
from backtest.reporting.render.charts import ReportCharts
from backtest.reporting.render.text import ReportWriters
from backtest.reporting.trades import TradeReconstruction

COLLABORATORS = {
    "_metrics": ReportMetrics, "_trade_reconstruction": TradeReconstruction,
    "_writers": ReportWriters, "_charts": ReportCharts,
}


def _public_methods(cls):
    return {name: member for name, member in vars(cls).items()
            if inspect.isfunction(member) or isinstance(member, staticmethod)}


def test_report_generator_no_longer_inherits_from_any_mixin():
    assert ReportGenerator.__mro__ == (ReportGenerator, object)


def test_every_method_the_mixins_provided_is_still_on_the_generator_with_the_same_signature():
    seen = set()
    for holder, collaborator in COLLABORATORS.items():
        for name, member in _public_methods(collaborator).items():
            if name == "__init__" or name in seen:
                continue
            seen.add(name)
            assert hasattr(ReportGenerator, name), f"{collaborator.__name__}.{name} has no forwarder"
            old = inspect.signature(getattr(collaborator, name))
            new = inspect.signature(getattr(ReportGenerator, name))
            if isinstance(member, staticmethod):
                assert old == new, name
            else:
                assert list(old.parameters)[1:] == list(new.parameters)[1:], name
                assert [p.default for p in list(old.parameters.values())[1:]] == \
                    [p.default for p in list(new.parameters.values())[1:]], name
    assert {"_reconstruct_closed_trades", "_aggregate_round_trips", "_save_report_text", "_plot_equity",
            "_calculate_equity_metrics", "_analyze_trades", "_write_exposure_section"} <= seen


def test_collaborators_are_created_lazily_once_per_instance_even_without_init():
    generator = ReportGenerator.__new__(ReportGenerator)                    # historical scripts build it this way
    assert not set(COLLABORATORS) & set(vars(generator))
    first = generator._trade_reconstruction
    assert generator._trade_reconstruction is first and "_trade_reconstruction" in vars(generator)
    assert isinstance(generator._metrics, ReportMetrics)
    assert generator._writers._host is generator and generator._charts._host is generator
    assert ReportGenerator.__new__(ReportGenerator)._trade_reconstruction is not first


def _fills():
    return pd.DataFrame([
        {"symbol": "BTC/USDT", "side": "buy", "qty": 1.0, "fill_price": 100.0, "fill_time": "2026-01-01",
         "commission": 0.1, "position_id": "p1", "strategy_id": "trend"},
        {"symbol": "BTC/USDT", "side": "sell", "qty": 1.0, "fill_price": 110.0, "fill_time": "2026-01-02",
         "commission": 0.1, "position_id": "p1", "strategy_id": "trend", "exit_reason": "signal"},
    ])


def test_standalone_reconstruction_matches_the_one_reached_through_the_generator():
    standalone = TradeReconstruction()
    via_generator = ReportGenerator.__new__(ReportGenerator)
    legs = standalone._reconstruct_closed_trades(_fills())
    assert legs and legs == via_generator._reconstruct_closed_trades(_fills())
    assert standalone._aggregate_round_trips(legs) == via_generator._aggregate_round_trips(legs)
    with pytest.raises(NotImplementedError):                                 # extras need a ReportGenerator host
        standalone._trade_metrics_from_closed(standalone._aggregate_round_trips(legs))


def test_trade_metrics_use_the_hosts_extended_analytics_so_overrides_still_apply():
    class Custom(ReportGenerator):
        def _extended_trade_analytics(self, closed_trades):
            return {"trade_quality": {"marker": len(closed_trades)}, "attribution": {}, "r_multiple": {},
                    "cost_sensitivity": {}}
    generator = Custom.__new__(Custom)
    closed = generator._aggregate_round_trips(generator._reconstruct_closed_trades(_fills()))
    metrics = generator._trade_metrics_from_closed(closed)
    assert metrics["TotalTrades"] == 1 and metrics["ExtendedAnalytics"]["trade_quality"] == {"marker": 1}


def test_the_text_report_reads_the_hosts_format_hooks_and_output_directory(tmp_path):
    class Hooked(ReportGenerator):
        calls = []

        @staticmethod
        def _format_metric_value(value):
            Hooked.calls.append(value)
            return f"<{value}>"
    generator = Hooked(str(tmp_path))
    generator._save_report_text({"TotalReturn": 0.1, "SharpeRatio": 1.5}, {"note": "x"}, {})
    text = (tmp_path / "report.txt").read_text(encoding="utf-8")
    assert "<0.1>" in text and 0.1 in Hooked.calls          # the writer called the host's hook, not its own copy
