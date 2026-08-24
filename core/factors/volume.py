import numpy as np
import pandas as pd

"""
成交量类扩展指标：OBV、VWAP、CMF（佳庆资金流量）、简化版成交量轮廓（Volume Profile）。
"""


class VolumeFactors:
    @staticmethod
    def calculate_all(df: pd.DataFrame) -> None:
        if "volume" not in df.columns:
            return
        df["OBV"] = VolumeFactors.OBV(df)
        df["VWAP"] = VolumeFactors.VWAP(df)
        df["CMF_20"] = VolumeFactors.CMF(df, 20)

        poc, vah, val = VolumeFactors.VOLUME_PROFILE(df, window=50, bins=20)
        df["VP_POC"] = poc
        df["VP_VAH"] = vah
        df["VP_VAL"] = val

    @staticmethod
    def OBV(df: pd.DataFrame) -> pd.Series:
        """
        能量潮（On-Balance Volume）。

        收盘上涨累加成交量，下跌累减成交量，持平不变。
        """
        direction = np.sign(df["close"].diff().fillna(0.0))
        signed_volume = direction * df["volume"]
        return signed_volume.cumsum()

    @staticmethod
    def VWAP(df: pd.DataFrame) -> pd.Series:
        """
        成交量加权均价（累计口径，从序列起点开始累积）。

        注：日内 VWAP 通常按交易日重置；此处提供累计版本，
        如需按日重置，调用方可在按日分组后逐日调用本方法。
        """
        tp = (df["high"] + df["low"] + df["close"]) / 3
        cum_vol = df["volume"].cumsum()
        cum_tp_vol = (tp * df["volume"]).cumsum()
        return cum_tp_vol / cum_vol.replace(0, np.nan)

    @staticmethod
    def CMF(df: pd.DataFrame, n: int = 20) -> pd.Series:
        """
        佳庆资金流量指标（Chaikin Money Flow）。

        CMF = sum(MFV, n) / sum(volume, n)
        MFV（Money Flow Volume）= [(close-low) - (high-close)] / (high-low) * volume
        """
        high_low_range = (df["high"] - df["low"]).replace(0, np.nan)
        mf_multiplier = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / high_low_range
        mf_volume = mf_multiplier * df["volume"]

        return mf_volume.rolling(window=n).sum() / df["volume"].rolling(window=n).sum().replace(
            0, np.nan
        )

    @staticmethod
    def VOLUME_PROFILE(
        df: pd.DataFrame, window: int = 50, bins: int = 20
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        简化版滚动成交量轮廓（基于 OHLCV，非逐笔数据的近似）。

        对每根 K 线，把其成交量均匀分摊到该根的 [low, high] 价格区间对应的分箱内，
        在滚动窗口内累加各分箱成交量，取：
        - POC（Point of Control）：成交量最大的分箱中点
        - VAH/VAL（Value Area High/Low）：以 POC 为中心累加分箱成交量占比达 70% 的价格区间上下界

        说明：这是基于 OHLCV 的近似估计，逐笔/tick 级别数据能得到更精确的轮廓。
        """
        n = len(df)
        poc = pd.Series(index=df.index, dtype=float)
        vah = pd.Series(index=df.index, dtype=float)
        val = pd.Series(index=df.index, dtype=float)

        highs = df["high"].to_numpy()
        lows = df["low"].to_numpy()
        volumes = df["volume"].to_numpy()

        for end in range(window - 1, n):
            start = end - window + 1
            window_high = highs[start : end + 1].max()
            window_low = lows[start : end + 1].min()
            if window_high <= window_low:
                continue

            edges = np.linspace(window_low, window_high, bins + 1)
            bin_volumes = np.zeros(bins)

            for i in range(start, end + 1):
                bar_high = highs[i]
                bar_low = lows[i]
                bar_volume = volumes[i]
                if bar_volume <= 0:
                    continue
                if bar_high <= bar_low:
                    idx = np.clip(np.searchsorted(edges, bar_low) - 1, 0, bins - 1)
                    bin_volumes[idx] += bar_volume
                    continue

                lo_idx = np.clip(np.searchsorted(edges, bar_low, side="right") - 1, 0, bins - 1)
                hi_idx = np.clip(np.searchsorted(edges, bar_high, side="right") - 1, 0, bins - 1)
                span = hi_idx - lo_idx + 1
                bin_volumes[lo_idx : hi_idx + 1] += bar_volume / span

            poc_idx = int(np.argmax(bin_volumes))
            poc_price = (edges[poc_idx] + edges[poc_idx + 1]) / 2
            poc.iloc[end] = poc_price

            total_volume = bin_volumes.sum()
            if total_volume <= 0:
                continue

            target = 0.7 * total_volume
            lo = hi = poc_idx
            covered = bin_volumes[poc_idx]
            while covered < target and (lo > 0 or hi < bins - 1):
                expand_lo = bin_volumes[lo - 1] if lo > 0 else -1
                expand_hi = bin_volumes[hi + 1] if hi < bins - 1 else -1
                if expand_hi >= expand_lo:
                    hi += 1
                    covered += bin_volumes[hi]
                else:
                    lo -= 1
                    covered += bin_volumes[lo]

            val.iloc[end] = edges[lo]
            vah.iloc[end] = edges[hi + 1]

        return poc, vah, val
