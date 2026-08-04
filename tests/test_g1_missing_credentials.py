import os
import sys
import unittest
from unittest.mock import patch

import run_live


class TestMissingCredentialsFailClosed(unittest.TestCase):
    def test_main_stops_before_broker_creation(self):
        clean_environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"EXCHANGE_API_KEY", "EXCHANGE_SECRET", "EXCHANGE_PASSWORD"}
        }
        with patch.dict(os.environ, clean_environment, clear=True), patch.object(
            sys, "argv", ["run_live.py"]
        ), patch("run_live.SafeLiveBroker") as broker:
            with self.assertRaises(SystemExit) as raised:
                run_live.main()
        self.assertEqual(raised.exception.code, 2)
        broker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
