import pandas as pd

"""
趋势类扩展指标（MACD）。

说明：
- SMA/EMA 已在 core.indicators.Indicators 中实现，这里直接复用，避免重复定义。
"""

from core.indicators import Indicators


class TrendFactors:
    @staticmethod
    def calculate_all(df: pd.DataFrame) -> None:
        macd_line, signal_line, hist = TrendFactors.MACD(df["close"])
        df["MACD_LINE"] = macd_line
        df["MACD_SIGNAL"] = signal_line
        df["MACD_HIST"] = hist

    @staticmethod
    def MACD(
        series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        MACD（异同移动平均线）。

        - MACD_LINE = EMA(fast) - EMA(slow)
        - MACD_SIGNAL = EMA(MACD_LINE, signal)
        - MACD_HIST = MACD_LINE - MACD_SIGNAL
        """
        ema_fast = Indicators.EMA(series, fast)
        ema_slow = Indicators.EMA(series, slow)
        macd_line = ema_fast - ema_slow
        signal_line = Indicators.EMA(macd_line, signal)
        hist = macd_line - signal_line
        return macd_line, signal_line, hist
