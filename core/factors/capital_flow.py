import numpy as np
import pandas as pd

"""
资金流类扩展指标：基于资金费率（funding rate）/持仓量（open interest）的衍生特征。

与其他 factors 模块不同，本模块的输入不是 OHLCV，而是独立拉取的资金费率/持仓量时间序列
（见 DataFetcher.fetch_funding_rate_history / fetch_open_interest_history）。
这里只做特征计算，不做数据拉取，方便离线测试与解耦。
"""


class CapitalFlowFactors:
    @staticmethod
    def funding_rate_zscore(funding_rate: pd.Series, window: int = 30) -> pd.Series:
        """
        资金费率滚动 z-score，用于识别费率极端（多空拥挤，潜在反转/挤仓信号）。
        """
        mean = funding_rate.rolling(window=window).mean()
        std = funding_rate.rolling(window=window).std()
        return (funding_rate - mean) / std.replace(0, np.nan)

    @staticmethod
    def funding_rate_cumulative(funding_rate: pd.Series, window: int = 30) -> pd.Series:
        """
        滚动窗口内累计资金费率（近似持有合约多头/空头的累计资金成本）。
        """
        return funding_rate.rolling(window=window).sum()

    @staticmethod
    def open_interest_change_pct(open_interest: pd.Series, window: int = 1) -> pd.Series:
        """
        持仓量变化率：正值表示新增持仓（趋势可能延续），
        价格与持仓量背离（价涨仓减/价跌仓增）常作为潜在反转的辅助信号。
        """
        return open_interest.pct_change(periods=window)

    @staticmethod
    def price_oi_divergence(close: pd.Series, open_interest: pd.Series, window: int = 1) -> pd.Series:
        """
        价格变化方向与持仓量变化方向的背离标记：
        +1 = 同向（价涨仓增 / 价跌仓减，趋势健康）
        -1 = 背离（价涨仓减 / 价跌仓增，趋势可能减弱）
         0 = 其中一方持平
        """
        price_dir = np.sign(close.pct_change(periods=window))
        oi_dir = np.sign(open_interest.pct_change(periods=window))
        return (price_dir * oi_dir).astype(float)
