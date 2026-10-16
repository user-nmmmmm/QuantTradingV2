"""Market-data loading for the backtest command: local CSV, synthetic, exchange and secondary data."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from datetime import date
import io
import json
import pandas as pd
import re

from core.foundation.content_hash import sha256_lf_bytes
from core.market.data import DataHandler
from core.market.data_fetcher import DataFetcher
from core.market.temporal_data import compatibility_audit, freeze_ohlcv, temporal_policy as parse_temporal_policy
from core.market.universe import PointInTimeUniverse, normalize_symbol, static_universe_manifest


def _research_window(path: Path, timeframe: str, end: str) -> dict | None:
    policy_path = path.parent / "_research_policy.json"
    if not policy_path.exists():
        return None
    policy = json.loads(policy_path.read_bytes())
    if policy.get("schema") != "adopted-research-window/v1" or policy.get("timeframe") != timeframe:
        raise ValueError("unsupported adopted research policy or timeframe")
    entry = policy.get("files", {}).get(path.name)
    if not isinstance(entry, dict):
        raise ValueError(f"{path.name}: not registered in the adopted research policy")
    if (not isinstance(entry.get("research_end_inclusive"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", "")))
            or not isinstance(policy.get("receipt"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(policy.get("receipt_sha256", "")))):
        raise ValueError("invalid adopted research policy fields")
    cutoff = entry["research_end_inclusive"]
    if date.fromisoformat(cutoff).isoformat() != cutoff or date.fromisoformat(end).isoformat() != end:
        raise ValueError("adopted research end must be a canonical daily date")
    if end > cutoff:
        raise ValueError(f"{path.name}: requested end {end} exceeds adopted research end {cutoff}")
    return {**entry, "receipt": policy["receipt"], "receipt_sha256": policy["receipt_sha256"]}


def _load_local_ohlcv(symbol: str, start: str, end: str, data_dir: str, *,
                      timeframe="1d", temporal_policy=None) -> pd.DataFrame:
    """
    从本地缓存目录读取单标的 OHLCV CSV（由 scripts/fetch_binance_data.py 生成）。

    - 文件名按 symbol 归一化匹配，兼容 BTC/USDT、BTC-USDT、BTC_USDT 等写法。
    - 起止日期均包含在内；实际筛选区间为 [start 00:00, end 次日 00:00)。
    - 有时区的索引先转成 UTC 再去掉时区；此函数按归一后的时间裁剪。
    - 指定版本存储时先冻结并从快照读取原文件，保留本次输入身份；
      快照不意味着数据在历史决策时刻已经可用，严格筛选由时间策略负责。
    """
    root = Path(data_dir)
    if not root.is_dir():
        raise ValueError(f"Local data directory does not exist: {root}")

    candidates = [
        root / f"{_safe_symbol_name(symbol)}.csv",
        root / f"{symbol.replace('/', '_').replace('-', '_').replace(':', '_')}.csv",
        root / f"{symbol}.csv",
    ]
    path = next((item for item in candidates if item.exists()), None)
    if path is None:
        raise FileNotFoundError(
            f"No local CSV for {symbol} in {root} (tried: {', '.join(c.name for c in candidates)})"
        )

    research_window = _research_window(path, timeframe, end)
    raw_identity = None
    if temporal_policy is not None and parse_temporal_policy(temporal_policy).store_path:
        from core.market.data_versions import DataVersionStore
        policy = parse_temporal_policy(temporal_policy)
        store = DataVersionStore(policy.store_path)
        raw_identity = store.freeze_files(f"raw-csv:{path.resolve()}", {path.name: path},
                                         observed_at=datetime.now(timezone.utc))
        raw = store.read_file(raw_identity["snapshot_id"], path.name)
        df = DataHandler.validate(pd.read_csv(io.BytesIO(raw), index_col=0, parse_dates=True))
    elif research_window:
        raw = path.read_bytes()
        df = DataHandler.validate(pd.read_csv(io.BytesIO(raw), index_col=0, parse_dates=True))
    else:
        df = DataHandler.load_csv(str(path))
    if research_window and sha256_lf_bytes(raw) != research_window["sha256"]:
        raise ValueError(f"{path.name}: bytes differ from the adopted research policy")
    if temporal_policy is not None:
        df = freeze_ohlcv(df, symbol=symbol, timeframe=timeframe, policy=temporal_policy,
            source_reference=f"csv:{path.name}", dataset_id=f"csv:{path.resolve()}|{symbol}|{timeframe}")
        if raw_identity:
            df.attrs["temporal_raw_file_snapshot"] = raw_identity["snapshot_id"]
    else:
        df.attrs["temporal_audit"] = compatibility_audit()
    if research_window:
        df.attrs["adopted_research_window"] = research_window
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1)
    return df[(df.index >= start_ts) & (df.index < end_ts)]


def get_data(
    symbol: str,
    start: str,
    end: str,
    source: str = "synthetic",
    days: int = 365,
    *,
    exchange: str = "binance",
    timeframe: str = "1d",
    data_timezone: str = "UTC",
    data_dir: str | None = None,
    market_type: str = "spot",
    derivatives_contract_file: str | None = None,
    derivatives_funding_file: str | None = None,
    derivatives_observations_file: str | None = None,
    derivatives_max_age: str = "24h",
    temporal_policy=None,
) -> pd.DataFrame:
    """
    获取单标的数据（封装 DataFetcher 的多源实现）。

    参数：
    - symbol：标的代码（不同数据源格式不同）
    - start/end：日期字符串（YYYY-MM-DD）
    - source：synthetic/yahoo/ccxt/local
    - days：回测天数（ccxt 作为 limit 的近似）
    - data_dir：source=local 时的本地缓存目录（如 data/binance/1d）
    """
    from core.market.derivatives_data import LinearContractSpec, load_derivatives_bundle, merge_derivative_features

    market_type = "margin" if market_type == "spot_margin" else market_type
    if market_type not in {"spot", "margin", "perpetual"}:
        raise ValueError(f"unsupported market_type: {market_type}")
    derivative_inputs = any((derivatives_contract_file, derivatives_funding_file, derivatives_observations_file))
    if derivative_inputs and market_type != "perpetual":
        raise ValueError("derivative inputs require market_type=perpetual")
    spec = None
    if market_type == "perpetual":
        if not derivatives_contract_file:
            raise ValueError("perpetual market data requires an explicit derivative contract file")
        spec = LinearContractSpec.from_json(derivatives_contract_file)
        spec.validate_request(symbol, exchange, market_type)
        if spec.contract_multiplier != 1:
            raise ValueError("bar engine uses base quantity; nonunit contract multipliers require explicit event replay")
        if source not in {"local", "ccxt"}:
            raise ValueError("perpetual OHLCV requires local or ccxt contract data")
    if derivatives_observations_file and not derivatives_funding_file:
        raise ValueError("derivative observations require an explicit funding file (missing costs cannot be assumed zero)")
    fetcher = DataFetcher(data_timezone=data_timezone,
                          **({"temporal_policy": temporal_policy} if temporal_policy is not None else {}))

    if source == "local":
        if not data_dir:
            raise ValueError("--source local requires --data-dir")
        frame = _load_local_ohlcv(symbol, start, end, data_dir, timeframe=timeframe,
                                  temporal_policy=temporal_policy)
    elif source == "ccxt":
        frame = fetcher.fetch_ccxt(
            symbol,
            timeframe=timeframe,
            limit=days,
            start_date=start,
            end_date=end,
            exchange_id=exchange,
            market_type=market_type,
            strict=True,
        )
    elif source == "yahoo":
        frame = fetcher.fetch_yahoo(symbol, start, end)
    else:
        # Synthetic / Scenario
        frame = fetcher.generate_scenario(symbol, start, end)
    if temporal_policy is not None and not frame.empty and not frame.attrs.get("temporal_identity"):
        frame = freeze_ohlcv(frame, symbol=symbol, timeframe=timeframe, policy=temporal_policy,
                             source_reference=source)
    elif temporal_policy is None:
        frame.attrs.setdefault("temporal_audit", compatibility_audit())
    if spec is not None:
        frame.attrs.update(market_type=market_type, contract_spec=spec.__dict__, contract_spec_digest=spec.digest)
        frame["derivative_contract_id"] = spec.contract_id
        frame["derivative_contract_spec_digest"] = spec.digest
        frame["funding_settlement_mode"] = "event_replay_required"
        if derivatives_funding_file:
            # These identity columns are recreated by the feature merger.
            frame = frame.drop(columns=["derivative_contract_id", "derivative_contract_spec_digest"])
            bundle = load_derivatives_bundle(derivatives_contract_file, derivatives_funding_file,
                                             derivatives_observations_file)
            frame = merge_derivative_features(frame, bundle, max_age=derivatives_max_age)
        else:
            frame.attrs["derivatives"] = {"quality": {"funding_status": "missing",
                                                        "observation_status": "missing",
                                                        "cost_mode": "explicit_position_event_replay_required"},
                                           "provenance": {"contract_spec": spec.__dict__, "spec_digest": spec.digest}}
    return frame


def _safe_symbol_name(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol).strip("._") or "symbol"


def _load_secondary_data(directory: str, symbols) -> dict:
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"Secondary data directory does not exist: {root}")
    result = {}
    for symbol in symbols:
        candidates = [
            root / f"{_safe_symbol_name(symbol)}.csv",
            root / f"{symbol}.csv",
        ]
        path = next((item for item in candidates if item.exists()), None)
        if path is None:
            continue
        frame = DataHandler.load_csv(str(path))
        result[symbol] = DataHandler.annotate_quality(frame)
    return result


def _resolve_date_range(args) -> tuple[datetime, datetime]:
    """Validate explicit dates or derive the requested range from ``--days``."""
    if args.start and args.end:
        try:
            start_date = datetime.strptime(args.start, "%Y-%m-%d")
            end_date = datetime.strptime(args.end, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("Invalid date format. Please use YYYY-MM-DD.") from exc
        if start_date >= end_date:
            raise ValueError("Start date must be before end date.")
        args.days = (end_date - start_date).days
        print(f"Config: Date Range={args.start} to {args.end} ({args.days} days)")
        return start_date, end_date
    end_date = datetime.now()
    start_date = end_date - timedelta(days=args.days)
    print(
        f"Config: Last {args.days} Days (Auto-calculated: "
        f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})"
    )
    return start_date, end_date


def _load_requested_data(args, start_date: datetime, end_date: datetime):
    """Fetch, quality-annotate and apply the point-in-time universe."""
    data_map = {}
    from config.config import config
    requested_market_type = args.market_type or (config.get("account") or {}).get("mode", "spot")
    download_started_at = datetime.now(timezone.utc)
    configured_temporal = (config.get("data") or {}).get("temporal_policy")
    temporal = configured_temporal
    if any(getattr(args, field, None) is not None for field in (
            "temporal_mode", "temporal_knowledge", "temporal_unknown", "data_version_store", "temporal_decision_delay_seconds")):
        temporal = dict(vars(parse_temporal_policy(configured_temporal)))
        for argument, field in (("temporal_mode", "mode"), ("temporal_knowledge", "knowledge"),
            ("temporal_unknown", "unknown"), ("data_version_store", "store_path"),
            ("temporal_decision_delay_seconds", "decision_delay_seconds")):
            if getattr(args, argument, None) is not None:
                temporal[field] = getattr(args, argument)
    args.resolved_temporal_policy = temporal
    for symbol in args.symbols:
        frame = get_data(
            symbol,
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d"),
            args.source,
            args.days,
            exchange=args.exchange,
            timeframe=args.timeframe,
            data_timezone=args.data_timezone,
            data_dir=args.data_dir,
            market_type=requested_market_type,
            derivatives_contract_file=getattr(args, "derivatives_contract_file", None),
            derivatives_funding_file=getattr(args, "derivatives_funding_file", None),
            derivatives_observations_file=getattr(args, "derivatives_observations_file", None),
            derivatives_max_age=getattr(args, "derivatives_max_age", "24h"),
            **({"temporal_policy": temporal} if temporal is not None else {}),
        )
        if not frame.empty and len(frame) > 10:
            print(f"Loaded {symbol}: {len(frame)} bars")
            key = normalize_symbol(symbol)
            if key in data_map:
                raise ValueError(f"Duplicate normalized symbol: {key}")
            data_map[key] = DataHandler.annotate_quality(frame)
        else:
            print(f"Failed to load sufficient data for {symbol}")
    if not data_map:
        raise RuntimeError("No data available. Exiting.")

    if args.universe_file:
        universe = PointInTimeUniverse.from_csv(args.universe_file)
        data_map = universe.apply(data_map)
        universe_identity = universe.to_manifest()
        if not data_map:
            raise RuntimeError("Point-in-time universe removed all requested data.")
    else:
        universe_identity = static_universe_manifest(data_map)
    return (
        data_map,
        universe_identity,
        download_started_at,
        datetime.now(timezone.utc),
    )
