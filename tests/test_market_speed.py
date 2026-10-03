from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from analysis.market_speed import summarize_market_speed


def test_velocity_uses_actual_elapsed_minutes_and_excludes_unclosed_bars():
    prices = 100. * np.exp(np.array([0., .001, .003, .005]))
    frame = pd.DataFrame({"close": prices, "high": prices * 1.001, "low": prices * .999},
        index=pd.to_datetime(["2026-10-02T00:00:00Z", "2026-10-02T00:01:00Z",
                              "2026-10-02T00:03:00Z", "2026-10-02T00:05:00Z"]))
    result = summarize_market_speed(frame, timeframe="1m",
        observed_at=datetime(2026, 10, 2, 0, 5, tzinfo=timezone.utc))
    assert result["closed_bars"] == 3
    assert result["excluded_unclosed_bars"] == 1
    assert result["gap_count"] == 1
    assert result["last_closed_bar_age_seconds"] == 60.
    assert result["velocity_bps_per_minute"]["median_absolute"] == pytest.approx(10.)
    assert result["velocity_bps_per_minute"]["p95_absolute"] == pytest.approx(10.)


def test_invalid_prices_do_not_turn_into_zero_returns():
    frame = pd.DataFrame({"close": [100., 101., -1., 103.], "high": [102.] * 4, "low": [99.] * 4},
                         index=pd.date_range("2026-10-01", periods=4, freq="min"))
    result = summarize_market_speed(frame, timeframe="1m",
        observed_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    assert result["valid_price_pairs"] == 1
    assert result["velocity_bps_per_minute"]["median_absolute"] == pytest.approx(np.log(1.01) * 10000.)


def test_insufficient_closed_data_fails_explicitly():
    frame = pd.DataFrame({"close": [100.], "high": [101.], "low": [99.]},
                         index=pd.date_range("2026-10-02", periods=1))
    with pytest.raises(ValueError, match="two closed bars"):
        summarize_market_speed(frame, timeframe="1d",
            observed_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
