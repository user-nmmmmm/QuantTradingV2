import pandas as pd

"""
波动率类扩展指标：肯特纳通道（Keltner Channel）。

说明：
- ATR/布林带已在 core.indicators.Indicators 中实现，这里复用 ATR，避免重复定义。
"""

from core.indicators import Indicators


class VolatilityFactors:
    @staticmethod
    def calculate_all(df: pd.DataFrame) -> None:
        upper, middle, lower = VolatilityFactors.KELTNER(df, 20, 2.0)
        df["KC_UPPER"] = upper
        df["KC_MIDDLE"] = middle
        df["KC_LOWER"] = lower

    @staticmethod
    def KELTNER(
        df: pd.DataFrame, n: int = 20, atr_mult: float = 2.0
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        肯特纳通道（Keltner Channel）。

        - middle = EMA(close, n)
        - upper/lower = middle ± atr_mult * ATR(n)
        """
        middle = Indicators.EMA(df["close"], n)
        atr = Indicators.ATR(df, n)
        upper = middle + atr_mult * atr
        lower = middle - atr_mult * atr
        return upper, middle, lower
