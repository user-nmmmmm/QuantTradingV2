import numpy as np
import pandas as pd

"""
支撑阻力类扩展指标：枢轴点（Pivot Points）、摆动高低点、斐波那契回撤。
"""


class SupportResistanceFactors:
    @staticmethod
    def calculate_all(df: pd.DataFrame) -> None:
        pivot, r1, r2, s1, s2 = SupportResistanceFactors.PIVOT_POINTS(df)
        df["PIVOT"] = pivot
        df["PIVOT_R1"] = r1
        df["PIVOT_R2"] = r2
        df["PIVOT_S1"] = s1
        df["PIVOT_S2"] = s2

        df["SWING_HIGH"] = SupportResistanceFactors.SWING_HIGH(df, 5)
        df["SWING_LOW"] = SupportResistanceFactors.SWING_LOW(df, 5)

    @staticmethod
    def PIVOT_POINTS(
        df: pd.DataFrame,
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        经典枢轴点（Classic Pivot Points），基于上一根 K 线的 H/L/C 计算，
        用作当前 K 线的支撑/阻力参考位。

        - PIVOT = (prev_high + prev_low + prev_close) / 3
        - R1 = 2*PIVOT - prev_low        S1 = 2*PIVOT - prev_high
        - R2 = PIVOT + (prev_high-prev_low)   S2 = PIVOT - (prev_high-prev_low)
        """
        prev_high = df["high"].shift(1)
        prev_low = df["low"].shift(1)
        prev_close = df["close"].shift(1)

        pivot = (prev_high + prev_low + prev_close) / 3
        r1 = 2 * pivot - prev_low
        s1 = 2 * pivot - prev_high
        r2 = pivot + (prev_high - prev_low)
        s2 = pivot - (prev_high - prev_low)
        return pivot, r1, r2, s1, s2

    @staticmethod
    def SWING_HIGH(df: pd.DataFrame, order: int = 5) -> pd.Series:
        """
        摆动高点：某根 K 线的 high 是其左右各 order 根范围内的最大值时标记为摆动高点，
        否则为 NaN（供策略识别潜在阻力位）。
        """
        high = df["high"]
        rolling_max = high.rolling(window=2 * order + 1, center=True).max()
        return high.where(high == rolling_max)

    @staticmethod
    def SWING_LOW(df: pd.DataFrame, order: int = 5) -> pd.Series:
        """
        摆动低点：某根 K 线的 low 是其左右各 order 根范围内的最小值时标记为摆动低点，
        否则为 NaN（供策略识别潜在支撑位）。
        """
        low = df["low"]
        rolling_min = low.rolling(window=2 * order + 1, center=True).min()
        return low.where(low == rolling_min)

    @staticmethod
    def FIBONACCI_RETRACEMENT(swing_high: float, swing_low: float) -> dict:
        """
        斐波那契回撤位（给定一段摆动区间的最高价/最低价，返回常用回撤价位）。

        levels: 0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0
        （0 对应 swing_high，1.0 对应 swing_low，即从高点向低点回撤）
        """
        if swing_high <= swing_low:
            raise ValueError("swing_high must be greater than swing_low")

        span = swing_high - swing_low
        ratios = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
        return {f"FIB_{ratio:.3f}": swing_high - ratio * span for ratio in ratios}
