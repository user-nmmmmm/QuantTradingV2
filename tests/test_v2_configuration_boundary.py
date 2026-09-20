from copy import deepcopy

import pytest

from composition.factory import build_strategy_registry
from config.config import config
from core.strategy_governance import GovernanceError, assert_live_admission


def test_v2_profile_needs_named_experiment_and_is_research_only():
    class Settings:
        def __init__(self, data):
            self.data = data

        def get(self, section, key=None):
            row = self.data.get(section)
            return row if key is None else (row or {}).get(key)

        def require(self, section, key=None):
            row = self.data[section]
            return row if key is None else row[key]

    data = deepcopy(config._config)
    data["routing"]["TREND_UP"] = "TrendPortfolioV2"
    data.setdefault("research", {}).pop("experiment_id", None)
    data["research"]["trend_portfolio_v2"] = {"asset_base_weight": .5}
    with pytest.raises(ValueError, match="experiment_id"):
        build_strategy_registry(Settings(data))
    data["research"]["experiment_id"] = "v2-boundary-test"
    assert "TrendPortfolioV2" in build_strategy_registry(Settings(data))
    data["strategy_governance"]["TrendPortfolioV2"] = "admitted"
    with pytest.raises(GovernanceError, match="research-only"):
        assert_live_admission(Settings(data), ["TrendPortfolioV2"])
