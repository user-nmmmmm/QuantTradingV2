"""
批量回测矩阵：多币种 × 多周期 × 多时间窗，汇总核心指标到一张表。

做什么
------
1. 对每个周期，调用 ``scripts/fetch_binance_data.py`` 把所需币种的本地缓存补齐
   （增量，已有的不会重下）。
2. 对每个 (周期 × 时间窗) 组合，用 ``main.py --source local`` 跑一次回测。
3. 解析每次运行的 ``report.txt``，把总收益 / CAGR / 回撤 / Sharpe / PF / 交易数 / 胜率
   汇总到 ``outputs/backtest_matrix/<时间戳>/summary.csv`` 和 ``summary.md``。

为什么可以扫周期
----------------
``core/metrics.py`` 的年化因子由 equity 序列的时间间隔自动推断（``infer_periods_per_year``），
Sharpe / CAGR 会随周期自适应；引擎的 ``timeframe`` 只作为口径与 manifest 身份。

用法
----
    # 默认：扩展到 ~28 个币种，周期 1d/4h/1h，4 个时间窗
    python scripts/run_backtest_matrix.py

    # 只跑日线、只跑全样本窗
    python scripts/run_backtest_matrix.py --timeframes 1d --windows full

    # 自定义
    python scripts/run_backtest_matrix.py --timeframes 1d 4h --symbols BTC/USDT ETH/USDT SOL/USDT \
        --windows full bull2021 bear2022 --capital 100000

    # 已经拉过数据，跳过下载
    python scripts/run_backtest_matrix.py --skip-fetch
"""

from __future__ import annotations

import argparse
import hashlib
import csv
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.timeframes import timeframe_delta  # noqa: E402

PY = sys.executable

# --- 扩展币种池：30 个币安 USDT 交易对，涵盖不同上市年份和板块 ------------------
# 每个标的从它自己在币安的首根 K 线开始（union 对齐，早于上市的时间点不参与）。
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "SOL/USDT",
    "DOGE/USDT", "AVAX/USDT", "DOT/USDT", "LTC/USDT", "LINK/USDT", "TRX/USDT",
    "MATIC/USDT", "ATOM/USDT", "UNI/USDT", "ETC/USDT", "XLM/USDT", "BCH/USDT",
    "FIL/USDT", "NEAR/USDT", "ICP/USDT", "AAVE/USDT", "APT/USDT", "ARB/USDT",
    "OP/USDT", "INJ/USDT", "SUI/USDT", "SEI/USDT", "TIA/USDT", "LDO/USDT",
]

DEFAULT_TIMEFRAMES = ["1d", "4h", "1h"]

# 时间窗：name -> (start, end)。end=None 表示到今天。
WINDOWS: dict[str, tuple[str, str | None]] = {
    "full": ("2017-07-01", None),  # 币安上线首月；每个币从自己首根 K 线起算
    "bull2021": ("2020-10-01", "2021-11-30"),
    "bear2022": ("2021-11-01", "2022-12-31"),
    "recent": ("2023-01-01", None),
}

# 高频周期不需要拉到 2017（数据量爆炸也没意义），按周期给个抓取起点下限
FETCH_FLOOR = {"1h": "2020-01-01", "4h": "2019-01-01", "15m": "2022-01-01", "5m": "2023-01-01"}

CORE_METRIC_KEYS = {
    "Total Return": "total_return",
    "CAGR": "cagr",
    "Max Drawdown %": "max_dd",
    "Sharpe Ratio": "sharpe",
    "Total Trades": "trades",
    "Win Rate": "win_rate",
    "Profit Factor": "profit_factor",
    "Net PnL": "net_pnl",
    "End Equity": "end_equity",
}


def safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace("-", "_").replace(":", "_")


def verify_cache(symbols: list[str], timeframe: str, *, generated_after: datetime | None = None,
                 start: str | None = None, end: str | None = None) -> dict:
    directory = REPO_ROOT / "data" / "binance" / timeframe
    manifest = json.loads((directory / "_manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "binance-cache/v2" or manifest.get("provider") != "binance"
            or manifest.get("market_type") != "spot"
            or manifest.get("timeframe") != timeframe or manifest.get("failures")):
        raise ValueError("Cache has unverified source, timeframe, or refresh failures")
    if generated_after is not None and datetime.fromisoformat(manifest["generated_at"]) < generated_after:
        raise ValueError("Refresh left a stale cache manifest")
    for symbol in symbols:
        record = manifest.get("symbols", {}).get(symbol, {})
        if (record.get("provider") != "binance" or record.get("timeframe") != timeframe
                or record.get("market_type") != "spot"
                or record.get("symbol") != symbol.strip().upper().replace("/", "-").replace("_", "-")
                or record.get("coverage", {}).get("status") != "complete"):
            raise ValueError(f"Incomplete or unverified cache: {symbol}")
        path = (directory / record["file"]).resolve()
        if not path.is_relative_to(directory.resolve()) or not path.is_file():
            raise ValueError(f"Invalid cache file: {symbol}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"):
            raise ValueError(f"Cache hash mismatch: {symbol}")
        if start is not None:
            required_start = pd.to_datetime(start, utc=True)
            if (pd.to_datetime(record["requested_start"], utc=True) > required_start
                    or pd.to_datetime(record["first"], utc=True) > required_start):
                raise ValueError(f"Cache does not cover requested start for {symbol}: {start}")
        if end is not None:
            delta = pd.Timedelta(timeframe_delta(timeframe))
            required_end = min(pd.to_datetime(end, utc=True) + pd.Timedelta(days=1),
                               pd.Timestamp.now(tz="UTC").floor(delta))
            if (pd.to_datetime(record["requested_end"], utc=True) + pd.Timedelta(days=1) < required_end
                    or pd.to_datetime(record["coverage"]["effective_end_exclusive"], utc=True) < required_end
                    or pd.to_datetime(record["last"], utc=True) + delta < required_end):
                raise ValueError(f"Cache does not cover requested end for {symbol}: {end}")
    return manifest


def fetch_for_timeframe(symbols: list[str], timeframe: str, start: str, end: str | None = None) -> dict:
    floor = FETCH_FLOOR.get(timeframe)
    eff_start = max(start, floor) if floor else start
    cmd = [
        PY, str(REPO_ROOT / "scripts" / "fetch_binance_data.py"),
        "--timeframe", timeframe, "--start", eff_start,
        "--symbols", *symbols,
    ]
    end = end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cmd += ["--end", end]
    print(f"[fetch] {timeframe} from {eff_start} ({len(symbols)} symbols) ...", flush=True)
    started = datetime.now(timezone.utc)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)
    return verify_cache(symbols, timeframe, generated_after=started, start=eff_start, end=end)


def newest_report_dir(before: set[Path]) -> Path | None:
    now = {p for p in (REPO_ROOT / "reports").glob("2*_*Syms_*") if p.is_dir()}
    fresh = now - before
    if not fresh:
        return None
    return max(fresh, key=lambda p: p.stat().st_mtime)


def parse_report(report_dir: Path) -> dict:
    metric_path = report_dir / "metrics.json"
    if metric_path.exists():
        payload = json.loads(metric_path.read_text(encoding="utf-8"))
        values = payload.get("metrics", payload)
        keys = {"TotalReturn": "total_return", "CAGR": "cagr", "MaxDrawdownPct": "max_dd",
                "SharpeRatio": "sharpe", "TotalTrades": "trades", "WinRate": "win_rate",
                "ProfitFactor": "profit_factor", "NetPnL": "net_pnl", "EndEquity": "end_equity"}
        return {target: values[source].get("value") if isinstance(values[source], dict) else values[source]
                for source, target in keys.items() if source in values}
    txt = (report_dir / "report.txt").read_text(encoding="utf-8", errors="replace")
    out: dict = {}
    # Core Metrics 段：形如 "Total Return (总收益率)     : 0.1704"
    for raw_key, col in CORE_METRIC_KEYS.items():
        m = re.search(rf"^{re.escape(raw_key)}\s*\([^)]*\)\s*:\s*([+-]?(?:\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+|inf(?:inity)?|nan))\b", txt, re.M | re.I)
        if m:
            out[col] = float(m.group(1))
    # 有效样本区间
    m = re.search(r"Start:\s*(\S+)\s+End:\s*(\S+)", txt) or re.search(r"Start:\s*(\S+)", txt)
    return out


def display_metric(value, spec: str) -> str:
    return "N/A" if value is None else format(value, spec)


def run_one(symbols: list[str], timeframe: str, start: str, end: str | None,
            capital: float, seed: int, market_type: str, *, report_root: Path | None = None,
            report_profile: str = "full") -> dict:
    before = {p for p in (REPO_ROOT / "reports").glob("2*_*Syms_*") if p.is_dir()}
    cli_syms = [safe_symbol(s).replace("_", "-") for s in symbols]  # BTC/USDT -> BTC-USDT
    end = end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cmd = [
        PY, str(REPO_ROOT / "main.py"),
        "--source", "local", "--data-dir", f"data/binance/{timeframe}",
        "--symbols", *cli_syms,
        "--start", start, "--end", end,
        "--timeframe", timeframe,
        "--capital", str(capital), "--seed", str(seed),
        "--disable-routing-log",
    ]
    if market_type:
        cmd += ["--market-type", market_type]
    cmd += ["--report-profile", report_profile]
    if report_root is not None:
        cmd += ["--output-dir", str(report_root)]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    rec = {
        "timeframe": timeframe, "window_start": start, "window_end": end,
        "n_symbols": len(symbols), "exit_code": proc.returncode,
    }
    # An explicitly assigned cell must never pick up a concurrent run's report.
    rdir = (report_root if report_root.is_dir() else None) if report_root is not None else newest_report_dir(before)
    if proc.returncode != 0 or rdir is None:
        rec["error"] = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["unknown"]
        rec["error"] = rec["error"][0][:300]
        return rec
    rec["report_dir"] = str(rdir.resolve())
    try:
        rec.update(parse_report(rdir))
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"parse failed: {exc}"
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--timeframes", nargs="+", default=DEFAULT_TIMEFRAMES)
    ap.add_argument("--windows", nargs="+", default=list(WINDOWS),
                    help=f"任选：{', '.join(WINDOWS)}")
    ap.add_argument("--capital", type=float, default=100000.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--market-type", default="", choices=["", "spot", "margin", "perpetual"])
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--output-dir", type=Path, help="New exclusive matrix directory")
    ap.add_argument("--report-profile", choices=["full", "compact", "workbook"], default="full")
    ap.add_argument("--start", help="Explicit common-history lower bound for this matrix")
    ap.add_argument("--per-symbol", action="store_true",
                    help="每个币种单独跑一次回测（而不是一个组合），用于看单币贡献")
    args = ap.parse_args(argv)

    unknown = [w for w in args.windows if w not in WINDOWS]
    if unknown:
        ap.error(f"未知时间窗 {unknown}；可选：{list(WINDOWS)}")
    for label, values in (("symbols", args.symbols), ("timeframes", args.timeframes), ("windows", args.windows)):
        if len(values) != len(set(values)):
            ap.error(f"duplicate {label} are not separate experiments")
    if args.start:
        try:
            datetime.strptime(args.start, "%Y-%m-%d")
        except ValueError:
            ap.error("--start must be YYYY-MM-DD")
        if any(args.start > (WINDOWS[w][1] or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
               for w in args.windows):
            ap.error("--start is later than a selected window's end")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out_dir = args.output_dir or REPO_ROOT / "outputs" / "backtest_matrix" / stamp
    out_dir.mkdir(parents=True, exist_ok=False)

    cache_identities = {}
    earliest = min(WINDOWS[w][0] for w in args.windows)
    if args.start:
        earliest = max(earliest, args.start)
    latest = max(WINDOWS[w][1] or datetime.now(timezone.utc).strftime("%Y-%m-%d") for w in args.windows)
    try:
        for tf in args.timeframes:
            required_start = max(earliest, FETCH_FLOOR.get(tf, earliest))
            cache_identities[tf] = (verify_cache(args.symbols, tf, start=required_start, end=latest) if args.skip_fetch
                                    else fetch_for_timeframe(args.symbols, tf, earliest, latest))
    except (subprocess.CalledProcessError, ValueError, OSError, KeyError) as exc:
        (out_dir / "refresh_failure.json").write_text(json.dumps({
            "status": "failed", "reason": str(exc), "symbols": args.symbols,
            "backtests_started": 0,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    (out_dir / "cache_identities.json").write_text(json.dumps(cache_identities, indent=2), encoding="utf-8")

    # 每个 job：(label, symbol_list)
    if args.per_symbol:
        jobs = [(s, [s]) for s in args.symbols]
    else:
        jobs = [("ALL", list(args.symbols))]

    results: list[dict] = []
    total = len(args.timeframes) * len(args.windows) * len(jobs)
    i = 0
    for tf in args.timeframes:
        for w in args.windows:
            s, e = WINDOWS[w]
            for label, syms in jobs:
                i += 1
                s = max(s, args.start or s, FETCH_FLOOR.get(tf, s))
                print(f"\n=== [{i}/{total}] tf={tf} window={w} ({s}..{e or 'today'}) subject={label} ===", flush=True)
                rec = run_one(syms, tf, s, e, args.capital, args.seed, args.market_type,
                              report_root=out_dir / f"cell_{i:04}", report_profile=args.report_profile)
                rec["window"] = w
                rec["subject"] = label
                results.append(rec)
                tag = rec.get("error") or f"Ret {display_metric(rec.get('total_return'), '+.2%')}  Sharpe {display_metric(rec.get('sharpe'), '.2f')}  DD {display_metric(rec.get('max_dd'), '.2%')}  Trades {rec.get('trades', '?')}"
                print(f"    -> {tag}", flush=True)

    # --- 汇总输出 -----------------------------------------------------------
    cols = ["subject", "timeframe", "window", "window_start", "window_end", "n_symbols",
            "total_return", "cagr", "max_dd", "sharpe", "profit_factor",
            "trades", "win_rate", "net_pnl", "end_equity", "exit_code",
            "report_dir", "error"]
    csv_path = out_dir / "summary.csv"
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2,
                                                    allow_nan=False), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        for r in results:
            wr.writerow({c: r.get(c, "") for c in cols})

    (out_dir / "config.json").write_text(json.dumps({
        "symbols": args.symbols, "timeframes": args.timeframes, "windows": args.windows,
        "capital": args.capital, "seed": args.seed, "market_type": args.market_type,
        "per_symbol": args.per_symbol, "report_profile": args.report_profile,
        "start": args.start,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# 回测矩阵 {stamp}",
        "",
        f"- 币种：{len(args.symbols)} 个",
        f"- 周期：{', '.join(args.timeframes)}",
        f"- 时间窗：{', '.join(args.windows)}",
        f"- 初始资金：{args.capital:,.0f}  seed={args.seed}  account={args.market_type or 'config'}",
        "",
        "| 标的 | 周期 | 时间窗 | 总收益 | CAGR | 最大回撤 | Sharpe | PF | 交易数 | 胜率 |",
        "|---|---|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    # per-symbol 模式下按总收益排序，组合模式保持原顺序
    ordered = results
    if args.per_symbol:
        ordered = sorted(results, key=lambda r: (r.get("window", ""), -(r.get("total_return") or -1e9)))
    for r in ordered:
        if r.get("error"):
            lines.append(f"| {r.get('subject','')} | {r['timeframe']} | {r['window']} | ERROR: {r['error'][:50]} | | | | | | |")
            continue
        lines.append(
            f"| {r.get('subject','')} | {r['timeframe']} | {r['window']} "
            f"| {display_metric(r.get('total_return'), '+.2%')} "
            f"| {display_metric(r.get('cagr'), '+.2%')} "
            f"| {display_metric(r.get('max_dd'), '.2%')} "
            f"| {display_metric(r.get('sharpe'), '.2f')} "
            f"| {display_metric(r.get('profit_factor'), '.2f')} "
            f"| {display_metric(r.get('trades'), 'g')} "
            f"| {display_metric(r.get('win_rate'), '.1%')} |"
        )
    # Cache coverage is a selected input interval, not independent listing evidence.
    inception: dict[str, dict[str, str]] = {}
    for tf in args.timeframes:
        mpath = REPO_ROOT / "data" / "binance" / tf / "_manifest.json"
        if not mpath.exists():
            continue
        meta = json.loads(mpath.read_text(encoding="utf-8"))
        for sym, info in meta.get("symbols", {}).items():
            inception.setdefault(sym, {})[tf] = info.get("first", "")[:10]
    if inception:
        lines += ["", "## 本次缓存的首根 K 线（不代表实际上线日期）", "",
                  "| 币种 | " + " | ".join(args.timeframes) + " | 行数(首周期) |",
                  "|---|" + "|".join(["--:"] * (len(args.timeframes) + 1)) + "|"]
        first_tf = args.timeframes[0]
        fm_path = REPO_ROOT / "data" / "binance" / first_tf / "_manifest.json"
        rows_map = {}
        if fm_path.exists():
            rows_map = {s: i.get("rows", "") for s, i in
                        json.loads(fm_path.read_text(encoding="utf-8")).get("symbols", {}).items()}
        for sym in args.symbols:
            cells = [inception.get(sym, {}).get(tf, "-") for tf in args.timeframes]
            lines.append(f"| {sym} | " + " | ".join(cells) + f" | {rows_map.get(sym, '-')} |")

    md_path = out_dir / "summary.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n汇总写入：\n  {csv_path}\n  {md_path}")
    print("\n" + "\n".join(lines))
    failures = [r for r in results if r.get("error") or r.get("exit_code")]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
