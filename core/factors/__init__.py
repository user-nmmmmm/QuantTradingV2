"""
Factors（扩展指标库）

与 core.indicators.Indicators 完全独立、互不影响：
- core.indicators.Indicators 是现有策略/状态机依赖的最小指标集（SMA/ATR/ADX/BBANDS），
  维持原样以避免影响实盘/回测已验证的信号路径。
- 本包按类别拆分新增指标，供策略按需 opt-in 使用，不会自动挂载到现有 DataFrame。

分类：
- trend：趋势类（MACD 等，EMA/SMA 见 core.indicators）
- momentum：动量/震荡类（RSI/Stochastic/CCI/Williams %R/MFI）
- volatility：波动率类（Keltner Channel；ATR/BBANDS 见 core.indicators）
- volume：成交量类（OBV/VWAP/CMF/Volume Profile）
- support_resistance：支撑阻力类（Fibonacci/Pivot Points/摆动高低点）
- capital_flow：资金流类（资金费率/持仓量衍生特征，依赖外部数据拉取）
"""

import pandas as pd

from .trend import TrendFactors
from .momentum import MomentumFactors
from .volatility import VolatilityFactors
from .volume import VolumeFactors
from .support_resistance import SupportResistanceFactors
from .capital_flow import CapitalFlowFactors

__all__ = [
    "TrendFactors",
    "MomentumFactors",
    "VolatilityFactors",
    "VolumeFactors",
    "SupportResistanceFactors",
    "CapitalFlowFactors",
    "Factors",
]


class Factors:
    """
    扩展指标聚合入口。

    与 Indicators.calculate_all 不同，本方法返回新增列名列表，
    且不修改传入 df 的既有列，只新增列（原地挂载，便于策略直接读取）。
    """

    @staticmethod
    def calculate_all(df: pd.DataFrame) -> pd.DataFrame:
        TrendFactors.calculate_all(df)
        MomentumFactors.calculate_all(df)
        VolatilityFactors.calculate_all(df)
        VolumeFactors.calculate_all(df)
        SupportResistanceFactors.calculate_all(df)
        return df
