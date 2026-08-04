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
