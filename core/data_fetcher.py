import os
from datetime import datetime
from typing import Optional, Union

import numpy as np
import pandas as pd

from core.logger import get_logger

# 本模块与回测/实盘的数据源解耦：上层只关心返回统一格式的 OHLCV DataFrame
# （index 为时间戳，列名为 open/high/low/close/volume，均为小写）

logger = get_logger(__name__)
_UNSET = object()


class DataFetcher:
    DEFAULT_CCXT_EXCHANGES = ("binance", "okx", "kraken", "coinbase")
    """
    统一数据获取器（DataFetcher）。

    支持的数据源：
    - Yahoo Finance（yfinance）：适合股票/ETF/部分加密的历史数据
    - 交易所（CCXT）：适合加密现货/合约的历史 K 线
    - Synthetic（情景数据）：用于无外部依赖的回测验证/压力测试（generate_scenario）

    输出约定：
    - 返回的 DataFrame 以 timestamp 为 DatetimeIndex
    - 列名统一为小写：open/high/low/close/volume

    代理支持：
    - 通过 proxy_url 设置 HTTP_PROXY / HTTPS_PROXY，便于在受限网络环境抓取数据
    """

    def __init__(
        self,
        proxy_url: Optional[str] = _UNSET,
        request_timeout_ms: int = 10000,
    ):
        """
        参数：
        - proxy_url：代理地址（默认指向本机常见代理端口）；若为 None，则不设置代理
        """
        if proxy_url is _UNSET:
            proxy_url = os.getenv("QUANT_PROXY_URL")

        self.proxy_url = proxy_url
        self.request_timeout_ms = request_timeout_ms

        if self.proxy_url:
            logger.info("Using outbound proxy for data fetches: %s", self.proxy_url)

    def _build_ccxt_proxies(self) -> Optional[dict]:
        """
        配置代理环境变量。

        说明：
        - yfinance 基于 requests，会读取 HTTP_PROXY/HTTPS_PROXY
        - ccxt 也可通过 proxies 参数单独传入；这里两者都覆盖
        """
        if not self.proxy_url:
            return None

        return {
            "http": self.proxy_url,
            "https": self.proxy_url,
        }

    @staticmethod
    def _to_yahoo_crypto_symbol(symbol: str) -> Optional[str]:
        normalized = symbol.replace("/", "-").upper()
        if "-" not in normalized:
            return None

        base, quote = normalized.split("-", 1)
        quote_map = {"USDT": "USD", "USD": "USD"}
        yahoo_quote = quote_map.get(quote)
        if yahoo_quote is None:
            return None
        return f"{base}-{yahoo_quote}"

    def _fallback_to_yahoo_crypto(
        self,
        symbol: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> pd.DataFrame:
        yahoo_symbol = self._to_yahoo_crypto_symbol(symbol)
        if yahoo_symbol is None:
            return pd.DataFrame()

        if start_date is None or end_date is None:
            return pd.DataFrame()

        logger.warning(
            "CCXT fetch failed for %s; falling back to Yahoo Finance symbol %s",
            symbol,
            yahoo_symbol,
        )
        return self.fetch_yahoo(yahoo_symbol, start_date, end_date)

    @staticmethod
    def _normalize_ccxt_symbol(symbol: str, exchange_id: str) -> str:
        normalized = symbol.replace("-", "/").upper()
        if "/" not in normalized:
            return normalized

        base, quote = normalized.split("/", 1)
        if exchange_id in {"kraken", "coinbase"} and quote == "USDT":
            quote = "USD"
        return f"{base}/{quote}"

    def fetch_yahoo(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Yahoo Finance 拉取历史行情（通过 yfinance）。

        参数：
        - symbol：Yahoo 的 ticker（例如 AAPL、SPY、BTC-USD）
        - start_date / end_date：YYYY-MM-DD
        """
        try:
            import yfinance as yf

            logger.info(f"Fetching {symbol} via yfinance...")

            download_kwargs = {
                "start": start_date,
                "end": end_date,
                "progress": False,
                "timeout": self.request_timeout_ms / 1000,
            }
            if self.proxy_url:
                try:
                    import requests

                    session = requests.Session()
                    session.proxies.update(
                        {"http": self.proxy_url, "https": self.proxy_url}
                    )
                    download_kwargs["session"] = session
                except Exception:
                    logger.warning("Failed to construct proxy session for yfinance")

            df = yf.download(symbol, **download_kwargs)

            if df.empty:
                logger.warning(f"No data returned for {symbol}")
                return pd.DataFrame()

            # Handle MultiIndex columns if present
            if isinstance(df.columns, pd.MultiIndex):
                try:
                    df.columns = df.columns.droplevel(1)
                except IndexError:
                    pass

            return self._normalize(df)

        except ImportError:
            logger.error("yfinance not installed. Please run: pip install yfinance")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error fetching {symbol} from Yahoo: {e}")
            return pd.DataFrame()

    def fetch_ccxt(
        self,
        symbol: str,
        timeframe: str = "1d",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 1000,
        exchange_id: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        通过 CCXT 从交易所拉取历史 K 线（当前默认 binance）。

        参数：
        - symbol：优先使用 CCXT 格式（例如 BTC/USDT）；若传入 BTC-USD，会尝试转换为 BTC/USDT
        - timeframe：K 线周期（例如 1m/5m/1h/1d）
        - start_date / end_date：YYYY-MM-DD，可选；用于控制拉取范围（通过 since 分页）
        - limit：单次分页上限（CCXT 通常最大 1000）

        返回：
        - 标准化后的 OHLCV DataFrame（时间戳为 DatetimeIndex）
        """
        try:
            import ccxt

            exchange_ids = [exchange_id] if exchange_id else list(self.DEFAULT_CCXT_EXCHANGES)

            for current_exchange_id in exchange_ids:
                ccxt_symbol = self._normalize_ccxt_symbol(symbol, current_exchange_id)
                try:
                    logger.info(
                        "Fetching %s via CCXT (%s)...",
                        ccxt_symbol,
                        current_exchange_id,
                    )
                    exchange_class = getattr(ccxt, current_exchange_id)
                    exchange = exchange_class(
                        {
                            "enableRateLimit": True,
                            "proxies": self._build_ccxt_proxies(),
                            "timeout": self.request_timeout_ms,
                        }
                    )
                    if hasattr(exchange, "session") and hasattr(exchange.session, "trust_env"):
                        exchange.session.trust_env = True

                    since = None
                    if start_date:
                        dt = datetime.strptime(start_date, "%Y-%m-%d")
                        since = int(dt.timestamp() * 1000)

                    all_ohlcv = []
                    current_since = since
                    while True:
                        batch_limit = min(limit, 1000)
                        ohlcv = exchange.fetch_ohlcv(
                            ccxt_symbol,
                            timeframe=timeframe,
                            limit=batch_limit,
                            since=current_since,
                        )
                        if not ohlcv:
                            break
                        all_ohlcv.extend(ohlcv)
                        last_timestamp = ohlcv[-1][0]
                        current_since = last_timestamp + 1

                        if end_date:
                            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                            end_ts = int(end_dt.timestamp() * 1000)
                            if last_timestamp >= end_ts:
                                break

                        if len(ohlcv) < batch_limit:
                            break

                        if len(all_ohlcv) >= 10000:
                            logger.warning(
                                "Reached safety limit of 10000 candles for %s on %s",
                                ccxt_symbol,
                                current_exchange_id,
                            )
                            break

                    if not all_ohlcv:
                        logger.warning(
                            "No data returned for %s on %s",
                            ccxt_symbol,
                            current_exchange_id,
                        )
                        continue

                    df = pd.DataFrame(
                        all_ohlcv,
                        columns=["timestamp", "open", "high", "low", "close", "volume"],
                    )
                    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                    df.set_index("timestamp", inplace=True)
                    if end_date:
                        df = df[df.index <= end_date]
                    return self._normalize(df)
                except Exception as exchange_error:
                    logger.error(
                        "Error fetching %s from CCXT exchange %s: %s",
                        ccxt_symbol,
                        current_exchange_id,
                        exchange_error,
                    )

            fallback_df = self._fallback_to_yahoo_crypto(symbol, start_date, end_date)
            if not fallback_df.empty:
                return fallback_df
            return pd.DataFrame()

        except ImportError:
            logger.error("ccxt not installed. Please run: pip install ccxt")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error fetching {symbol} from CCXT: {e}")
            fallback_df = self._fallback_to_yahoo_crypto(symbol, start_date, end_date)
            if not fallback_df.empty:
                return fallback_df
            return pd.DataFrame()

    def generate_scenario(
        self,
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
    ) -> pd.DataFrame:
        """
        生成情景模拟数据（Synthetic Scenario）。

        用途：
        - 在无外部数据依赖时验证回测引擎、路由与风控逻辑
        - 通过“趋势上行/震荡/趋势下行”三阶段组合，覆盖常见市场制度

        生成方式（简化）：
        - 对数收益服从正态分布，按阶段赋予不同均值/波动
        - 由累计收益生成 price_path，再构造符合 OHLC 约束的 open/high/low/close
        """
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, "%Y-%m-%d")
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, "%Y-%m-%d")

        logger.info("Generating scenario-based data for %s", symbol)

        dates = pd.date_range(start=start_date, end=end_date, freq="D")
        days = len(dates)

        if days < 10:
            logger.warning(
                "Date range is short for scenario generation: %s days", days
            )

        # Split into 3 phases: Trend Up, Sideways, Trend Down
        phase_len = days // 3

        # 1. Trend Up (Strong upward drift, low volatility)
        phase1_returns = np.random.normal(0.005, 0.01, size=phase_len)

        # 2. Sideways (Zero drift, higher volatility)
        phase2_returns = np.random.normal(0.0, 0.02, size=phase_len)

        # 3. Trend Down (Strong downward drift, high volatility)
        remaining = days - (phase_len * 2)
        phase3_returns = np.random.normal(-0.005, 0.015, size=remaining)

        returns = np.concatenate([phase1_returns, phase2_returns, phase3_returns])

        start_price = 10000.0 if "BTC" in symbol else 2000.0
        price_path = start_price * np.exp(np.cumsum(returns))

        # OHLC
        high = price_path * (1 + np.abs(np.random.normal(0, 0.01, size=days)))
        low = price_path * (1 - np.abs(np.random.normal(0, 0.01, size=days)))
        close = price_path
        open_p = price_path * (1 + np.random.normal(0, 0.005, size=days))

        # Fix High/Low consistency
        high = np.maximum(high, np.maximum(open_p, close))
        low = np.minimum(low, np.minimum(open_p, close))

        data = {
            "open": open_p,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.random.randint(1000, 100000, size=days),
        }

        df = pd.DataFrame(data, index=dates)
        return self._normalize(df)

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        统一列名与数据类型。

        说明：
        - 列名全部转为小写
        - 对 open/high/low/close/volume 尝试转为数值（errors='coerce' -> 无法解析则 NaN）
        """
        df.columns = [c.lower() for c in df.columns]
        # Ensure required columns exist
        required = ["open", "high", "low", "close", "volume"]
        for col in required:
            if col not in df.columns:
                # Try to map similar names? For now just return as is
                pass
            else:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        return df
