"""Admission and artifact isolation for bounded web walk-forward research."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.backtest_jobs import BacktestJobs, PROJECT_ROOT
from dashboard.robust_research import DEFAULTS, RobustResearch, window_geometry
from dashboard.robust_worker import _json_value, utc_timestamp
from dashboard.strategy_presets import load_base_config


class RobustResearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.jobs = BacktestJobs(self.root / "data", self.root / "reports")
        self.research = RobustResearch(self.jobs)
        self.payload = {"source": "synthetic", "symbols": ["BTC/USDT", "ETH/USDT", "BNB/USDT"],
                        "start": "2025-01-01", "end": "2025-12-31", "capital": 10000,
                        "slippage_bps": 5, "seed": 42, **copy.deepcopy(DEFAULTS)}

    def tearDown(self):
        self.jobs.close()
        self.directory.cleanup()

    def test_options_use_shared_csrf_and_limits(self):
        options = self.research.options()
        self.assertEqual(options["csrf_token"], self.jobs.csrf_token)
        self.assertEqual(options["limits"]["max_candidates"], 6)
        self.assertEqual(options["limits"]["max_windows"], 4)

    def test_validated_strategy_is_explicit_and_input_unmodified(self):
        validated = self.research.validate(self.payload)
        self.assertEqual(validated["strategy"]["family"], "trend_breakout")
        self.assertEqual(validated["strategy"]["parameters"]["entry_window"], 20)
        self.assertNotIn("strategy", self.payload)

    def test_grid_and_window_budget(self):
        invalid = [{"entry_windows": [20, 30, 50, 60]}, {"entry_windows": [20]},
                   {"exit_windows": [5, 10, 15]}, {"entry_windows": [20, 20, 50]},
                   {"exit_windows": [20]}, {"windows": 5}, {"windows": True},
                   {"purge_bars": 0}, {"train_bars": 500}, {"test_bars": 1},
                   {"entry_windows": [True, 30, 50]}, {"entry_windows": [20., 30, 50]},
                   {"selection_metric": "test_return"}]
        for overrides in invalid:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.research.validate({**self.payload, **overrides})

    def test_unknown_fields_and_arbitrary_strategy_rejected(self):
        for overrides in ({"script": "arbitrary.py"}, {"strategy": {"family": "mean_reversion"}}, {"output_dir": "../outside"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.research.validate({**self.payload, **overrides})

    def test_insufficient_history_rejected_before_queue(self):
        with self.assertRaisesRegex(ValueError, "daily bars"):
            self.research.validate({**self.payload, "end": "2025-04-01"})

    def test_local_source_uses_existing_strict_data_validation(self):
        with self.assertRaises(ValueError):
            self.research.validate({**self.payload, "source": "local"})

    def test_window_geometry_bounds_actual_engine_windows(self):
        from analysis.research_validation import walk_forward_splits
        configuration, _ = load_base_config(PROJECT_ROOT / "config" / "params.yaml")
        for count in range(1, 5):
            payload = {**self.payload, "windows": count}
            geometry = window_geometry(payload, configuration)
            splits = walk_forward_splits(geometry["required_bars"], train_size=payload["train_bars"],
                validation_size=payload["validation_bars"], test_size=payload["test_bars"],
                purge_size=payload["purge_bars"], step=geometry["step_bars"])
            evaluated = [row for row in splits if row["train_start"] >= geometry["warmup_bars"]]
            self.assertEqual(len(evaluated), count)
            self.assertTrue(all(row["test_start"] - row["validation_end"] == 5 for row in evaluated))
            self.assertTrue(all(left["test_end"] <= right["test_start"] for left, right in zip(evaluated, evaluated[1:])))

    def test_submit_uses_shared_scheduler(self):
        with patch.object(self.jobs, "submit_task", return_value={"id": "queued"}) as submit:
            self.assertEqual(self.research.submit(self.payload), {"id": "queued"})
        self.assertEqual(submit.call_args.args[0], "robust")
        self.assertEqual(submit.call_args.args[1]["strategy"]["family"], "trend_breakout")

    def _save_result(self, *, status="succeeded", kind="robust", content=None):
        identifier = "research_20261003_123456_aabbccdd"
        self.jobs.store.save_job({"id": identifier, "kind": kind, "status": status, "logs": []})
        directory = self.jobs.reports_dir / identifier
        directory.mkdir()
        (directory / "result.json").write_text(json.dumps(content or {"id": identifier, "windows": []}), encoding="utf-8")
        return identifier

    def test_result_reopens_from_persistent_store(self):
        identifier = self._save_result()
        self.assertEqual(self.research.result(identifier)["id"], identifier)

    def test_result_rejects_incomplete_job(self):
        identifier = self._save_result(status="failed")
        with self.assertRaises(FileNotFoundError):
            self.research.result(identifier)

    def test_result_rejects_wrong_kind_or_identifier(self):
        identifier = self._save_result(kind="backtest")
        with self.assertRaises(FileNotFoundError):
            self.research.result(identifier)
        for invalid in ("../result", "research_bad", None, {"id": identifier}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.research.result(invalid)

    def test_result_rejects_identity_mismatch_and_nonfinite_json(self):
        identifier = self._save_result(content={"id": "mismatch"})
        with self.assertRaises(ValueError):
            self.research.result(identifier)
        path = self.jobs.reports_dir / identifier / "result.json"
        path.write_text('{"id": "' + identifier + '", "return": NaN}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.research.result(identifier)

    def test_command_uses_fixed_module_and_snapshot_hash(self):
        snapshot = self.root / "config.yaml"
        snapshot.write_text("a: 1", encoding="utf-8")
        with patch.object(self.jobs.store, "get", return_value={"config_sha256": "registered-digest"}):
            command = self.research._command("research_id", self.payload, self.root / "output", snapshot)
        self.assertEqual(command[1:4], ["-u", "-m", "dashboard.robust_worker"])
        self.assertIn("--config-sha256", command)
        self.assertEqual(command[command.index("--config-sha256") + 1], "registered-digest")
        self.assertEqual(json.loads(command[command.index("--parameters") + 1]), self.payload)

    def test_nonfinite_statistics_export_as_unavailable(self):
        self.assertEqual(_json_value({"values": [float("inf"), float("nan"), .2], "status": "insufficient"}),
                         {"values": [None, None, .2], "status": "insufficient"})

    def test_worker_timestamps_keep_utc_daily_boundaries(self):
        self.assertEqual(utc_timestamp("2026-08-27 00:00:00"), "2026-08-27T00:00:00Z")
        self.assertEqual(utc_timestamp("2026-08-27T08:00:00+08:00"), "2026-08-27T00:00:00Z")
        self.assertEqual(utc_timestamp("2026-08-27T00:00:00Z"), "2026-08-27T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
