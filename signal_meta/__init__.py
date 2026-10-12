"""Signal meta layer: observation, outcome labels, expected-value ledger, adaptive regime model and the
versioned temporal labels / financing evidence they use.

These modules sit beside ``ops`` above ``core`` (see ``[tool.importlinter]`` in pyproject.toml): they may
import ``core`` but nothing in ``core`` imports them. ``core.market.temporal_data`` stays in ``core`` because the
market-data layer needs it.
"""
