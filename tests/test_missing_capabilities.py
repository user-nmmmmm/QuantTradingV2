import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from analysis.validation import ValidationConfig, validate_parameter_candidates
from core.cost_model import CostModel
from core.gray_release import GrayReleasePolicy
from core.live_safety import StartupSafetyPolicy
from core.metric_result import MetricResult
from core.supervisor import RestartPolicy, supervise
from core.system_factory import build_strategy_registry
from core.portfolio import Portfolio
from strategies.statistical_arbitrage import PairsTradingModel
from strategies.volatility import VolatilityReversionStrategy


class MissingCapabilityAcceptanceTests(unittest.TestCase):
    def test_metric_result_distinguishes_zero_from_unavailable(self):
        zero = MetricResult("return", 0.0, sample_size=10).to_dict()
        missing = MetricResult("sharpe", None, "undefined", reason="zero variance").to_dict()
        self.assertEqual(zero["value"], 0.0)
        self.assertIsNone(missing["value"])
        self.assertIn("status", MetricResult.json_schema()["properties"])

    def test_cost_model_marks_unmodeled_costs(self):
        costs = CostModel(0.001, 0.002).calculate(quantity=2, price=100)
        self.assertAlmostEqual(costs.modeled_total, 0.6)
        self.assertEqual(costs.funding_status, "not_modeled")
        self.assertIsNone(costs.funding)

    def test_oos_selection_uses_training_only(self):
        index = pd.date_range("2024-01-01", periods=300)
        candidates = {
            "stable": pd.Series([0.01] * 300, index=index),
            "overfit": pd.Series([0.02] * 210 + [-0.10] * 90, index=index),
        }
        result = validate_parameter_candidates(
            candidates, p_values=[0.01, 0.20],
            config=ValidationConfig(bootstrap_samples=20, monte_carlo_samples=20),
        )
        self.assertEqual(result["selected_on"], "train_only")
        self.assertEqual(result["selected_candidate"], "overfit")
        self.assertLess(result["oos_mean_return"], 0)

    def test_gray_release_requires_r7_permissions_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, snapshot = root / "r7.json", root / "state.db"
            evidence.write_text(json.dumps({"passed": True}), encoding="utf-8")
            snapshot.write_bytes(b"snapshot")
            startup = StartupSafetyPolicy(False, "binance", "spot", ("BTC/USDT",), ("binance",), ("spot",), ("BTC/USDT",), "USDT", 10, 20)
            exchange = MagicMock()
            exchange.fetch_api_permissions.return_value = {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}
            policy = GrayReleasePolicy("binance", "BTC/USDT", 10, 20, str(evidence), str(snapshot))
            with patch.dict("os.environ", {"QUANT_R8_APPROVED": "approved"}):
                policy.validate(startup, exchange)

    def test_shared_hard_stop(self):
        strategy = VolatilityReversionStrategy()
        portfolio = Portfolio(1000)
        portfolio.positions["X"] = {"qty": 1.0, "avg_price": 100.0}
        strategy.context["X"] = {"stop_loss": 95.0}
        frame = pd.DataFrame({"low": [94.0], "high": [101.0], "close": [96.0]})
        self.assertEqual(strategy.hard_stop_exit("X", 0, frame, portfolio)["reason"], "hard_stop")

    def test_new_alpha_capabilities(self):
        self.assertIn("VolatilityReversion", build_strategy_registry())
        left = pd.Series(range(100, 180), dtype=float)
        right = left * 0.5
        self.assertIn(PairsTradingModel(window=60).signal(left, right).action, {"hold", "exit", "short_left_long_right", "long_left_short_right"})

    def test_supervisor_restarts_with_bounded_backoff(self):
        results = iter([MagicMock(returncode=1), MagicMock(returncode=0)])
        delays = []
        clock = iter([0, 1, 2, 3])
        value = supervise(["worker"], RestartPolicy(max_restarts=2), runner=lambda *a, **k: next(results), sleep=delays.append, monotonic=clock.__next__)
        self.assertEqual(value, 0)
        self.assertEqual(delays, [1.0])


if __name__ == "__main__":
    unittest.main()
