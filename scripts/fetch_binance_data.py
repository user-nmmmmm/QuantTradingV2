"""
批量下载并缓存币安（Binance）历史行情，供回测使用。

设计要点
--------
- 复用 ``core.data_fetcher.DataFetcher``（统一归一化 / 去重 / 时区口径）。
- 按周期将每块限制为最多约 9000 根，避免 ``fetch_ccxt`` 单次 10000 根安全上限，
  因此 ``1h`` / ``5m`` 这类高频周期跨多年也能完整下载。
- 增量更新：本地已有缓存时，只补下最后一根 K 线之后的数据。
- 每个周期目录下写 ``_manifest.json``：记录每个文件的 SHA-256、行数、时间范围和抓取时间，
  便于回测复现与 Phase 2 的数据关口核对。

输出
----
    data/binance/<timeframe>/<SAFE_SYMBOL>.csv      # index=timestamp, 列=open/high/low/close/volume
    data/binance/<timeframe>/_manifest.json
    data/binance/funding/<SAFE_SYMBOL>.csv          # --with-funding 时，列=funding_rate

回测里怎么用
------------
    # 方式 A：直接把缓存目录当第二数据源 / 独立核对源
    python main.py --source ccxt --symbols BTC/USDT ETH/USDT --start 2020-01-01 --end 2024-01-01 \
        --secondary-data-dir data/binance/1d

    # 方式 B：先用本脚本填满缓存，再自行改 get_data 读本地（见 README 说明）

用法示例
--------
    # 默认：Phase 0 基线那 10 个标的，日线，2017-01-01 至今
    python scripts/fetch_binance_data.py

    # 指定标的与周期
    python scripts/fetch_binance_data.py --symbols BTC/USDT ETH/USDT --timeframe 1h --start 2022-01-01

    # 强制全量重下（忽略已有缓存）
    python scripts/fetch_binance_data.py --no-incremental

    # 一并下载永续资金费率（Phase 3 T-3.6 需要）
    python scripts/fetch_binance_data.py --with-funding
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.data_fetcher import DataFetcher, timeframe_delta  # noqa: E402
from core.logger import get_logger  # noqa: E402
from core.universe import normalize_symbol  # noqa: E402

logger = get_logger("fetch_binance_data")

# --- 默认参数 -----------------------------------------------------------------
# 30 个币安 USDT 交易对；DEFAULT_START 设为币安上线首月，
# 每个标的实际会从它自己在币安的首根 K 线开始（更早的分块返回空）。
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "SOL/USDT",
    "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LTC/USDT", "LINK/USDT", "TRX/USDT",
    "MATIC/USDT", "ATOM/USDT", "UNI/USDT", "ETC/USDT", "XLM/USDT", "BCH/USDT",
    "FIL/USDT", "NEAR/USDT", "ICP/USDT", "AAVE/USDT", "APT/USDT", "ARB/USDT",
    "OP/USDT", "INJ/USDT", "SUI/USDT", "SEI/USDT", "TIA/USDT", "LDO/USDT",
]
DEFAULT_START = "2017-07-01"  # 币安现货上线首月
DEFAULT_TIMEFRAME = "1d"
DEFAULT_OUT_ROOT = REPO_ROOT / "data" / "binance"
REQUIRED_COLS = ["open", "high", "low", "close", "volume"]


def safe_symbol(symbol: str) -> str:
    """BTC/USDT -> BTC_USDT （与 main.py 的 _safe_symbol_name 口径一致）。"""
    return symbol.replace("/", "_").replace("-", "_").replace(":", "_")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_ohlcv(
    fetcher: DataFetcher,
    symbol: str,
    timeframe: str,
    start: str,
    end: str,
    exchange: str,
) -> pd.DataFrame:
    """Bound each inclusive UTC chunk below the fetcher's 10000-bar cap."""
    if exchange != "binance":
        raise ValueError("Binance cache requires the Binance provider")
    delta = timeframe_delta(timeframe)
    days = max(1, int(pd.Timedelta(days=1) / delta))
    chunk_days = max(1, 9000 // days)
    cursor, final_day = pd.Timestamp(start), pd.Timestamp(end)
    frames: list[pd.DataFrame] = []
    while cursor <= final_day:
        last_day = min(cursor + pd.Timedelta(days=chunk_days - 1), final_day)
        chunk_start, chunk_end = cursor.strftime("%Y-%m-%d"), last_day.strftime("%Y-%m-%d")
        logger.info("  %s %s..%s", symbol, chunk_start, chunk_end)
        part = fetcher.fetch_ccxt(
            symbol,
            timeframe=timeframe,
            start_date=chunk_start,
            end_date=chunk_end,
            limit=1000,
            exchange_id=exchange,
        )
        if part is not None and not part.empty:
            if (part.attrs.get("provider") != "binance" or part.attrs.get("timeframe") != timeframe
                    or part.attrs.get("market_type") != "spot"
                    or normalize_symbol(part.attrs.get("symbol", "")) != normalize_symbol(symbol)):
                raise ValueError(f"Unverified provider/symbol/timeframe/account for {symbol}")
            if part.attrs.get("pagination_termination") == "safety_limit":
                raise ValueError(f"Truncated download for {symbol}")
            frames.append(part)
        cursor = last_day + pd.Timedelta(days=1)
    if not frames:
        return pd.DataFrame(columns=REQUIRED_COLS)
    df = pd.concat(frames)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "timestamp"
    df = df[REQUIRED_COLS]
    df.attrs.update(provider="binance", timeframe=timeframe, market_type="spot", symbol=symbol)
    return df


def validate_coverage(frame: pd.DataFrame, start: str, end: str, timeframe: str, as_of=None) -> dict:
    """Strict coverage of closed bars; leading history needs separate PIT evidence."""
    delta = timeframe_delta(timeframe)
    now = pd.Timestamp(as_of if as_of is not None else datetime.now(timezone.utc))
    now = now.tz_convert("UTC").tz_localize(None) if now.tzinfo else now
    boundary = min(pd.Timestamp(end) + pd.Timedelta(days=1), now.floor(delta))
    expected = pd.date_range(pd.Timestamp(start), boundary, freq=delta, inclusive="left")
    actual = frame.index[(frame.index >= pd.Timestamp(start)) & (frame.index < boundary)]
    if expected.empty or not actual.equals(expected):
        raise ValueError(f"Incomplete requested coverage: {len(actual)}/{len(expected)} closed bars")
    return {"status": "complete", "effective_end_exclusive": boundary.isoformat(),
            "end_policy": "closed_bars_only", "expected_rows": len(expected)}


def load_existing(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index.name = "timestamp"
        return df[REQUIRED_COLS].sort_index()
    except Exception as exc:  # 缓存损坏就当没有
        logger.warning("忽略损坏的缓存 %s: %s", path, exc)
        return None


def merge_incremental(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    if old is None or old.empty:
        return new
    combined = pd.concat([old, new])
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return combined


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="CCXT 格式，如 BTC/USDT")
    parser.add_argument("--timeframe", default=DEFAULT_TIMEFRAME, help="1d / 4h / 1h / 15m / 5m ...")
    parser.add_argument("--start", default=DEFAULT_START, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD，默认今天(UTC)")
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT))
    parser.add_argument("--proxy", default=None, help="HTTP(S) 代理，如 http://127.0.0.1:7890；默认读环境变量 QUANT_PROXY_URL")
    parser.add_argument("--no-incremental", action="store_true", help="忽略已有缓存，全量重下")
    parser.add_argument("--with-funding", action="store_true", help="一并下载永续资金费率历史")
    args = parser.parse_args(argv)

    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_dir = Path(args.out_root) / args.timeframe
    out_dir.mkdir(parents=True, exist_ok=True)

    fetcher = DataFetcher(proxy_url=args.proxy, data_timezone="UTC")

    manifest_path = out_dir / "_manifest.json"
    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    manifest = {
        "schema_version": "binance-cache/v2",
        "provider": args.exchange,
        "market_type": "spot",
        "exchange": args.exchange,
        "timeframe": args.timeframe,
        "requested_start": args.start,
        "requested_end": end,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbols": {},
    }

    failures: list[str] = []
    for symbol in args.symbols:
        logger.info("下载 %s (%s)", symbol, args.timeframe)
        csv_path = out_dir / f"{safe_symbol(symbol)}.csv"

        old_record = old_manifest.get("symbols", {}).get(symbol, {})
        trusted = (old_manifest.get("schema_version") == "binance-cache/v2"
                   and old_manifest.get("provider") == args.exchange
                   and old_manifest.get("timeframe") == args.timeframe
                   and old_manifest.get("market_type") == "spot"
                   and old_record.get("provider") == args.exchange
                   and old_record.get("timeframe") == args.timeframe
                   and old_record.get("market_type") == "spot"
                   and old_record.get("symbol") == normalize_symbol(symbol)
                   and old_record.get("file") == csv_path.name
                   and old_record.get("coverage", {}).get("status") == "complete"
                   and csv_path.exists() and old_record.get("sha256") == sha256_file(csv_path))
        existing = load_existing(csv_path) if trusted and not args.no_incremental else None
        if csv_path.exists() and not trusted:
            logger.warning("Unverified legacy cache excluded from incremental merge: %s", csv_path)
        fetch_from = args.start
        if existing is not None and not existing.empty:
            last = existing.index.max()
            fetch_from = (last.normalize()).strftime("%Y-%m-%d")
            logger.info("  已有缓存至 %s，增量补下 %s..%s", last.date(), fetch_from, end)

        try:
            fresh = fetch_ohlcv(fetcher, symbol, args.timeframe, fetch_from, end, args.exchange)
            if fresh.empty:
                raise ValueError("Refresh returned no verified data")
        except Exception as exc:
            logger.error("  抓取失败 %s: %s", symbol, exc)
            failures.append(symbol)
            continue

        merged = merge_incremental(existing, fresh)
        if merged.empty:
            logger.error("  无数据 %s", symbol)
            failures.append(symbol)
            continue

        try:
            coverage = validate_coverage(merged, args.start, end, args.timeframe)
        except ValueError as exc:
            failures.append(symbol)
            logger.error("  Coverage rejected %s: %s", symbol, exc)
            continue
        merged = merged.loc[merged.index < pd.Timestamp(coverage["effective_end_exclusive"])]
        legacy_copy = None
        if csv_path.exists() and not trusted:
            legacy = out_dir / "_legacy_unverified"
            legacy.mkdir(exist_ok=True)
            archived = legacy / f"{csv_path.stem}.{sha256_file(csv_path)}.csv"
            if not archived.exists():
                archived.write_bytes(csv_path.read_bytes())
            legacy_copy = str(archived.relative_to(out_dir))
        merged.to_csv(csv_path)
        manifest["symbols"][symbol] = {
            "provider": args.exchange, "timeframe": args.timeframe, "market_type": "spot",
            "symbol": normalize_symbol(symbol), "original_symbol": symbol,
            "requested_start": args.start, "requested_end": end, "coverage": coverage,
            "legacy_unverified_copy": legacy_copy,
            "file": csv_path.name,
            "rows": int(len(merged)),
            "first": merged.index.min().isoformat(),
            "last": merged.index.max().isoformat(),
            "sha256": sha256_file(csv_path),
        }
        logger.info("  写入 %s（%d 行，%s .. %s）", csv_path.name, len(merged),
                    merged.index.min().date(), merged.index.max().date())

        if args.with_funding:
            perp = f"{symbol}:USDT" if ":" not in symbol else symbol
            try:
                fr = fetcher.fetch_funding_rate_history(perp, exchange_id=args.exchange,
                                                        start_date=args.start, end_date=end)
                if fr is not None and not fr.empty:
                    fdir = Path(args.out_root) / "funding"
                    fdir.mkdir(parents=True, exist_ok=True)
                    fpath = fdir / f"{safe_symbol(symbol)}.csv"
                    fr.sort_index().to_csv(fpath)
                    manifest["symbols"][symbol]["funding_file"] = str(fpath.relative_to(args.out_root))
                    manifest["symbols"][symbol]["funding_rows"] = int(len(fr))
                    logger.info("  资金费率 %d 行 -> %s", len(fr), fpath.name)
                else:
                    logger.warning("  资金费率无数据 %s", perp)
            except Exception as exc:
                logger.warning("  资金费率抓取失败 %s: %s", perp, exc)

    manifest["failures"] = failures
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("清单写入 %s", manifest_path)

    if failures:
        logger.error("以下标的失败：%s", ", ".join(failures))
        return 1
    logger.info("完成：%d 个标的", len(args.symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
