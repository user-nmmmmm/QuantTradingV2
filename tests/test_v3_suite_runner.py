from copy import deepcopy

from config.config import config
from scripts.run_trend_portfolio_v3 import effective_config, source_hashes, suite_specs
from core.market_data import HistoricalMarketDataAdapter
import pandas as pd


def test_matrix_registered_without_result_driven_search():
    specs = suite_specs()
    assert len(specs) == 88
    assert len({s["run_id"] for s in specs}) == 88
    for variant in ("momentum", "breakout"):
        for financing in ("assumed", "verified_only"):
            subset = [s for s in specs if s["variant"] == variant and s["financing_mode"] == financing]
            assert sum(s["role"] == "rolling" for s in subset) == 13
            assert sum(s["role"] == "neighbor" for s in subset) == 4


def test_new_profile_does_not_mutate_old_configuration():
    before = deepcopy(config._config)
    profile = effective_config(before, suite_specs()[0])
    assert before == config._config
    assert profile["risk"]["max_leverage"] == 3
    assert profile["portfolio_risk"]["max_crypto_beta_exposure"] == 2
    assert profile["backtest"]["end_of_backtest_mode"] == "valuation_only"
    assert profile["strategy_governance"]["TrendPortfolioV3"] == "isolated_research"


def test_source_identity_includes_actual_routing_and_factor_dependencies():
    files = source_hashes()
    assert "router/router.py" in files
    assert "core/factors/volume.py" in files
    assert "core/selection_v2.py" in files


def test_window_stream_skips_events_but_preserves_exact_history_and_positions():
    dates = pd.date_range("2020-01-01", periods=200)
    frame = pd.DataFrame({"open": 100., "high": 102., "low": 98., "close": 100., "volume": 1000.}, index=dates)
    adapter = HistoricalMarketDataAdapter({"AAA": frame}, calculate_indicators=False)
    full = list(adapter.stream())
    active = list(adapter.stream(start_at=dates[180].tz_localize("UTC")))
    assert len(active) == 20
    for actual, expected in zip(active, full[180:]):
        assert actual.timestamp == expected.timestamp
        assert actual.positions == expected.positions
        pd.testing.assert_series_equal(actual.bars["AAA"], expected.bars["AAA"])
        pd.testing.assert_frame_equal(actual.histories["AAA"], frame)
