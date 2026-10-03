"""Explicit policy for small synthetic engineering fixtures, never production."""
from copy import deepcopy

import pytest

from config.config import config


@pytest.fixture(scope='module', autouse=True)
def synthetic_health_policy():
    # These modules test routing/reporting/reproducibility on one or two fake
    # symbols. Health recovery itself is tested with its registered policy in
    # test_strategy_p0_p1 and test_strategy_remediation.
    prior = deepcopy(config._config)
    config._config = deepcopy(prior)
    config._config['strategy_health']['probation_min_distinct_symbols'] = 1
    try:
        yield
    finally:
        config._config = prior
