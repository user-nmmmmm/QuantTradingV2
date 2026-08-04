import unittest
from unittest.mock import MagicMock

from core.retry import with_retry


class InsufficientFundsError(Exception):
    pass


class TestRetry(unittest.TestCase):
    def test_retries_on_ambiguous_error_then_succeeds(self):
        fn = MagicMock(side_effect=[TimeoutError("slow"), TimeoutError("slow"), "ok"])
        sleep = MagicMock()

        result = with_retry(fn, max_attempts=3, base_delay=0.5, sleep_fn=sleep)

        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0])

    def test_does_not_retry_non_ambiguous_error(self):
        fn = MagicMock(side_effect=InsufficientFundsError("insufficient balance"))

        with self.assertRaises(InsufficientFundsError):
            with_retry(fn, max_attempts=3, sleep_fn=MagicMock())

        fn.assert_called_once_with()

    def test_raises_after_max_attempts(self):
        fn = MagicMock(side_effect=TimeoutError("slow"))

        with self.assertRaises(TimeoutError):
            with_retry(fn, max_attempts=3, sleep_fn=MagicMock())

        self.assertEqual(fn.call_count, 3)


if __name__ == "__main__":
    unittest.main()
