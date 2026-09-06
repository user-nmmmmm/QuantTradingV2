"""Public-data downloader boundaries, without network access."""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.run_expanded_universe_backtest import EXTRA_BASES, DEFAULT_SYMBOLS, fetch_symbol


def bars(count=32):
    return [[int(t.timestamp() * 1000), "10", "12", "9", "11", "100"]
            for t in pd.date_range("2026-07-31", periods=count, tz="UTC")]


def download(tmp_path, rows):
    session = MagicMock()
    response = session.get.return_value
    response.json.return_value = rows
    response.url = "https://data-api.binance.vision/api/v3/klines"
    with patch("scripts.run_expanded_universe_backtest.requests.Session") as factory:
        factory.return_value.__enter__.return_value = session
        entry = fetch_symbol("BTC/USDT", tmp_path, "2026-08-01", "2026-08-31")
    return entry, session


def test_inclusive_end_and_no_pre_start_bars(tmp_path):
    entry, session = download(tmp_path, bars(33))
    assert entry["rows"] == 31
    assert entry["first"] == pd.Timestamp("2026-08-01")
    assert entry["last"] == pd.Timestamp("2026-08-31")
    assert not entry["ends_before_requested_end"]
    params = session.get.call_args.kwargs["params"]
    assert params["endTime"] == int(pd.Timestamp("2026-09-01", tz="UTC").timestamp() * 1000) - 1


@pytest.mark.parametrize("rows", [[], bars(10), bars() + [bars()[0]]])
def test_reject_empty_short_or_duplicate_data(tmp_path, rows):
    with pytest.raises(ValueError):
        download(tmp_path, rows)


def test_predeclared_unique_sixty_symbols():
    symbols = DEFAULT_SYMBOLS + [f"{base}/USDT" for base in EXTRA_BASES]
    assert len(symbols) == len(set(symbols)) == 60
