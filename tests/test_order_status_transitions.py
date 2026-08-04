import unittest

from core.domain import OrderStatus
from core.orders import validate_transition


class TestOrderStatusTransitions(unittest.TestCase):
    def test_unknown_can_transition_to_terminal_states(self):
        targets = (
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED,
        )
        for target in targets:
            with self.subTest(target=target):
                validate_transition("unknown", target)

    def test_unknown_cannot_transition_to_submitting(self):
        with self.assertRaises(ValueError):
            validate_transition("unknown", OrderStatus.SUBMITTING)

    def test_canceled_allows_late_fill_race(self):
        validate_transition("canceled", OrderStatus.FILLED)


if __name__ == "__main__":
    unittest.main()
