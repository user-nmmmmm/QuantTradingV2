import pandas as pd
import numpy as np

"""
Indicators（指标库）模块

本模块提供回测系统用到的基础技术指标，并约定：
- 输入：价格序列或含 OHLCV 的 DataFrame（列名小写 open/high/low/close/volume）
- 输出：与输入索引等长的 pd.Series（或三元组），并在 DataFrame 上原地挂载列
- NaN 处理：多数滚动指标在前 n-1 根会出现 NaN；为兼容测试与避免误用，
  ATR/ADX 这里显式将前 n-1 根设为 NaN。

指标清单（当前项目使用）：
- 趋势：SMA/EMA
- 波动：ATR
- 强度：ADX
- 区间：布林带 BBANDS
"""

class Indicators:
    """
    基础指标实现模块。
    """
    
    @staticmethod
    def calculate_all(df: pd.DataFrame):
        """
        Calculate all necessary indicators and add them to the DataFrame in-place.

        说明：
        - 该方法会直接修改传入的 df（in-place），便于下游（状态机、策略）直接读取列
        - 若 df 缺失某些列（例如 close），上层应先通过 DataHandler.validate 标准化
        """
        # Trend Indicators
        df['SMA_10'] = Indicators.SMA(df['close'], 10)
        df['SMA_30'] = Indicators.SMA(df['close'], 30)
        df['SMA_120'] = Indicators.SMA(df['close'], 120)
        
        # Volatility / Stop Loss
        df['ATR_14'] = Indicators.ATR(df, 14)
        
        # Range Indicators
        upper, middle, lower = Indicators.BBANDS(df['close'], 20, 2)
        df['BB_UPPER'] = upper
        df['BB_MIDDLE'] = middle
        df['BB_LOWER'] = lower
        
        # Strength
        df['ADX_14'] = Indicators.ADX(df, 14)

    @staticmethod
    def SMA(series: pd.Series, n: int) -> pd.Series:
        """
        简单移动平均（Simple Moving Average）。

        返回：
        - 与 series 等长的滚动均值序列；前 n-1 根为 NaN
        """
        return series.rolling(window=n).mean()

    @staticmethod
    def EMA(series: pd.Series, n: int) -> pd.Series:
        """
        指数移动平均（Exponential Moving Average）。

        说明：
        - adjust=False 更符合交易软件常见实现（递推形式）
        """
        return series.ewm(span=n, adjust=False).mean()

    @staticmethod
    def ATR(df: pd.DataFrame, n: int = 14) -> pd.Series:
        """
        平均真实波幅 (Average True Range)
        TR = Max(High-Low, Abs(High-PreClose), Abs(Low-PreClose))
        ATR = SMA(TR, n) (Usually Wilder's Smoothing is used, but SMA/EMA is requested/acceptable)
        这里使用 Wilder's Smoothing (alpha=1/n) 的 EMA 来逼近标准 ATR

        实现细节：
        - 先计算 TR（True Range）
        - 再用 Wilder 平滑（ewm(alpha=1/n)）得到 ATR
        - 为避免“第一根就有值”误导使用，这里将前 n-1 根置为 NaN
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        # 计算 TR
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # 使用 Wilder's Smoothing: ewm(alpha=1/n, adjust=False)
        atr = tr.ewm(alpha=1/n, adjust=False).mean()
        # 或者为了简单匹配 "NaN 只允许在前 n 根出现"，如果使用 rolling mean，前面会有 n-1 个 NaN
        # 如果使用 ewm，第一个值就有，但可能不准。
        # 为了符合 "NaN 只允许在前 n 根出现" 且准确性，通常前 n 个是不准的。
        # 标准 ATR 计算通常前 14 天用 SMA，后面用 Wilder's。
        # 这里简化使用 Wilder's EMA 全程，第一个值为 TR。
        
        # 强制前 n-1 个设为 NaN 以符合验收标准 "NaN 只允许在前 n 根出现" (其实是前 n-1 个是 NaN, 第 n 个有值)
        # 但 ewm 默认从第0个就有值。
        # 如果用户严格要求 "NaN 只允许在前 n 根出现" (implied: first n are NaN or unreliable)
        # 让我们把前 n-1 个设为 NaN
        atr.iloc[:n-1] = np.nan
        return atr

    @staticmethod
    def ADX(df: pd.DataFrame, n: int = 14) -> pd.Series:
        """
        平均趋向指标 (Average Directional Index)

        实现步骤（简化版）：
        1) 计算 TR、+DM、-DM
        2) 对 TR/+DM/-DM 做 Wilder 平滑（ewm(alpha=1/n)）
        3) 计算 +DI、-DI
        4) 计算 DX 并平滑得到 ADX

        说明：
        - 标准 ADX 的初始化阶段通常更复杂（先 SMA 再 Wilder），这里采用一致的 EWM 近似
        - 同样将前 n-1 根置为 NaN，避免早期不稳定值干扰策略
        """
        high = df['high']
        low = df['low']
        close = df['close']
        
        # 1. Calculate TR
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        
        # 2. Calculate +DM, -DM
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        
        plus_dm = pd.Series(plus_dm, index=df.index)
        minus_dm = pd.Series(minus_dm, index=df.index)
        
        # 3. Smooth TR, +DM, -DM using Wilder's smoothing (alpha=1/n)
        # 初始值通常是 SMA，这里为了连贯性直接用 EWM
        tr_smooth = tr.ewm(alpha=1/n, adjust=False).mean()
        plus_dm_smooth = plus_dm.ewm(alpha=1/n, adjust=False).mean()
        minus_dm_smooth = minus_dm.ewm(alpha=1/n, adjust=False).mean()
        
        # 4. Calculate +DI, -DI
        # 避免除以 0
        plus_di = 100 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
        minus_di = 100 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))
        
        # 5. Calculate DX
        dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan))
        
        # 6. Calculate ADX = SMA(DX, n) (Standard is Wilder's, but user prompt says "ADX(df, n=14)")
        # 通常 ADX 本身也是平滑过的。
        adx = dx.ewm(alpha=1/n, adjust=False).mean()
        
        # 设置前 n-1 为 NaN
        # ADX 需要更多数据才能稳定，但为了满足接口要求：
        adx.iloc[:n-1] = np.nan
        
        return adx

    @staticmethod
    def BBANDS(series: pd.Series, n: int = 20, k: int = 2) -> tuple[pd.Series, pd.Series, pd.Series]:
        """
        布林带 (Bollinger Bands)
        Returns: (upper, middle, lower)

        计算：
        - middle = SMA(series, n)
        - upper/lower = middle ± k * rolling_std(series, n)
        """
        middle = series.rolling(window=n).mean()
        std = series.rolling(window=n).std()
        
        upper = middle + k * std
        lower = middle - k * std
        
        return upper, middle, lower


Indicator = Indicators
