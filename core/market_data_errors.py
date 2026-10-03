"""Failures of a refresh that still preserves successful symbol updates."""


class MarketDataRefreshError(RuntimeError):
    def __init__(self, failures):
        self.failures = dict(failures)
        super().__init__("market data refresh failed for: " + ", ".join(sorted(failures)))
