import numpy as np
import pandas as pd

"""
动量/震荡类扩展指标：RSI、Stochastic、CCI、Williams %R、MFI（资金流量指标）。

MFI 本质是"成交量加权版 RSI"，需要 volume 列，因此放在动量类而非纯成交量类，
与 RSI 并列便于对比使用。
"""


class MomentumFactors:
    @staticmethod
    def calculate_all(df: pd.DataFrame) -> None:
        df["RSI_14"] = MomentumFactors.RSI(df["close"], 14)

        k, d = MomentumFactors.STOCH(df, 14, 3, 3)
        df["STOCH_K"] = k
        df["STOCH_D"] = d

        df["CCI_20"] = MomentumFactors.CCI(df, 20)
        df["WILLR_14"] = MomentumFactors.WILLIAMS_R(df, 14)

        if "volume" in df.columns:
            df["MFI_14"] = MomentumFactors.MFI(df, 14)

    @staticmethod
    def RSI(series: pd.Series, n: int = 14) -> pd.Series:
        """
        相对强弱指标（RSI），使用 Wilder 平滑。
        """
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)

        avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.where(avg_loss != 0, 100.0)
        rsi.iloc[: n - 1] = np.nan
        return rsi

    @staticmethod
    def STOCH(
        df: pd.DataFrame, k_period: int = 14, d_period: int = 3, smooth_k: int = 3
    ) -> tuple[pd.Series, pd.Series]:
        """
        随机震荡指标（Stochastic Oscillator）。

        - %K_raw = 100 * (close - lowest_low(n)) / (highest_high(n) - lowest_low(n))
        - %K = SMA(%K_raw, smooth_k)
        - %D = SMA(%K, d_period)
        """
        low_n = df["low"].rolling(window=k_period).min()
        high_n = df["high"].rolling(window=k_period).max()

        denom = (high_n - low_n).replace(0, np.nan)
        raw_k = 100 * (df["close"] - low_n) / denom

        k = raw_k.rolling(window=smooth_k).mean()
        d = k.rolling(window=d_period).mean()
        return k, d

    @staticmethod
    def CCI(df: pd.DataFrame, n: int = 20) -> pd.Series:
        """
        顺势指标（Commodity Channel Index）。

        CCI = (TP - SMA(TP, n)) / (0.015 * mean_abs_deviation(TP, n))
        TP（Typical Price）= (high + low + close) / 3
        """
        tp = (df["high"] + df["low"] + df["close"]) / 3
        sma_tp = tp.rolling(window=n).mean()
        mad = tp.rolling(window=n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
        cci = (tp - sma_tp) / (0.015 * mad.replace(0, np.nan))
        return cci

    @staticmethod
    def WILLIAMS_R(df: pd.DataFrame, n: int = 14) -> pd.Series:
        """
        威廉指标（Williams %R），取值范围 [-100, 0]。
        """
        high_n = df["high"].rolling(window=n).max()
        low_n = df["low"].rolling(window=n).min()
        denom = (high_n - low_n).replace(0, np.nan)
        return -100 * (high_n - df["close"]) / denom

    @staticmethod
    def MFI(df: pd.DataFrame, n: int = 14) -> pd.Series:
        """
        资金流量指标（Money Flow Index），成交量加权版 RSI，取值范围 [0, 100]。
        """
        tp = (df["high"] + df["low"] + df["close"]) / 3
        raw_money_flow = tp * df["volume"]

        tp_diff = tp.diff()
        positive_flow = raw_money_flow.where(tp_diff > 0, 0.0)
        negative_flow = raw_money_flow.where(tp_diff < 0, 0.0)

        positive_sum = positive_flow.rolling(window=n).sum()
        negative_sum = negative_flow.rolling(window=n).sum()

        money_ratio = positive_sum / negative_sum.replace(0, np.nan)
        mfi = 100 - (100 / (1 + money_ratio))
        mfi = mfi.where(negative_sum != 0, 100.0)
        return mfi
