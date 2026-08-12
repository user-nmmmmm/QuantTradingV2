import json
import tempfile
import unittest
from pathlib import Path
from tests.baseline_harness import ARTIFACTS, load_bundle, materialize
from tests.engine_baseline_harness import (
    DEFAULT_BARS,
    DEFAULT_SEED,
    DEFAULT_SYMBOLS,
    DEFAULT_WARMUP_PERIOD,
    SCHEMA_VERSION,
    build_synthetic_data_map,
    canonical_json,
    run_engine,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "backtest"
ENGINE_BASELINE_PATH = FIXTURE_DIR / "engine" / "engine_baseline_v1.json"

class TestBacktestFixedBaselines(unittest.TestCase):
    def bundles(self):
        paths = sorted(FIXTURE_DIR.glob("*.json"))
        self.assertEqual(len(paths), 3)
        return [(path, load_bundle(path)) for path in paths]

    def test_contract_and_frozen_metadata(self):
        required = {"start", "end", "seed", "symbols", "config", "data_summary", "data_sha256"}
        for path, bundle in self.bundles():
            with self.subTest(path=path.name):
                metadata = bundle["metadata"]
                self.assertEqual(bundle["schema_version"], "backtest-baseline-v1")
                self.assertTrue(required.issubset(metadata))
                self.assertEqual(metadata["symbols"], sorted(metadata["symbols"]))
                self.assertEqual(metadata["data_summary"]["rows"], len(bundle["data"]))
                self.assertEqual(set(bundle["artifacts"]), set(ARTIFACTS))

    def test_three_consecutive_runs_are_structurally_identical(self):
        for path, bundle in self.bundles():
            with self.subTest(path=path.name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                runs = [materialize(bundle, root / f"run-{number}") for number in range(1, 4)]
                normalized = [json.dumps(run, sort_keys=True, separators=(",", ":")) for run in runs]
                self.assertEqual(normalized[0], normalized[1])
                self.assertEqual(normalized[1], normalized[2])

    def test_hand_calculable_round_trip(self):
        bundle = load_bundle(FIXTURE_DIR / "hand_calculable.json")
        trade = bundle["artifacts"]["closed_trades"][0]
        gross = (trade["exit_price"] - trade["entry_price"]) * trade["qty"]
        net = gross - trade["commission"] - trade["slippage_cost"]
        self.assertAlmostEqual(trade["gross_pnl"], gross)
        self.assertAlmostEqual(trade["net_pnl"], net)
        self.assertAlmostEqual(bundle["artifacts"]["metrics"]["net_pnl"], net)
        self.assertAlmostEqual(bundle["artifacts"]["equity"][-1]["equity"], 10000 + net)

    def test_no_trade_fixture_is_really_empty(self):
        bundle = load_bundle(FIXTURE_DIR / "no_trades.json")
        for artifact in ("orders", "fills", "closed_trades"):
            self.assertEqual(bundle["artifacts"][artifact], [])
        self.assertEqual(bundle["artifacts"]["metrics"]["total_trades"], 0)

class TestBacktestEngineEquivalenceBaseline(unittest.TestCase):
    """Drives the real BacktestEngine end-to-end and compares it against a
    recorded golden run. Unlike TestBacktestFixedBaselines above, this
    actually exercises HistoricalMarketDataAdapter -> EventProcessor ->
    Router -> strategies -> Broker, so it's the regression guard for
    architecture-level changes (positional-index threading, incremental
    indicators, event-pipeline memory bounds, ...).

    Regenerate the fixture with `python -m tests.generate_engine_baseline`
    only after a reviewed, intentional behavior change.
    """

    def _load_baseline(self):
        with ENGINE_BASELINE_PATH.open(encoding="utf-8") as stream:
            return json.load(stream)

    def test_matches_recorded_baseline(self):
        bundle = self._load_baseline()
        self.assertEqual(bundle["schema_version"], SCHEMA_VERSION)
        self.assertEqual(bundle["metadata"]["seed"], DEFAULT_SEED)
        self.assertEqual(bundle["metadata"]["symbols"], list(DEFAULT_SYMBOLS))
        self.assertEqual(bundle["metadata"]["bars_per_symbol"], DEFAULT_BARS)
        self.assertEqual(bundle["metadata"]["warmup_period"], DEFAULT_WARMUP_PERIOD)
        self.assertGreater(bundle["artifacts"]["metrics"]["TotalTrades"], 0)

        data_map = build_synthetic_data_map(
            seed=bundle["metadata"]["seed"],
            symbols=bundle["metadata"]["symbols"],
            bars=bundle["metadata"]["bars_per_symbol"],
        )
        artifacts = run_engine(data_map, warmup_period=bundle["metadata"]["warmup_period"])

        self.assertEqual(canonical_json(artifacts), canonical_json(bundle["artifacts"]))

    def test_two_runs_in_same_process_are_identical(self):
        data_map = build_synthetic_data_map()
        first = run_engine(data_map, warmup_period=DEFAULT_WARMUP_PERIOD)
        second = run_engine(data_map, warmup_period=DEFAULT_WARMUP_PERIOD)
        self.assertEqual(canonical_json(first), canonical_json(second))


if __name__ == "__main__":
    unittest.main()
