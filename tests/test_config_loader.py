import logging
import os
import tempfile
import unittest

import yaml

from config.config import ConfigLoadError, ConfigLoader


class TestConfigLoader(unittest.TestCase):
    def test_missing_yaml_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "params.yaml")
            with self.assertRaisesRegex(ConfigLoadError, "not found"):
                ConfigLoader(config_path=missing)

    def test_empty_yaml_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = os.path.join(directory, "params.yaml")
            with open(empty, "w", encoding="utf-8"):
                pass
            with self.assertRaisesRegex(ConfigLoadError, "empty or not a valid mapping"):
                ConfigLoader(config_path=empty)

    def test_missing_required_key_raises_with_exact_path(self):
        source = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "params.yaml"
        )
        with open(source, "r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
        del payload["risk"]["risk_per_trade"]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "params.yaml")
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(payload, handle)
            with self.assertRaisesRegex(ConfigLoadError, "risk.risk_per_trade"):
                ConfigLoader(config_path=path)

    def test_normal_yaml_loads_correctly_and_logs_source(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "params.yaml"
        )
        with self.assertLogs("config.config", level=logging.INFO) as captured:
            loaded = ConfigLoader(config_path=path)

        self.assertEqual(loaded.get("execution")["commission_rate_taker"], 0.0005)
        rendered = "\n".join(captured.output)
        self.assertIn("taker=0.0005", rendered)
        self.assertIn("maker=0.0002", rendered)
        self.assertIn("max_leverage=3.0", rendered)
        self.assertIn("max_dd=0.20", rendered)
        self.assertIn("TREND_UP", rendered)


if __name__ == "__main__":
    unittest.main()
