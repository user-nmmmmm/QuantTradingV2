import pandas as pd

from core.logger import get_logger

logger = get_logger(__name__)

"""
DataHandler（数据处理）模块

本模块用于将原始行情数据标准化为回测引擎可消费的统一格式，并提供数据质量分析能力。

数据契约（Data Contract）：
- index 必须为 DatetimeIndex（或可转换为 DatetimeIndex）
- 必须包含 OHLCV 五列：open/high/low/close/volume（大小写不敏感，内部统一为小写）
- 列类型尽量转换为数值，无法转换的值会变为 NaN（后续由策略/引擎决定如何处理）

工具能力：
- validate：校验并标准化单个 DataFrame
- resample_ohlcv：将低周期 K 线重采样到高周期（严格按收盘时间标记）
- analyze_quality / generate_quality_report：统计缺失、重复、时间缺口、异常尖刺等
"""

class DataHandler:
    """
    负责数据的加载、验证和标准化。
    """
    REQUIRED_COLUMNS = ['open', 'high', 'low', 'close', 'volume']
    ANOMALY_COLUMNS = [
        "anomaly_missing_ohlcv",
        "anomaly_invalid_ohlc",
        "anomaly_spike",
        "anomaly_nonpositive",
        "anomaly_any",
    ]

    @staticmethod
    def validate(df: pd.DataFrame) -> pd.DataFrame:
        """
        验证 DataFrame 是否符合 Bar/Series 约定。
        1. 必须包含 datetime index
        2. 必须包含 open, high, low, close, volume 列
        3. 列名统一转换为小写

        返回：
        - 标准化后的 df（原地修改并返回同一个对象）

        常见失败原因：
        - index 不是时间索引且无法转换（例如纯数字且缺乏日期语义）
        - 缺失 OHLCV 关键列
        """
        if not isinstance(df.index, pd.DatetimeIndex):
            # 尝试将 index 转换为 datetime
            try:
                df.index = pd.to_datetime(df.index)
            except Exception as e:
                raise ValueError("DataFrame index must be DatetimeIndex or convertible to DatetimeIndex") from e

        # 统一列名为小写
        df.columns = [c.lower() for c in df.columns]

        # 检查必需列
        missing_columns = [col for col in DataHandler.REQUIRED_COLUMNS if col not in df.columns]
        if missing_columns:
            raise ValueError(f"Missing required columns: {missing_columns}")

        # 确保数据类型为数值型
        for col in DataHandler.REQUIRED_COLUMNS:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 删除任何包含 NaN 的行 (可选，视策略而定，这里暂时保留原始行为，由后续步骤处理)
        # df.dropna(subset=DataHandler.REQUIRED_COLUMNS, inplace=True)

        return df

    @staticmethod
    def load_csv(file_path: str) -> pd.DataFrame:
        """
        从 CSV 加载并验证。

        约定：
        - CSV 第一列为时间索引（index_col=0）
        - 自动 parse_dates=True
        """
        df = pd.read_csv(file_path, index_col=0, parse_dates=True)
        return DataHandler.validate(df)

    @staticmethod
    def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
        """
        Resample OHLCV data to a higher timeframe.
        rule: "4H", "1D", etc.

        重要的时间语义：
        - closed='right', label='right'：让时间戳代表“该根 bar 的收盘时间”
        - 该约定与“信号在 bar 收盘产生、下根 bar 开盘执行”的回测模型更一致
        """
        agg_dict = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }
        # Only aggregate columns that exist
        current_agg = {k: v for k, v in agg_dict.items() if k in df.columns}
        
        # Resample
        # closed='right', label='right' ensures that the bar labeled '04:00' contains data ending at '04:00'
        # This aligns with the concept that the timestamp represents the CLOSE time.
        resampled = df.resample(rule, closed='right', label='right').agg(current_agg)
        
        # Drop NaNs created by resampling (e.g. gaps)
        resampled.dropna(inplace=True)
        
        return resampled

    @staticmethod
    def analyze_quality(df: pd.DataFrame, symbol: str = "Unknown") -> dict:
        """
        Analyze data quality: gaps, duplicates, spikes.

        指标说明：
        - duplicates：时间索引重复的行数
        - gaps：时间差 > 1.5 * 典型频率（median diff）的数量（用于发现缺口）
        - spikes：单根收盘涨跌幅绝对值 > 20% 的次数（用于发现异常点/极端行情）
        """
        annotated = DataHandler.annotate_quality(df, copy=True)
        report = {
            "symbol": symbol,
            "total_rows": len(df),
            "start_date": str(df.index.min()),
            "end_date": str(df.index.max()),
            "duplicates": df.index.duplicated().sum(),
            "missing_values": df.isnull().sum().to_dict(),
            "gaps": 0,
            "spikes": int(annotated["anomaly_spike"].sum()),
            "invalid_ohlc": int(annotated["anomaly_invalid_ohlc"].sum()),
            "nonpositive_prices": int(annotated["anomaly_nonpositive"].sum()),
            "anomaly_rows": int(annotated["anomaly_any"].sum()),
            "anomaly_timestamps": [
                str(timestamp) for timestamp in annotated.index[annotated["anomaly_any"]]
            ],
        }

        # Check for gaps (assuming regular frequency if possible)
        # Calculate time diffs
        if len(df) > 1:
            diffs = df.index.to_series().diff().dropna()
            # Infer frequency: median diff
            freq = diffs.median()
            # Gaps: diff > 1.5 * freq
            gaps = diffs[diffs > 1.5 * freq]
            report["gaps"] = len(gaps)
            report["expected_freq"] = str(freq)
            
            # Detailed anomaly flags are calculated by annotate_quality so the
            # exact same facts can travel with bars and fills (T-2.10).

        return report

    @staticmethod
    def annotate_quality(
        df: pd.DataFrame,
        *,
        spike_threshold: float = 0.20,
        copy: bool = True,
    ) -> pd.DataFrame:
        """Attach row-level anomaly facts to a market frame.

        Flags are ordinary boolean columns, so the historical adapter carries
        them into ``MarketDataSlice.bars`` and Broker persists them into each
        fill's ``data_quality_context``.  The data is marked, not silently
        deleted or repaired.
        """

        if spike_threshold <= 0:
            raise ValueError("spike_threshold must be positive")
        result = df.copy() if copy else df
        required = [column for column in DataHandler.REQUIRED_COLUMNS if column in result]
        missing = result[required].isna().any(axis=1) if required else pd.Series(True, index=result.index)
        invalid_ohlc = (
            (result["high"] < result[["open", "close", "low"]].max(axis=1))
            | (result["low"] > result[["open", "close", "high"]].min(axis=1))
        )
        nonpositive = (result[["open", "high", "low", "close"]] <= 0).any(axis=1)
        spike = result["close"].pct_change(fill_method=None).abs().gt(spike_threshold)
        result["anomaly_missing_ohlcv"] = missing.astype(bool)
        result["anomaly_invalid_ohlc"] = invalid_ohlc.fillna(True).astype(bool)
        result["anomaly_spike"] = spike.fillna(False).astype(bool)
        result["anomaly_nonpositive"] = nonpositive.fillna(True).astype(bool)
        result["anomaly_any"] = result[DataHandler.ANOMALY_COLUMNS[:-1]].any(axis=1)
        return result

    @staticmethod
    def generate_quality_report(data_map: dict, output_path: str = None) -> dict:
        """
        Generate comprehensive quality report for multiple symbols.

        参数：
        - data_map：symbol -> DataFrame
        - output_path：若提供，则写入 JSON 文件（default=str 用于序列化 Timestamp）
        """
        full_report = {}
        for symbol, df in data_map.items():
            full_report[symbol] = DataHandler.analyze_quality(df, symbol)
            
        if output_path:
            import json
            try:
                with open(output_path, 'w') as f:
                    json.dump(full_report, f, indent=4, default=str)
                logger.info("Data quality report saved to %s", output_path)
            except Exception as e:
                logger.error("Failed to save data quality report: %s", e)
                
        return full_report
