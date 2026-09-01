import tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock
import pandas as pd
from backtest.reporting import ReportGenerator
from core.broker import Broker
from core.portfolio import Portfolio
from core.runtime import EventProcessor, MarketDataSlice
from live_trading.engine import LiveTradingEngine
class P3CompletionTests(unittest.TestCase):
    def test_runtime_values_once_and_reuses_prices(self):
        portfolio = MagicMock(cash=1000.0); portfolio.get_total_value.return_value = 1000.0
        risk = MagicMock(); risk.check_circuit_breaker.return_value = False
        processor = EventProcessor(portfolio=portfolio, execution=MagicMock(), risk_manager=risk, state_machine=MagicMock(), router=MagicMock(), allocator=MagicMock())
        result = processor.process(MarketDataSlice(pd.Timestamp("2026-01-01T00:00:00Z"), {}, {}), execute_market_event=False)
        portfolio.get_total_value.assert_called_once_with(processor.last_prices)
        self.assertIs(result.prices, processor.last_prices)
    def test_day_order_caches_submission_date(self):
        order = Broker(Portfolio(1000.0)).submit_order("TEST/USDT", "buy", 1.0, timestamp=pd.Timestamp("2026-01-01T00:00:00Z"))
        self.assertEqual(order.submitted_date.isoformat(), "2026-01-01")
    def test_metrics_only_writes_no_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "metrics"
            curve = pd.DataFrame({"equity": [100.0, 101.0], "cash": [100.0, 101.0]}, index=pd.date_range("2026-01-01", periods=2, tz="UTC"))
            self.assertIn("TotalReturn", ReportGenerator(str(output)).generate([], curve, metrics_only=True))
            self.assertEqual(list(output.iterdir()), [])
    def test_fifo_and_dead_code_sources(self):
        root = Path(__file__).parents[1]
        reporting = (root / "backtest" / "reporting" / "trades.py").read_text(
            encoding="utf-8"
        )
        engine = (root / "backtest" / "engine.py").read_text(encoding="utf-8")
        self.assertIn("itertuples(", reporting); self.assertNotIn("iterrows(", reporting)
        self.assertNotIn("pop(0)", reporting); self.assertNotIn("_looks_daily_or_slower", engine)
    def test_unknown_query_cache(self):
        engine = object.__new__(LiveTradingEngine); engine.broker = MagicMock()
        engine.broker.has_unresolved_unknown.side_effect = [False, True]; engine._unresolved_unknown_cache = None
        self.assertFalse(engine._has_unresolved_unknown()); self.assertFalse(engine._has_unresolved_unknown())
        self.assertEqual(engine.broker.has_unresolved_unknown.call_count, 1)
        self.assertTrue(engine._has_unresolved_unknown(refresh=True))
if __name__ == "__main__": unittest.main()
