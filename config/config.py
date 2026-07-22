import yaml
import os
from typing import Dict, Any

class ConfigLoader:
    _instance = None
    _config = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigLoader, cls).__new__(cls)
            cls._instance._load_config()
        return cls._instance

    def _load_config(self):
        # Default path relative to this file
        base_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(base_dir, "params.yaml")
        
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f)
        else:
            # Fallback defaults if file missing
            self._config = {
                "execution": {
                    "commission_rate_taker": 0.001,
                    "commission_rate_maker": 0.0005,
                    "slippage_bps": 5,
                    "use_impact_cost": False
                },
                "risk": {
                    "max_leverage": 3.0,
                    "risk_per_trade": 0.01,
                    "max_drawdown_limit": 0.20,
                    "liquidity_limit_pct": 0.01,
                    "max_pos_size_pct": 0.20,
                },
                "state": {
                    "stability_period": 2,
                    "ma_fast": 20,
                    "ma_slow": 60,
                    "adx_period": 14,
                    "adx_threshold": 25,
                    "atr_period": 14,
                    "atr_pct_threshold": 0.05,
                },
                "routing": {
                    "TREND_UP": "TrendBreakout",
                    "TREND_DOWN": "TrendBreakdown",
                    "SIDEWAYS": "RangeMeanReversion",
                    "VOLATILE": "TrendBreakout"
                },
                "router": {
                    "cooldown_bars": 2,
                },
                "data": {
                    "check_quality": True
                }
            }

    def get(self, section: str, key: str = None) -> Any:
        if self._config is None:
            self._load_config()
            
        if section not in self._config:
            return None
            
        if key:
            return self._config[section].get(key)
        return self._config[section]

# Global instance for easy access
config = ConfigLoader()
