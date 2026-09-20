"""Freeze the actual controlled working tree and compare three offline processes.

This accepts an explicitly hashed dirty-tree snapshot; main_acceptance retains
its stricter clean-commit gate. No exchange, holdout, commit or live deployment.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (".github", "analysis", "backtest", "composition", "config", "core", "dashboard",
               "data", "live_trading", "research", "router", "scripts", "strategies", "tests")


def source_manifest(root: Path) -> dict:
    paths = set(root.glob("*.py")) | set(root.glob("*.toml")) | set(root.glob("requirements*"))
    paths.update(root / name for name in (".gitattributes", ".gitignore", ".python-version") if (root / name).is_file())
    for folder in SOURCE_DIRS:
        for path in (root / folder).rglob("*"):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            if path.suffix in {".py", ".ps1"} or (folder in {"config", "tests", ".github"} and path.suffix in {".json", ".yaml", ".yml", ".csv"}):
                paths.add(path)
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.is_file()}


def verify_source(root: Path, expected: dict) -> None:
    actual = source_manifest(root)
    changed = [name for name in sorted(set(actual) | set(expected)) if actual.get(name) != expected.get(name)]
    if changed:
        raise ValueError("Frozen input identity changed: " + ", ".join(changed))


def save(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def worker(output: Path) -> None:
    sys.path.insert(0, str(ROOT))
    import pandas as pd
    from backtest.engine import BacktestEngine
    from backtest.reporting import ReportGenerator
    from backtest.reporting.serialization import metrics_document
    from config.config import config
    from core.broker import Broker
    from core.portfolio import Portfolio
    from tests.engine_baseline_harness import build_synthetic_data_map, DEFAULT_WARMUP_PERIOD, data_digest

    def clean(value):
        if isinstance(value, pd.DataFrame):
            return clean(value.rename_axis("timestamp").reset_index().to_dict("records"))
        if isinstance(value, pd.Series):
            return clean(value.rename_axis("timestamp").reset_index().to_dict("records"))
        if is_dataclass(value) and not isinstance(value, type):
            return clean(asdict(value))
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if isinstance(value, Enum):
            return value.value
        return value

    data = build_synthetic_data_map()
    data_records = {name: json.loads(frame.reset_index().to_json(orient="records", date_format="iso"))
                    for name, frame in data.items()}
    engine = BacktestEngine(initial_capital=10000., slippage=.0005, random_slip=False,
                            warmup_period=DEFAULT_WARMUP_PERIOD)
    result = engine.run(data, routing_log_enabled=False)
    if not result["accounting_check"]["ok"] or result["accounting_check"]["checks_performed"] <= 0:
        raise AssertionError("synthetic accounting reconciliation failed")
    import numpy as np
    if result["equity_curve"].empty or not np.isfinite(result["equity_curve"][["equity", "cash"]].to_numpy()).all():
        raise AssertionError("synthetic equity/cash facts must be finite")
    reporter = ReportGenerator.__new__(ReportGenerator)
    legs = reporter._reconstruct_closed_trades(pd.DataFrame(result["trades"]))
    events = []
    for envelope in result["event_log"]:
        # run_id and envelope ID are random audit transport IDs. Business
        # payload IDs, event order and timestamp remain in the comparison.
        if envelope.event_type in {"order", "order_intent", "fill"}:
            events.append({"event_type": envelope.event_type, "payload": clean(envelope.payload)})
    scenario = {"input_sha256": data_digest(data_records), "input": data_records,
                "orders_and_fills": events, "trades": result["trades"],
                "closed_trades": reporter._aggregate_round_trips(legs),
                "equity": clean(result["equity_curve"]), "health": result["strategy_health"],
                "health_transitions": result["strategy_health_transitions"],
                "accounting_check": result["accounting_check"]}
    # Hand-calculable production Broker path: 10 units at 100 -> 110,
    # fees 1 + 1.1, so cash/equity and net realized gain must be 10097.9.
    portfolio = Portfolio(10000.)
    broker = Broker(portfolio, commission_rate=.001, slippage=0., max_participation_rate=1.)
    for side, price, day in (("buy", 100., 1), ("sell", 110., 2)):
        broker.submit_order("TEST/USDT", side, 10., price=price, order_type="market",
                            timestamp=pd.Timestamp(f"2024-02-0{day}"), strategy_id="Hand")
        broker.process_orders({"TEST/USDT": pd.Series(dict(open=price, high=price+1,
            low=price-1, close=price, volume=1000.), name=pd.Timestamp(f"2024-02-0{day+1}"))})
    if abs(portfolio.cash - 10097.9) > 1e-8:
        raise AssertionError(f"hand accounting mismatch: {portfolio.cash}")
    hand = {"trades": broker.trades, "cash": portfolio.cash, "positions": portfolio.positions,
            "closed_trades": reporter._aggregate_round_trips(reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades)))}
    if len(hand["closed_trades"]) != 1 or abs(hand["closed_trades"][0]["net_pnl"] - 97.9) > 1e-8:
        raise AssertionError("hand closed PnL does not reconcile with cash")
    empty = BacktestEngine(initial_capital=10000.).run({"TEST/USDT": pd.DataFrame()})
    no_trade = {key: clean(empty[key]) for key in ("trades", "equity_curve", "entry_observations", "accounting_check")}
    output.mkdir(parents=True, exist_ok=False)
    save(output / "resolved_config.json", config._config)
    document = metrics_document(clean({"synthetic": scenario, "hand_calculable": hand, "no_trade": no_trade}))
    save(output / "facts.json", {**document["metrics"], "conversion_diagnostics": document["nonfinite_values"]})


def run(output: Path) -> int:
    output.mkdir(parents=True, exist_ok=False)
    manifest = source_manifest(ROOT)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    identity = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    save(output / "source_manifest.json", {"source_revision": revision, "source_sha256": identity, "files": manifest,
        "policy": "controlled source including untracked Python; config, fixture inputs and dependency locks; excludes reports/docs/caches/credentials"})
    snapshot = output / "source"
    for name in manifest:
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    checks = []
    for number in range(1, 4):
        verify_source(snapshot, manifest)
        destination = output / f"run_{number}"
        with (output / f"run_{number}.log").open("wb") as handle:
            result = subprocess.run([sys.executable, str(snapshot / "scripts/roadmap_baseline.py"),
                "worker", "--output", str(destination)], cwd=snapshot, stdout=handle,
                stderr=subprocess.STDOUT, env={**os.environ, "PYTHONIOENCODING": "utf-8", "QUANT_SANDBOX_E2E": "0"})
        checks.append({"name": f"process_{number}", "exit_code": result.returncode})
        if result.returncode:
            break
    verify_source(snapshot, manifest)
    passed = len(checks) == 3 and all(row["exit_code"] == 0 for row in checks)
    hashes = {}
    if passed:
        for name in ("facts.json", "resolved_config.json"):
            hashes[name] = [hashlib.sha256((output / f"run_{n}" / name).read_bytes()).hexdigest() for n in range(1, 4)]
            passed = passed and len(set(hashes[name])) == 1
    save(output / "acceptance.json", {"task_id": "SYS-01", "run_id": output.name,
        "source_revision": revision, "source_sha256": identity, "checks": checks, "artifact_hashes": hashes,
        "engineering_status": "pass" if passed else "fail", "research_status": "not_applicable",
        "operational_status": "pending", "limitations": ["Offline frozen-worktree reproducibility only; no remote CI, exchange or admission evidence."]})
    print(json.dumps({"passed": passed, "source_sha256": identity, "output": str(output)}))
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "worker"))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "worker":
        worker(args.output.resolve())
        return 0
    return run(args.output.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
