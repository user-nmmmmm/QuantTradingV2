"""回测命令行入口：读取行情、执行历史回放并按报告模式保存结果。

数据源包括 synthetic（合成验证）、yahoo、ccxt 和 local（本地 CSV）。
参数定义集中在 backtest.cli；无参数时打印用法并退出，不进入交互式问答。
full 报告额外保存审计与复现证据，可用 --replay-manifest 核对确定性输出。
"""

import sys
import os
import shutil
import argparse
import random
import json
import re
from dataclasses import asdict
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 直接运行脚本时也能解析仓库内的包。
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.data_fetcher import DataFetcher
from core.temporal_data import compatibility_audit, freeze_ohlcv, temporal_policy as parse_temporal_policy
from core.data import DataHandler
from core.backtest_audit import (
    cross_verify_top_trades,
    validate_audit_coverage,
    write_event_log,
    write_json_report,
)
from core.reproducibility import (
    artifact_hashes,
    build_run_manifest,
    capital_allocation_digest,
    data_identity,
    deterministic_result_digest,
    load_data_snapshots,
    load_manifest,
    runtime_identity,
    save_data_snapshots,
    sha256_file,
    write_manifest,
)
from core.universe import PointInTimeUniverse, normalize_symbol, static_universe_manifest
from backtest.engine import BacktestEngine, DEFAULT_INITIAL_CAPITAL
from backtest.cli import build_backtest_parser
from backtest.reporting import ReportGenerator, format_primary_metrics
from core.logger import get_logger

logger = get_logger(__name__)

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

    raw_identity = None
    if temporal_policy is not None and parse_temporal_policy(temporal_policy).store_path:
        import io
        from core.data_versions import DataVersionStore
        policy = parse_temporal_policy(temporal_policy)
        store = DataVersionStore(policy.store_path)
        raw_identity = store.freeze_files(f"raw-csv:{path.resolve()}", {path.name: path},
                                         observed_at=datetime.now(timezone.utc))
        df = DataHandler.validate(pd.read_csv(io.BytesIO(store.read_file(
            raw_identity["snapshot_id"], path.name)), index_col=0, parse_dates=True))
    else:
        df = DataHandler.load_csv(str(path))
    if temporal_policy is not None:
        df = freeze_ohlcv(df, symbol=symbol, timeframe=timeframe, policy=temporal_policy,
            source_reference=f"csv:{path.name}", dataset_id=f"csv:{path.resolve()}|{symbol}|{timeframe}")
        if raw_identity:
            df.attrs["temporal_raw_file_snapshot"] = raw_identity["snapshot_id"]
    else:
        df.attrs["temporal_audit"] = compatibility_audit()
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
    from core.derivatives_data import LinearContractSpec, load_derivatives_bundle, merge_derivative_features

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


def replay_manifest(manifest_path: str) -> int:
    """Re-run a saved manifest and compare trades/equity/report payload exactly."""

    path = Path(manifest_path).resolve()
    manifest = load_manifest(path)
    expected_code = manifest["code"]
    config_path = Path(__file__).resolve().parent / "config" / "params.yaml"
    if manifest["config"]["sha256"] != sha256_file(config_path):
        print("Replay refused: current config hash differs from manifest.", file=sys.stderr)
        return 7
    snapshots = load_data_snapshots(
        path.parent / "data_inputs", manifest["data_snapshots"], verify=True
    )
    execution = manifest["execution"]
    try:
        replay_temporal_policy = _restore_temporal_replay_inputs(snapshots, execution, path.parent)
        replay_temporal_financing = _restore_temporal_financing(execution, path.parent)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Replay refused: frozen temporal evidence unavailable or changed: {exc}", file=sys.stderr)
        return 7
    # An old manifest must not inherit a subsequently enabled research model.
    meta_policy = execution.get("signal_meta_layer") or {"enabled": False}
    if meta_policy.get("enabled", False) and not execution.get("signal_meta_layer_digest"):
        print("Replay refused: enabled P1 policy has no recorded research digest.", file=sys.stderr)
        return 7
    from core.signal_adaptive_types import AdaptiveEVPolicy, MetaReplayPolicy
    try:
        adaptive_policy = AdaptiveEVPolicy.from_mapping(execution.get("signal_adaptive") or {"enabled": False})
        meta_replay_policy = MetaReplayPolicy.from_mapping(execution.get("signal_meta_replay") or {"enabled": False})
    except (TypeError, ValueError) as exc:
        print(f"Replay refused: invalid P2/P3 research policy: {exc}", file=sys.stderr)
        return 7
    for name, policy in (("signal_adaptive", adaptive_policy), ("signal_meta_replay", meta_replay_policy)):
        if policy.enabled and not execution.get(f"{name}_digest"):
            print(f"Replay refused: enabled {name} policy has no recorded research digest.", file=sys.stderr)
            return 7
    if ((meta_replay_policy.enabled and not adaptive_policy.enabled)
            or (adaptive_policy.enabled and not meta_policy.get("enabled", False))):
        print("Replay refused: P2/P3 research dependencies were not recorded as enabled.", file=sys.stderr)
        return 7
    recorded_observation = execution.get("signal_observation") or {}
    if adaptive_policy.enabled and (not recorded_observation.get("enabled", False)
                                    or not execution.get("signal_observation_digest")):
        print("Replay refused: P2 requires a recorded enabled P0 policy and digest.", file=sys.stderr)
        return 7
    if (meta_replay_policy.enabled
            and meta_replay_policy.horizon_bars not in recorded_observation.get("horizons", [])):
        print("Replay refused: P3 horizon is absent from the recorded P0 horizons.", file=sys.stderr)
        return 7
    symbol_order = execution.get("data_symbol_order")
    if symbol_order is not None:
        if len(symbol_order) != len(snapshots) or set(symbol_order) != set(snapshots):
            print("Replay refused: input symbol order does not match snapshots.", file=sys.stderr)
            return 7
        # Snapshot filenames/JSON keys are canonicalised alphabetically, but
        # the original event stream and audit lists preserve input order.
        snapshots = {symbol: snapshots[symbol] for symbol in symbol_order}
    seed = int(execution["seed"])
    np.random.seed(seed)
    random.seed(seed)
    try:
        from backtest.coin_selector import restore_selector
        candidate_selector = restore_selector(execution, snapshots, path.parent)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"Replay refused: frozen coin selector unavailable or changed: {exc}", file=sys.stderr)
        return 7
    engine = BacktestEngine(
        # Preserve the recorded numeric type too: early flat-account audit
        # rows distinguish JSON 10000 from 10000.0 in byte-exact digests.
        initial_capital=execution["capital"],
        slippage=execution.get("slippage"),
        random_slip=bool(execution.get("random_slip", False)),
        warmup_period=int(execution.get("warmup_period", 30)),
        alignment_mode=execution["alignment_mode"],
        benchmark_mode=execution["benchmark_mode"],
        benchmark_rebalance_cost_bps=float(execution["benchmark_rebalance_cost_bps"]),
        timeframe=execution["timeframe"],
        run_id=manifest["run_id"],
        account_mode=execution.get("account_mode"),
        signal_observation=execution.get("signal_observation") or {"enabled": False},
        signal_meta_layer=meta_policy,
        signal_adaptive=adaptive_policy.to_dict(),
        signal_meta_replay=meta_replay_policy.to_dict(),
        temporal_policy=replay_temporal_policy,
        temporal_financing=replay_temporal_financing,
        capital_allocation=execution.get("capital_allocation") or {"enabled": False},
        candidate_selector=candidate_selector,
    )
    result = engine.run(snapshots, routing_log_enabled=False)
    observed = deterministic_result_digest(result)
    expected = execution["result_digest"]
    research_expected = execution.get("signal_observation_digest")
    research_observed = None
    if research_expected is not None:
        from backtest.reporting.signal_observation import signal_observation_digest
        research_observed = signal_observation_digest(result.get("signal_observation"))
    meta_expected = execution.get("signal_meta_layer_digest")
    meta_observed = None
    if meta_expected is not None or result.get("signal_meta_layer") is not None:
        from backtest.reporting.signal_meta_layer import signal_meta_layer_digest
        meta_observed = signal_meta_layer_digest(result.get("signal_meta_layer"))
    additional_research = {}
    capital_expected = execution.get("capital_allocation_digest")
    capital_observed = capital_allocation_digest(result) if capital_expected is not None else None
    for name in ("signal_adaptive", "signal_meta_replay"):
        expected_digest = execution.get(f"{name}_digest")
        observed_digest = None
        if expected_digest is not None or result.get(name) is not None:
            from backtest.reporting.signal_adaptive import research_digest
            observed_digest = research_digest(result.get(name))
        additional_research[name] = (expected_digest, observed_digest)
    report = {
        "status": "passed" if (observed == expected and research_expected == research_observed
                               and meta_expected == meta_observed
                               and capital_expected == capital_observed
                               and all(pair[0] == pair[1] for pair in additional_research.values())) else "failed",
        "expected": expected,
        "observed": observed,
        "signal_observation_expected": research_expected,
        "signal_observation_observed": research_observed,
        "signal_meta_layer_expected": meta_expected,
        "signal_meta_layer_observed": meta_observed,
        "code_identity_expected": expected_code,
        "capital_allocation_expected": capital_expected,
        "capital_allocation_observed": capital_observed,
    }
    for name, (expected_digest, observed_digest) in additional_research.items():
        report[f"{name}_expected"] = expected_digest
        report[f"{name}_observed"] = observed_digest
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 8


def _temporal_report_payload(engine, results, data_map):
    """Freeze the evidence needed to restore temporal semantics after CSV load."""
    payload = {**(results.get("temporal_data") or {}), "schema": "temporal-run/v1",
               "policy": vars(parse_temporal_policy(getattr(engine, "temporal_policy", None))),
               "inputs": {}}
    adapter = getattr(engine, "market_data_adapter", None)
    histories = getattr(adapter, "_temporal_histories", {})
    for symbol, versions in histories.items():
        entry = {"identity": versions.identity}
        if not versions.store:
            entry["records"] = list(versions.records)
        raw_id = data_map[symbol].attrs.get("temporal_raw_file_snapshot")
        if raw_id:
            entry["raw_file_snapshot"] = raw_id
        payload["inputs"][symbol] = entry
    financing = getattr(engine, "temporal_financing", None)
    if financing is not None:
        from core.temporal_financing import TemporalFinancing
        if isinstance(financing, TemporalFinancing):
            payload["financing_evidence"] = financing.export()
    return payload


def _restore_temporal_financing(execution, report_dir):
    """Restore the exact frozen cost revisions, never a mutable external head."""
    expected = execution.get("temporal_financing_sha256")
    if expected is None:
        return None
    from core.signal_observation_types import fingerprint
    path = Path(report_dir) / "temporal_data.json"
    if sha256_file(path) != execution.get("temporal_data_sha256"):
        raise ValueError("temporal financing report hash mismatch")
    evidence = json.loads(path.read_text(encoding="utf-8")).get("financing_evidence")
    if evidence is None or fingerprint(evidence) != expected:
        raise ValueError("frozen temporal financing evidence missing or changed")
    return evidence


def _restore_temporal_replay_inputs(data_map, execution, report_dir):
    from core.temporal_data import TemporalDataError, TemporalOHLCV

    # Explicit compatibility policy prevents an old manifest inheriting today's
    # config. Strict replay must restore identical evidence, never re-import it.
    policy = parse_temporal_policy(execution.get("temporal_policy") or "retrospective")
    expected = execution.get("temporal_data_sha256")
    if not expected:
        if policy.mode == "strict" or policy.store_path:
            raise TemporalDataError("recorded temporal policy has no evidence digest")
        return vars(policy)
    evidence_path = Path(report_dir) / "temporal_data.json"
    if sha256_file(evidence_path) != expected:
        raise TemporalDataError("temporal report hash mismatch")
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "temporal-run/v1" or payload.get("policy") != vars(policy):
        raise TemporalDataError("temporal policy differs from recorded evidence")
    inputs = payload.get("inputs", {})
    if policy.mode == "strict" and set(inputs) != set(data_map):
        raise TemporalDataError("strict input evidence is incomplete")
    for symbol, entry in inputs.items():
        frame = data_map[symbol]
        identity = entry["identity"]
        store_path = identity.get("store_path")
        if store_path and not Path(store_path).is_dir():
            raise TemporalDataError("referenced immutable version store is missing")
        frame.attrs["temporal_identity"] = identity
        if "records" in entry:
            frame.attrs["temporal_records"] = entry["records"]
        reader = TemporalOHLCV(identity["dataset_id"], timeframe=identity["timeframe"],
                               policy={**vars(policy), "store_path": None})
        if not reader.import_identity(frame):
            raise TemporalDataError("frozen temporal records are missing")
        if entry.get("raw_file_snapshot"):
            if reader.store is None:
                raise TemporalDataError("raw CSV snapshot store is missing")
            reader.store.verify_snapshot(entry["raw_file_snapshot"])
            frame.attrs["temporal_raw_file_snapshot"] = entry["raw_file_snapshot"]
    return vars(policy)


def _build_parser() -> argparse.ArgumentParser:
    """Preserve the entrypoint API used by callers and tests."""
    return build_backtest_parser(DEFAULT_INITIAL_CAPITAL)


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


def _capital_allocation_options(args):
    """Validate per-run CLI overrides without changing the shared configuration."""
    from config.config import config
    from core.position_management import CapitalAllocationPolicy
    enabled = getattr(args, "smart_allocation", False)
    values = {key: getattr(args, key, None) for key in ("max_positions", "cash_reserve_pct")}
    if not enabled and any(value is not None for value in values.values()):
        raise ValueError("--max-positions and --cash-reserve-pct require --smart-allocation")
    if not enabled:
        return None
    policy = {**((config.get("allocation") or {}).get("capital") or {}), "enabled": True,
              **{key: value for key, value in values.items() if value is not None}}
    return asdict(CapitalAllocationPolicy.from_mapping(policy))


def _execute_backtest(args, data_map):
    """Construct the engine and run it with the requested reporting footprint."""
    print("\nInitializing Backtest Engine...")
    candidate_selector = None
    selector_identity = {"schema": "backtest-coin-selector/v1", "enabled": False,
                         "candidate": None, "new_training_updates": 0, "new_threshold_search": 0}
    if getattr(args, "coin_selector", "off") == "on":
        from backtest.coin_selector import create_selector
        candidate_selector, selector_identity = create_selector(
            data_map, initial_capital=args.capital,
            bundle_path=getattr(args, "selector_bundle", None))
    engine = BacktestEngine(
        initial_capital=args.capital,
        slippage=args.slippage,
        random_slip=args.random_slip,
        alignment_mode=args.alignment_mode,
        benchmark_mode=args.benchmark_mode,
        benchmark_rebalance_cost_bps=args.benchmark_rebalance_cost_bps,
        timeframe=args.timeframe,
        account_mode=("spot_margin" if args.market_type == "margin" else args.market_type),
        signal_observation={"enabled": True} if getattr(args, "observe_signals", False) else None,
        signal_meta_layer={"enabled": True} if getattr(args, "signal_meta_layer", False) else None,
        signal_adaptive={"enabled": True} if getattr(args, "adaptive_signal_meta", False) else None,
        signal_meta_replay={"enabled": True} if getattr(args, "signal_meta_replay", False) else None,
        temporal_policy=getattr(args, "resolved_temporal_policy", None),
        temporal_financing=getattr(args, "temporal_financing_evidence", None),
        capital_allocation=_capital_allocation_options(args),
        candidate_selector=candidate_selector,
    )
    engine.coin_selector_identity = selector_identity
    print("Running Backtest...")
    reports_dir = os.path.join(os.getcwd(), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    temp_routing_log = os.path.join(getattr(args, "output_dir", None) or reports_dir,
                                   "temp_routing_log.csv")
    results = engine.run(
        data_map,
        routing_log_path=temp_routing_log,
        routing_log_enabled=(
            not args.disable_routing_log and args.report_profile == "full"
        ),
    )
    return engine, results, temp_routing_log


def main(argv=None) -> int:
    """Parse, validate and orchestrate one backtest run."""
    parser = _build_parser()

    cli_args = list(sys.argv[1:] if argv is None else argv)
    if not cli_args:
        parser.print_help(sys.stderr)
        print(
            "\nError: no arguments supplied; use explicit CLI options "
            "(for example: --source synthetic --days 365).",
            file=sys.stderr,
        )
        return 2
    args = parser.parse_args(cli_args)

    if args.replay_manifest:
        return replay_manifest(args.replay_manifest)

    try:
        _capital_allocation_options(args)
        if args.selector_bundle and args.coin_selector != "on":
            raise ValueError("--selector-bundle requires --coin-selector on")
        if args.coin_selector == "on":
            if args.timeframe != "1d":
                raise ValueError("Coin selection currently requires --timeframe 1d")
            from backtest.coin_selector import read_bundle
            read_bundle(args.selector_bundle)
    except (TypeError, ValueError, OSError, KeyError, ImportError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.seed is not None:
        np.random.seed(args.seed)
        random.seed(args.seed)
        print(f"Random seed set to {args.seed}")

    print("Starting Quantitative Trading System...")
    print(f"Current Working Directory: {os.getcwd()}")

    try:
        start_date, end_date = _resolve_date_range(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.output_dir:
        try:
            Path(args.output_dir).mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            print(f"Output directory must be new: {exc}", file=sys.stderr)
            return 2

    print(
        f"Config: Capital={args.capital}, Symbols={args.symbols}, Source={args.source}, Slippage={args.slippage if args.slippage is not None else 'config'}, RandomSlip={args.random_slip}"
    )

    try:
        (
            data_map,
            universe_identity,
            download_started_at,
            download_completed_at,
        ) = _load_requested_data(args, start_date, end_date)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    # 1.1 Generate Data Quality Report
    print("Generating Data Quality Report...")
    # Store report in memory to save later in the specific backtest folder
    quality_report = DataHandler.generate_quality_report(data_map, output_path=None)
    for symbol, frame in data_map.items():
        if frame.attrs.get("derivatives"):
            quality_report.setdefault(symbol, {})["derivatives"] = frame.attrs["derivatives"]

    try:
        engine, results, temp_routing_log = _execute_backtest(args, data_map)
    except (TypeError, ValueError, OSError, KeyError, ImportError) as exc:
        print(f"Backtest setup failed: {exc}", file=sys.stderr)
        return 2

    if not results or results["equity_curve"].empty:
        print("\n" + "!" * 50)
        print("ERROR: Backtest failed or produced no results.")
        print("Possible causes:")
        print("1. No common timeframe found between symbols (check start/end dates).")
        print("2. Data fetching failed for some symbols.")
        print("3. Strategy produced no trades and no equity updates.")
        print("!" * 50 + "\n")
        # Cleanup temp log
        if os.path.exists(temp_routing_log):
            try:
                os.remove(temp_routing_log)
            except OSError as e:
                logger.warning("Failed to remove temp routing log %s: %s", temp_routing_log, e)
        return 4

    # 3. Generate Report
    print("\nGenerating Report...")

    # Calculate basic return for naming
    equity = results["equity_curve"]["equity"]
    total_return = (equity.iloc[-1] / equity.iloc[0]) - 1
    return_str = f"Ret{total_return * 100:.1f}pct"

    # Naming convention: YYYYMMDD_HHMMSS_{Days}d_{Syms}Syms_{Ret}pct
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    symbols_str = f"{len(args.symbols)}Syms"
    days_str = f"{args.days}d"

    folder_name = f"{timestamp}_{days_str}_{symbols_str}_{return_str}"
    output_dir = args.output_dir or os.path.join(os.getcwd(), "reports", folder_name)

    output_path = Path(output_dir)
    reporter = ReportGenerator(output_dir)
    artifact_failures = []
    try:
        write_json_report(output_path / "capital_allocation.json", {
            "policy": results.get("capital_allocation_policy"),
            "batches": results.get("capital_allocation_audit", []),
        })
        if (results.get("capital_allocation_policy") or {}).get("enabled"):
            pd.DataFrame(results.get("allocation_audit") or []).to_csv(
                output_path / "capital_allocation.csv", index=False)
    except Exception as exc:
        artifact_failures.append("capital_allocation")
        logger.exception("Failed to save capital allocation evidence: %s", exc)
    try:
        write_json_report(output_path / "temporal_data.json", _temporal_report_payload(engine, results, data_map))
    except Exception as exc:
        artifact_failures.append("temporal_data")
        logger.exception("Failed to save temporal data evidence: %s", exc)
    signal_summary = {}
    if results.get("signal_observation") is not None:
        from backtest.reporting.signal_observation import write_signal_observation_report
        signal_summary = write_signal_observation_report(results["signal_observation"], output_path)
        if signal_summary["status"] != "complete":
            artifact_failures.append("signal_observation_incomplete")
    meta_summary = {}
    if results.get("signal_meta_layer") is not None:
        from backtest.reporting.signal_meta_layer import write_signal_meta_layer_report
        meta_summary = write_signal_meta_layer_report(results["signal_meta_layer"], output_path)
        if meta_summary["status"] != "complete":
            artifact_failures.append("signal_meta_layer_incomplete")
    adaptive_summary, meta_replay_summary = {}, {}
    if results.get("signal_adaptive") is not None:
        from backtest.reporting.signal_adaptive import write_signal_adaptive_report
        adaptive_summary = write_signal_adaptive_report(
            results["signal_adaptive"], output_path, p1_payload=results.get("signal_meta_layer"))
        if adaptive_summary["status"] != "complete":
            artifact_failures.append("signal_adaptive_incomplete")
    if results.get("signal_meta_replay") is not None:
        from backtest.reporting.signal_adaptive import write_signal_meta_replay_report
        meta_replay_summary = write_signal_meta_replay_report(results["signal_meta_replay"], output_path)
        if meta_replay_summary["status"] != "complete":
            artifact_failures.append("signal_meta_replay_incomplete")

    effective_timestamps = engine.market_data_adapter.timestamps
    effective_period = {
        "start": effective_timestamps.min(),
        "end": effective_timestamps.max(),
        "bars": len(effective_timestamps),
        "alignment_mode": args.alignment_mode,
        "per_symbol": {
            symbol: {
                "start": frame.index.min(),
                "end": frame.index.max(),
                "rows": len(frame),
            }
            for symbol, frame in sorted(data_map.items())
        },
    }

    # Engines supplied by older callers may predate the CLI selector identity.
    selector_identity = getattr(engine, "coin_selector_identity", {
        "schema": "backtest-coin-selector/v1", "enabled": False,
        "candidate": None, "new_training_updates": 0, "new_threshold_search": 0})
    # Prepare metadata
    metadata = {
        "Days": args.days,
        "Start": start_date.strftime("%Y-%m-%d"),
        "End": end_date.strftime("%Y-%m-%d"),
        "Capital": args.capital,
        "Symbols": ", ".join(args.symbols),
        "Source": args.source,
        "RequestedPeriod": (
            f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
        ),
        "EffectivePeriod": (
            f"{effective_timestamps.min()} to {effective_timestamps.max()}"
        ),
        "AlignmentMode": args.alignment_mode,
        "BenchmarkMode": args.benchmark_mode,
        "Timeframe": args.timeframe,
        "MarketType": args.market_type or results.get("account_mode"),
        "AccountMode": results.get("account_mode"),
        "CoinSelector": "on" if selector_identity["enabled"] else "off",
        "CoinSelectorModelId": selector_identity.get("model_id"),
    }

    from backtest.coin_selector import write_selector_report
    selector_summary = write_selector_report(
        output_path, engine.candidate_selector, selector_identity)

    metrics = reporter.generate(
        results["trades"],
        results["equity_curve"],
        metadata=metadata,
        benchmark_curve=results.get("benchmark"),
        close_events=results.get("close_events"),
        lifecycle=results.get("lifecycle"),
        strategy_health=results.get("strategy_health"),
        protective_stops=results.get("protective_stop_summary"),
        report_profile=args.report_profile,
        data_quality=quality_report,
        # BM3: the signal->fill funnel is only derivable from the run's own
        # event stream, which nothing outside the engine has.
        event_log=results.get("event_log"),
        max_holding_days=results.get("effective_max_holding_days"),
    )


    # Daily research runs should be easy to inspect and cheap to retain.  The
    # full profile below remains the promotion/validation path with immutable
    # ledgers, event coverage and replay snapshots.
    if args.report_profile in {"workbook", "compact"}:
        if os.path.exists(temp_routing_log):
            try:
                os.remove(temp_routing_log)
            except OSError:
                logger.warning("Failed to remove temporary routing log %s", temp_routing_log)
        print("\n" + "=" * 30)
        print("BACKTEST RESULTS (core metrics)")
        print("=" * 30)
        print(format_primary_metrics(metrics, bilingual=False))
        print("=" * 30)
        report_name = (
            "backtest_report.xlsx" if args.report_profile == "workbook"
            else "report.pdf"
        )
        print(f"\n{args.report_profile.title()} report saved to: {output_path / report_name}")
        print("Use --report-profile full for ledgers, event logs and replay artifacts.")
        if artifact_failures:
            print("Research artifacts incomplete: " + ", ".join(artifact_failures), file=sys.stderr)
            return 5
        return 0

    # Phase 3 account/risk/cost ledgers are first-class mandatory audit
    # artifacts, not values buried only in the in-memory engine result.
    try:
        pd.DataFrame(results.get("margin_ledger") or []).to_csv(
            output_path / "margin_ledger.csv", index=False
        )
        pd.DataFrame(results.get("financing_ledger") or []).to_csv(
            output_path / "financing_ledger.csv", index=False
        )
        pd.DataFrame(results.get("execution_audit") or []).to_csv(
            output_path / "execution_audit.csv", index=False
        )
        pd.DataFrame(results.get("breaker_audit") or []).to_csv(
            output_path / "breaker_audit.csv", index=False
        )
        # SR1-4 deliverables: the health lifecycle and its authoritative
        # observation unit are audit artifacts, not log text.
        pd.DataFrame(results.get("strategy_health_transitions") or []).to_csv(
            output_path / "strategy_health_timeline.csv", index=False
        )
        pd.DataFrame(results.get("strategy_health_cohorts") or []).to_csv(
            output_path / "cohort_trades.csv", index=False
        )
        pd.DataFrame(results.get("risk_budget_reconciliation") or []).to_csv(
            output_path / "risk_budget_reconciliation.csv", index=False
        )
        # STR-P1-01 deliverable: the resident protective stop's intents and
        # fills, so a backtest stop can be audited exactly like a live one.
        pd.DataFrame(results.get("stop_order_audit") or []).to_csv(
            output_path / "stop_order_audit.csv", index=False
        )
        # SR3 deliverables: the ranking that actually decided allocation, and
        # the correlated-risk budget that metered it.
        pd.DataFrame(results.get("allocation_audit") or []).to_csv(
            output_path / "allocation_audit.csv", index=False
        )
        pd.DataFrame(results.get("correlated_risk_audit") or []).to_csv(
            output_path / "correlated_risk_audit.csv", index=False
        )
        write_json_report(output_path / "account_cost_contract.json", {
            **(results.get("account_cost_contract") or {}),
            "degenerate_ranking_batches": results.get(
                "degenerate_ranking_batches", 0
            ),
        })
        write_json_report(
            output_path / "strategy_health.json", results.get("strategy_health") or {}
        )
        pd.DataFrame([
            {
                "strategy": name,
                "status": entry.get("status"),
                "raw_setup_count": entry.get("raw_setup_count"),
                "suppressed_raw_setups": entry.get("suppressed_raw_setups"),
                "last_raw_setup_at": entry.get("last_raw_setup_at"),
                "last_suppressed_setup_at": entry.get("last_suppressed_setup_at"),
            }
            for name, entry in (results.get("strategy_health") or {}).items()
        ]).to_csv(output_path / "suppressed_setups.csv", index=False)
        write_json_report(output_path / "breaker_state.json", {
            "account_mode": results.get("account_mode"),
            **(results.get("breaker_state") or {}),
        })
        write_json_report(
            output_path / "backtest_lifecycle.json",
            results.get("lifecycle") or {},
        )
    except Exception as exc:
        artifact_failures.append("phase3_account_risk_audit")
        logger.exception("Failed to save Phase 3 account/risk artifacts: %s", exc)

    # T-2.7/T-2.8: preserve both benchmark definitions plus the selected
    # benchmark's weight, turnover and cost ledger for independent audit.
    try:
        if results.get("benchmark_fixed") is not None:
            results["benchmark_fixed"].to_csv(output_path / "benchmark_fixed.csv")
        if results.get("benchmark_dynamic") is not None:
            results["benchmark_dynamic"].to_csv(output_path / "benchmark_dynamic.csv")
        weights = results.get("benchmark_weights")
        if isinstance(weights, pd.DataFrame) and not weights.empty:
            weights.to_csv(output_path / "benchmark_weights.csv")
        turnover = results.get("benchmark_turnover")
        costs = results.get("benchmark_costs")
        if isinstance(turnover, pd.Series) and isinstance(costs, pd.Series):
            pd.concat([turnover.rename("turnover"), costs.rename("cost")], axis=1).to_csv(
                output_path / "benchmark_turnover_cost.csv"
            )
        write_json_report(
            output_path / "benchmark_metadata.json",
            results.get("benchmark_metadata") or {},
        )
    except Exception as exc:
        artifact_failures.append("benchmark_audit")
        logger.exception("Failed to save benchmark audit artifacts: %s", exc)

    # Save Data Quality Report
    dq_report_path = os.path.join(output_dir, "data_quality_report.json")
    try:
        with open(dq_report_path, "w", encoding="utf-8") as f:
            json.dump(quality_report, f, indent=4, default=str)
        print(f"Data quality report saved to {dq_report_path}")
    except Exception as e:
        artifact_failures.append("data_quality_report")
        logger.exception("Failed to save data quality report: %s", e)

    # Move Routing Log
    final_routing_log = os.path.join(output_dir, "routing_log.csv")
    if os.path.exists(temp_routing_log):
        try:
            shutil.move(temp_routing_log, final_routing_log)
            print(f"Routing log moved to {final_routing_log}")
        except Exception as e:
            artifact_failures.append("routing_log")
            logger.exception("Failed to move routing log: %s", e)

    # T-2.9: event pipeline audit is mandatory for a normal backtest.  If
    # trading occurred, every signal/risk/order/fill/close stage must exist.
    try:
        event_summary = write_event_log(
            results.get("event_log") or (), output_path / "event_log.jsonl"
        )
        audit_coverage = validate_audit_coverage(
            event_summary=event_summary,
            routing_log_path=final_routing_log,
            routing_required=not args.disable_routing_log,
            trade_count=len(results.get("trades") or []),
            close_count=sum((results.get("close_events") or {}).values()),
        )
        if audit_coverage["status"] != "ok":
            artifact_failures.append("event_audit_coverage")
    except Exception as exc:
        event_summary = {"status": "failed", "error": str(exc)}
        audit_coverage = {"status": "failed", "missing_event_types": ["event_log"]}
        artifact_failures.append("event_log")
        logger.exception("Failed to save mandatory event audit: %s", exc)

    # T-2.11: never pretend the primary feed verified itself.  Without an
    # independent directory this report stays explicitly UNVERIFIED.
    try:
        closed_trades = reporter._reconstruct_closed_trades(
            pd.DataFrame(results.get("trades") or [])
        )
        secondary_data = (
            _load_secondary_data(args.secondary_data_dir, data_map)
            if args.secondary_data_dir else None
        )
        market_data_audit = cross_verify_top_trades(
            closed_trades, data_map, secondary_data, top_n=20
        )
        write_json_report(output_path / "top_trade_market_data_audit.json", market_data_audit)
        if args.require_secondary_audit and market_data_audit["status"] != "passed":
            artifact_failures.append("secondary_data_audit")
    except Exception as exc:
        market_data_audit = {"status": "failed", "error": str(exc)}
        artifact_failures.append("secondary_data_audit")
        logger.exception("Failed top-trade market data audit: %s", exc)

    # T-2.1--T-2.5/T-2.13: snapshot exact engine inputs and write the complete
    # identity only after every auditable output exists.
    try:
        snapshot_entries = save_data_snapshots(data_map, output_path / "data_inputs")
        requested_period = {
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d"),
            "days": args.days,
        }
        identity = data_identity(
            data_map,
            source=args.source,
            exchange=args.exchange if args.source == "ccxt" else None,
            market_type=args.market_type or results.get("account_mode"),
            timeframe=args.timeframe,
            timezone_name=args.data_timezone,
            downloaded_at=download_completed_at,
        )
        identity["download_started_at"] = download_started_at
        identity["universe"] = universe_identity
        artifact_names = [
            "metrics.json", "closed_trades.csv", "reconciliation.json", "execution_quality.json", "invalid_closed_trades.json",
            "equity.csv", "trades.csv", "report.txt", "benchmark.csv",
            "benchmark_fixed.csv", "benchmark_dynamic.csv", "benchmark_weights.csv",
            "benchmark_turnover_cost.csv", "benchmark_metadata.json",
            "routing_log.csv", "event_log.jsonl", "data_quality_report.json",
            "top_trade_market_data_audit.json",
            "margin_ledger.csv", "financing_ledger.csv", "execution_audit.csv",
            "breaker_audit.csv", "breaker_state.json", "backtest_lifecycle.json",
            "temporal_data.json",
        ]
        artifact_names.extend(signal_summary.get("artifacts", []))
        artifact_names.append("capital_allocation.json")
        if (results.get("capital_allocation_policy") or {}).get("enabled"):
            artifact_names.append("capital_allocation.csv")
        artifact_names.extend(meta_summary.get("artifacts", []))
        artifact_names.extend(adaptive_summary.get("artifacts", []))
        artifact_names.extend(meta_replay_summary.get("artifacts", []))
        artifact_names.append("coin_selector.json")
        if selector_summary["enabled"]:
            artifact_names.append("coin_selection.csv")
        execution_identity = {
            **runtime_identity(),
            "capital": args.capital,
            "coin_selector": selector_summary,
            "data_symbol_order": list(data_map),
            "seed": args.seed,
            "slippage": engine.slippage,
            "random_slip": args.random_slip,
            "warmup_period": engine.warmup_period,
            "capital_allocation": asdict(engine.capital_allocation_policy),
            "capital_allocation_digest": capital_allocation_digest(results),
            "signal_observation": results["signal_observation"]["policy"] if results.get("signal_observation") else None,
            "signal_observation_digest": signal_summary.get("research_payload_sha256"),
            "signal_meta_layer": engine.signal_meta_policy.to_dict(),
            "signal_meta_layer_digest": meta_summary.get("research_payload_sha256"),
            "signal_meta_layer_artifacts": meta_summary.get("artifacts", []),
            "signal_adaptive": engine.signal_adaptive_policy.to_dict(),
            "signal_adaptive_digest": adaptive_summary.get("research_payload_sha256"),
            "signal_adaptive_artifacts": adaptive_summary.get("artifacts", []),
            "signal_meta_replay": engine.signal_meta_replay_policy.to_dict(),
            "signal_meta_replay_digest": meta_replay_summary.get("research_payload_sha256"),
            "signal_meta_replay_artifacts": meta_replay_summary.get("artifacts", []),
            "alignment_mode": args.alignment_mode,
            "benchmark_mode": args.benchmark_mode,
            "benchmark_rebalance_cost_bps": args.benchmark_rebalance_cost_bps,
            "timeframe": args.timeframe,
            "account_mode": results.get("account_mode"),
            "routing_log_enabled": not args.disable_routing_log,
            "result_digest": deterministic_result_digest(results),
            "temporal_policy": vars(parse_temporal_policy(getattr(engine, "temporal_policy", None))),
            "temporal_data_sha256": sha256_file(output_path / "temporal_data.json"),
        }
        if getattr(engine, "temporal_financing", None) is not None:
            from core.temporal_financing import TemporalFinancing
            from core.signal_observation_types import fingerprint
            if isinstance(engine.temporal_financing, TemporalFinancing):
                execution_identity["temporal_financing_sha256"] = fingerprint(engine.temporal_financing.export())
        manifest = build_run_manifest(
            run_id=results["run_id"],
            repo_root=Path(__file__).resolve().parent,
            config_path=Path(__file__).resolve().parent / "config" / "params.yaml",
            requested_period=requested_period,
            effective_period=effective_period,
            data=identity,
            snapshots=snapshot_entries,
            execution=execution_identity,
            artifacts=artifact_hashes(output_path, artifact_names),
            audit={
                "event_log": event_summary,
                "coverage": audit_coverage,
                "top_trade_market_data": market_data_audit,
            },
        )
        write_manifest(output_path / "run_manifest.json", manifest)
    except Exception as exc:
        artifact_failures.append("run_manifest")
        logger.exception("Failed to create reproducible run manifest: %s", exc)

    print("\n" + "=" * 30)
    print("BACKTEST RESULTS (core metrics)")
    print("=" * 30)
    # English-only on stdout: Windows consoles commonly default to the GBK
    # codepage, which garbles Chinese text; report.txt is written with an
    # explicit utf-8 encoding and carries the bilingual labels instead.
    print(format_primary_metrics(metrics, bilingual=False))
    print("=" * 30)
    print("Full metrics (drawdown events, trade quality, attribution, benchmark) written to report.txt")

    print(f"\nReport saved to: {output_dir}")
    if artifact_failures:
        print(
            "Report completed with artifact failures: "
            + ", ".join(artifact_failures),
            file=sys.stderr,
        )
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
