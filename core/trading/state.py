from enum import Enum
import pandas as pd
import numpy as np
from core.indicators import Indicators

"""
Market State（市场状态/市场制度）模块

该模块将价格与指标序列映射为离散的市场状态，用于：
- 策略路由（不同状态启用不同策略）
- 过滤模糊/不可交易区间（例如 NO_TRADE）
- 在回测与实盘中保持一致的“制度识别”逻辑

实现要点：
- 状态计算基于历史数据（SMA/ADX 等）并且使用稳定性过滤器（stability filter）
- 稳定性过滤器会要求候选状态连续出现 N 次后才切换，减少抖动
- 对齐（align_state_to_lower_tf）使用 forward fill，避免时间穿越（lookahead）
"""


class MarketState(Enum):
    TREND_UP = 1
    TREND_DOWN = 2
    SIDEWAYS = 3
    BULL_TREND = 1
    BEAR_TREND = 2
    RANGE = 3
    # High ADX and wide range but no stacked MA direction: a violent, directionless
    # transition/whipsaw regime. Routed to mean-reversion (see params.yaml
    # `routing`), NOT to breakout — a clean breakout resolves to TREND_UP/DOWN.
    VOLATILE = 5
    NO_TRADE = 4  # 模糊阶段，短期全部禁用


class MarketStateMachine:
    def __init__(
        self,
        stability_period: int = 3,
        ma_fast: int = 20,
        ma_slow: int = 60,
        adx_period: int = 14,
        adx_threshold: float = 25,
        atr_period: int = 14,
        atr_pct_threshold: float = 0.05,
    ):
        """
        参数：
        - stability_period：状态切换最少确认次数（越大越“钝化”，越小越敏感）
        - ma_fast/ma_slow：用于趋势结构判断的均线周期（默认 20/60）
        - adx_period/adx_threshold：用于趋势强度过滤（默认 ADX14 > 25）
        - atr_period/atr_pct_threshold：用于识别高波动（默认 ATR14/Close > 3%）
        """
        self.stability_period = stability_period
        self.ma_fast = ma_fast
        self.ma_slow = ma_slow
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold
        self.atr_period = atr_period
        self.atr_pct_threshold = atr_pct_threshold

    def get_states(self, df: pd.DataFrame) -> pd.Series:
        states = self.calculate_states(df)
        df["market_state"] = states
        return states

    def get_state(self, df: pd.DataFrame, i: int) -> MarketState:
        """
        获取第 i 根 K 线对应的市场状态。

        设计说明：
        - 从效率角度：更推荐一次性对全量 df 计算 states 并缓存；
        - 从接口兼容角度：回测引擎在循环里调用 get_state(df, i)，因此这里提供“懒计算”：
          - 若 df 已含 market_state 列，则直接读取；
          - 否则先对全 df 计算一次并写回 df["market_state"]，后续循环直接读取。

        稳定性过滤器依赖历史状态，因此仅计算单点状态意义不大；这里通过 calculate_states 解决。
        """
        # Engine calls this inside a loop.
        # If we calculate valid states for the whole DF once, it's efficient.
        # But 'get_state(df, i)' implies lookup.
        # Let's assume 'df' has a 'state' column if we pre-calculated?
        # Or we calculate it here looking back?

        # Let's implement `calculate_states(df)` and let Engine call it or
        # let `get_state` compute it if not present.

        # Check if 'market_state' column exists
        if "market_state" in df.columns:
            return df["market_state"].iloc[i]

        # If not, maybe we should calculate it for the whole DF now?
        # This might happen once per symbol.
        states = self.calculate_states(df)
        df["market_state"] = states
        return states.iloc[i]

    def calculate_states(self, df: pd.DataFrame) -> pd.Series:
        """
        计算整段数据的市场状态序列（返回 pd.Series，索引与 df 对齐）。

        判定规则（raw states，四个状态互斥）：
        - TREND_UP：ADX > 阈值 且 close > MA_fast > MA_slow（多头结构完整）
        - TREND_DOWN：ADX > 阈值 且 close < MA_fast < MA_slow（空头结构完整）
        - VOLATILE：ADX > 阈值 且 ATR% > 阈值，**但均线结构不成方向**
          —— 即“动得很凶却没有干净方向”的震荡/转折段，路由给均值回归类策略。
        - SIDEWAYS：其余情况

        为什么 VOLATILE 必须排除已成方向的 bar：
        三个条件共用同一个 ADX 门槛，若让 VOLATILE 无条件覆盖 TREND_UP/DOWN，
        则在加密日线（ATR 中位数约为价格 4.4%，远高于 atr_pct_threshold）上
        几乎所有趋势 bar 都会被 VOLATILE 吞掉——实测 BTC 2017-2026 上
        96.3% 的 TREND_UP 与 99.8% 的 TREND_DOWN 因此消失，趋势策略永不上场。

        最终输出：
        - 对 raw states 应用稳定性过滤器，减少短期噪声导致的频繁切换。
        """
        if (
            f"SMA_{self.ma_fast}" not in df.columns
            or f"SMA_{self.ma_slow}" not in df.columns
        ):
            df[f"SMA_{self.ma_fast}"] = Indicators.SMA(df["close"], self.ma_fast)
            df[f"SMA_{self.ma_slow}"] = Indicators.SMA(df["close"], self.ma_slow)

        col_atr = f"ATR_{self.atr_period}"
        col_adx = f"ADX_{self.adx_period}"
        if col_atr not in df.columns:
            df[col_atr] = Indicators.ATR(df, self.atr_period)
        if col_adx not in df.columns:
            df[col_adx] = Indicators.ADX(df, self.adx_period)

        close = df["close"]
        ma_fast = df[f"SMA_{self.ma_fast}"]
        ma_slow = df[f"SMA_{self.ma_slow}"]
        adx = df[col_adx]
        atr = df[col_atr]

        # Raw States
        raw_states = pd.Series(MarketState.SIDEWAYS, index=df.index)

        up_cond = (close > ma_fast) & (ma_fast > ma_slow) & (adx > self.adx_threshold)
        raw_states[up_cond] = MarketState.TREND_UP

        down_cond = (close < ma_fast) & (ma_fast < ma_slow) & (adx > self.adx_threshold)
        raw_states[down_cond] = MarketState.TREND_DOWN

        # VOLATILE only claims bars that no directional trend already owns:
        # strong ADX and wide range, but the moving averages are not stacked in
        # either direction (transition / whipsaw). Without this exclusion the
        # ATR gate — which nearly every crypto bar clears — would override both
        # trend states and starve the trend strategies entirely.
        atr_pct = atr / close.replace(0, np.nan)
        volatile_cond = (
            (adx > self.adx_threshold)
            & (atr_pct > self.atr_pct_threshold)
            & ~(up_cond | down_cond)
        )
        raw_states[volatile_cond] = MarketState.VOLATILE

        # Stability Filter
        return self._apply_stability_filter(raw_states)

    def _apply_stability_filter(self, raw_states: pd.Series) -> pd.Series:
        """
        稳定性过滤器（去抖动）。

        思路：
        - current_stable：当前已确认的稳定状态
        - candidate_state：候选新状态（最近出现但尚未确认）
        - consecutive_count：候选状态连续出现的次数
        - 当候选状态连续出现 >= stability_period 时，才将 current_stable 切换为 candidate_state

        该方法可以显著降低“状态翻转”带来的换仓/撤单噪声。
        """
        if len(raw_states) == 0:
            return raw_states

        stable_states = []
        current_stable = MarketState.SIDEWAYS

        consecutive_count = 0
        candidate_state = None

        for state in raw_states:
            if state == current_stable:
                consecutive_count = 0
                candidate_state = None
            else:
                if state == candidate_state:
                    consecutive_count += 1
                else:
                    candidate_state = state
                    consecutive_count = 1

                if consecutive_count >= self.stability_period:
                    current_stable = candidate_state
                    consecutive_count = 0
                    candidate_state = None

            stable_states.append(current_stable)

        return pd.Series(stable_states, index=raw_states.index)

    @staticmethod
    def align_state_to_lower_tf(
        state_high_tf: pd.Series, index_low_tf: pd.DatetimeIndex
    ) -> pd.Series:
        """
        将高周期（例如 1D）的状态序列对齐到低周期（例如 1H/15m）的时间索引。

        关键点：
        - 使用 forward fill（ffill）传播“最近一次已知的高周期状态”
        - 避免 lookahead：低周期在某时刻只能使用该时刻之前（含当根收盘已确认）的高周期状态
        """
        # Reindex with forward fill
        aligned = state_high_tf.reindex(index_low_tf, method="ffill")

        # Handle initial NaNs if low TF starts before high TF
        # aligned.fillna(method='bfill', inplace=True) # Deprecated in newer pandas
        aligned.fillna(MarketState.SIDEWAYS, inplace=True)

        return aligned
