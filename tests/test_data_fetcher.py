import os
import unittest
from unittest.mock import MagicMock, patch

from core.data_fetcher import DataFetcher


class TestDataFetcherProxyConfig(unittest.TestCase):
    def test_explicit_proxy_overrides_environment(self):
        with patch.dict(os.environ, {"QUANT_PROXY_URL": "http://env-proxy:7890"}):
            fetcher = DataFetcher(proxy_url="http://explicit-proxy:8888")
            self.assertEqual(fetcher.proxy_url, "http://explicit-proxy:8888")
            self.assertEqual(
                fetcher._build_ccxt_proxies(),
                {
                    "http": "http://explicit-proxy:8888",
                    "https": "http://explicit-proxy:8888",
                },
            )

    def test_environment_proxy_used_when_not_explicitly_passed(self):
        with patch.dict(os.environ, {"QUANT_PROXY_URL": "http://env-proxy:7890"}):
            fetcher = DataFetcher()
            self.assertEqual(fetcher.proxy_url, "http://env-proxy:7890")

    def test_explicit_none_disables_proxy(self):
        with patch.dict(os.environ, {"QUANT_PROXY_URL": "http://env-proxy:7890"}):
            fetcher = DataFetcher(proxy_url=None)
            self.assertIsNone(fetcher.proxy_url)
            self.assertIsNone(fetcher._build_ccxt_proxies())

    @patch("ccxt.binance")
    def test_fetch_ccxt_enables_trust_env_on_session(self, mock_binance):
        mock_exchange = MagicMock()
        mock_exchange.session = MagicMock()
        mock_exchange.session.trust_env = False
        mock_exchange.fetch_ohlcv.side_effect = [
            [[1704067200000, 1.0, 2.0, 0.5, 1.5, 100.0]],
            [],
        ]
        mock_binance.return_value = mock_exchange

        fetcher = DataFetcher(proxy_url=None)
        df = fetcher.fetch_ccxt(
            "BTC-USDT",
            start_date="2024-01-01",
            end_date="2024-01-02",
            limit=1,
            exchange_id="binance",
        )

        self.assertEqual(len(df), 1)
        self.assertTrue(mock_exchange.session.trust_env)

    @patch("ccxt.okx")
    @patch("ccxt.binance")
    def test_fetch_ccxt_falls_back_to_next_exchange(self, mock_binance, mock_okx):
        failing_exchange = MagicMock()
        failing_exchange.session = MagicMock()
        failing_exchange.session.trust_env = False
        failing_exchange.fetch_ohlcv.side_effect = Exception("restricted")
        mock_binance.return_value = failing_exchange

        okx_exchange = MagicMock()
        okx_exchange.session = MagicMock()
        okx_exchange.session.trust_env = False
        okx_exchange.fetch_ohlcv.side_effect = [
            [[1704067200000, 1.0, 2.0, 0.5, 1.5, 100.0]],
            [],
        ]
        mock_okx.return_value = okx_exchange

        fetcher = DataFetcher(proxy_url=None)
        df = fetcher.fetch_ccxt(
            "BTC-USDT",
            start_date="2024-01-01",
            end_date="2024-01-02",
            limit=1,
        )

        self.assertEqual(len(df), 1)
        self.assertTrue(okx_exchange.session.trust_env)


if __name__ == "__main__":
    unittest.main()
