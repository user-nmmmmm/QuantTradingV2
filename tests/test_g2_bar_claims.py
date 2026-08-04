import os
import tempfile
import unittest

from core.state_store_v2 import StateStore


class TestBarClaimRecovery(unittest.TestCase):
    def test_processing_claim_recovered_only_after_lease_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, "state.db"))
            key = "exchange|account|BTC/USDT|1m|bar"
            self.assertTrue(store.claim_bar(key, "2026-08-02T12:00:00+00:00", 60))
            self.assertFalse(store.claim_bar(key, "2026-08-02T12:00:30+00:00", 60))
            self.assertTrue(store.claim_bar(key, "2026-08-02T12:01:01+00:00", 60))
            store.complete_bar(key, "2026-08-02T12:01:02+00:00")
            self.assertFalse(store.claim_bar(key, "2026-08-03T12:00:00+00:00", 60))
            store.close()


if __name__ == "__main__":
    unittest.main()
