import io
import logging
import os
import unittest
from unittest.mock import patch

from core.logger import SensitiveDataFilter
from run_live import build_parser


class TestG1Entrypoint(unittest.TestCase):
    def test_default_mode_is_sandbox(self):
        args = build_parser().parse_args([])
        self.assertFalse(args.live)
        self.assertIsNone(args.market_type)

    def test_live_and_sandbox_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["--live", "--sandbox"])

    def test_cli_has_no_plaintext_credential_options(self):
        parser = build_parser()
        option_names = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        self.assertNotIn("--api_key", option_names)
        self.assertNotIn("--secret", option_names)

    def test_r8_gray_release_flags_are_registered(self):
        args = build_parser().parse_args(
            [
                "--r8-evidence", "reports/r7_acceptance.json",
                "--rollback-snapshot", "reports/snapshot.json",
                "--r8-max-order-notional", "1000",
                "--r8-max-daily-risk", "0.02",
            ]
        )
        self.assertEqual(args.r8_evidence, "reports/r7_acceptance.json")
        self.assertEqual(args.rollback_snapshot, "reports/snapshot.json")
        self.assertEqual(args.r8_max_order_notional, 1000.0)
        self.assertEqual(args.r8_max_daily_risk, 0.02)


class TestSensitiveDataFilter(unittest.TestCase):
    def test_redacts_environment_credentials_and_headers(self):
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "request secret=%s Authorization:Bearer-token",
            ("top-secret",),
            None,
        )
        with patch.dict(os.environ, {"EXCHANGE_SECRET": "top-secret"}):
            SensitiveDataFilter().filter(record)
        rendered = record.getMessage()
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("Bearer-token", rendered)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
