"""Credential-gated, read-only exchange sandbox smoke test."""
import os, unittest
from core.live_broker import LiveBroker
from core.portfolio import Portfolio
@unittest.skipUnless(os.getenv("QUANT_SANDBOX_E2E") == "1" and os.getenv("EXCHANGE_API_KEY") and os.getenv("EXCHANGE_SECRET"), "sandbox credentials not enabled")
class ExchangeSandboxEndToEndTests(unittest.TestCase):
    def test_authenticated_sync_and_public_market_read(self):
        symbol = os.getenv("QUANT_SANDBOX_SYMBOL", "BTC/USDT")
        broker = LiveBroker(Portfolio(0.0), exchange_id=os.getenv("QUANT_SANDBOX_EXCHANGE", "binance"), sandbox=True, market_type="spot", require_market_metadata=True)
        self.assertIn(symbol, broker.exchange.load_markets())
        ticker = broker.exchange.fetch_ticker(symbol)
        self.assertTrue(ticker.get("timestamp") or ticker.get("datetime"))
        result = broker.sync()
        self.assertTrue(result.ok, result.error)
if __name__ == "__main__": unittest.main()
