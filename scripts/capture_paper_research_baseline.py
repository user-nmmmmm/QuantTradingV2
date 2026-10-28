"""Capture PR25-01 worktree/environment identities and a hand-calculable account.

This offline engineering fixture does not certify alpha, full historical search
or release approval. Existing output directories are deliberately never reused.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import math
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
CI_PYTHON = "3.11"
FIXTURE_CONFIG = {
    "schema": "paper-research-account-fixture/v1",
    "symbol": "PR25/USDT",
    "initial_capital": 10000.0,
    "commission_rate": 0.001,
    "slippage": 0.0,
    "random_slip": False,
    "max_participation_rate": 1.0,
    "absolute_tolerance": 1e-8,
    "account_mode": "spot",
    "timeframe": "1d",
}
FIXTURE_DATA = {
    "schema": "paper-research-fixed-bars/v1",
    "bars": [
        {
            "timestamp": "2024-01-02T00:00:00+00:00",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.0,
            "volume": 1000.0,
        },
        {
            "timestamp": "2024-01-03T00:00:00+00:00",
            "open": 110.0,
            "high": 111.0,
            "low": 109.0,
            "close": 110.0,
            "volume": 1000.0,
        },
        {
            "timestamp": "2024-01-04T00:00:00+00:00",
            "open": 105.0,
            "high": 106.0,
            "low": 104.0,
            "close": 105.0,
            "volume": 1000.0,
        },
    ],
    "orders": [
        {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "side": "buy",
            "qty": 2.0,
            "reference_price": 100.0,
            "fill_bar": 0,
        },
        {
            "timestamp": "2024-01-02T00:00:00+00:00",
            "side": "sell",
            "qty": 1.0,
            "reference_price": 110.0,
            "fill_bar": 1,
        },
    ],
}
# Independent arithmetic: buy cash = 10000 - 2*100 - .20; sale adds 110-.11.
# Closed unit = 110-100-.10-.11 = 9.79; remaining entry fee = .10.
EXPECTED_ROWS = [
    {
        "cash": 9799.8,
        "qty": 2.0,
        "fees": 0.2,
        "equity": 9999.8,
        "realized_net_pnl": 0.0,
        "unrealized_net_pnl": -0.2,
    },
    {
        "cash": 9909.69,
        "qty": 1.0,
        "fees": 0.31,
        "equity": 10019.69,
        "realized_net_pnl": 9.79,
        "unrealized_net_pnl": 9.9,
    },
    {
        "cash": 9909.69,
        "qty": 1.0,
        "fees": 0.31,
        "equity": 10014.69,
        "realized_net_pnl": 9.79,
        "unrealized_net_pnl": 4.9,
    },
]


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _write(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _command(root: Path, args: list[str], timeout: int = 60) -> dict:
    try:
        result = subprocess.run(args, cwd=root, capture_output=True, timeout=timeout)
        return {
            "command": args,
            "exit_code": result.returncode,
            "stdout": result.stdout.decode("utf-8", errors="replace").strip(),
            "stderr": result.stderr.decode("utf-8", errors="replace").strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": args, "exit_code": None, "error_type": type(exc).__name__}


def capture_current_state(root: Path) -> dict:
    """Bind actual controlled bytes, including untracked source, without raw patches."""
    from analysis.research_evidence import source_identity

    files = source_identity(root)
    revision = _command(root, ["git", "rev-parse", "HEAD"])
    diff = _command(root, ["git", "diff", "--no-ext-diff", "--name-status", "HEAD"])
    untracked = _command(root, ["git", "ls-files", "--others", "--exclude-standard", "-z"])
    names = untracked.get("stdout", "").split("\0")
    actual_untracked = {name: files[name] for name in names if name in files}
    return {
        "schema": "paper-research-current-state/v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "head": revision.get("stdout") if revision.get("exit_code") == 0 else None,
        "git_status": "available" if revision.get("exit_code") == 0 else "unavailable",
        "controlled_difference": {
            "name_status": diff.get("stdout", "").splitlines(),
            "exit_code": diff.get("exit_code"),
            "policy": "path/status only; controlled bytes hashed below; no raw patch saved",
        },
        "untracked_source_hashes": actual_untracked,
        "source_hashes": files,
        "source_manifest_sha256": _digest(files),
        "config_sha256": _digest(FIXTURE_CONFIG),
        "data_manifest_sha256": _digest(FIXTURE_DATA),
        "input_identity_encoding": "sha256 of sorted compact UTF-8 JSON, without trailing newline",
        "scope": "finite current worktree; not complete historical search or clean-commit acceptance",
    }


def capture_environment(root: Path, *, run_checks: bool = True) -> dict:
    """Record dependency versions and actual local/CI runtime differences."""
    versions = {}
    for name in (
        "numpy",
        "pandas",
        "scipy",
        "cvxpy",
        "osqp",
        "clarabel",
        "scs",
        "scikit-learn",
        "lightgbm",
        "torch",
        "pytest",
        "ruff",
        "PyYAML",
        "requests",
    ):
        try:
            versions[name] = {"status": "installed", "version": importlib.metadata.version(name)}
        except importlib.metadata.PackageNotFoundError:
            versions[name] = {"status": "missing", "version": None}
    try:
        import numpy as np

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            np.show_config()
        blas = {"status": "available", "numpy_show_config": buffer.getvalue()}
    except ImportError as exc:
        blas = {"status": "missing_dependency", "error_type": type(exc).__name__}
    solvers = {"status": "missing_optional_dependency", "installed": []}
    if versions["cvxpy"]["status"] == "installed":
        try:
            import cvxpy

            solvers = {"status": "available", "installed": cvxpy.installed_solvers()}
        except ImportError as exc:
            solvers = {
                "status": "missing_optional_dependency",
                "installed": [],
                "error_type": type(exc).__name__,
            }
    workflow = root / ".github/workflows/tests.yml"
    ci_versions = []
    if workflow.is_file():
        ci_versions = sorted(
            set(
                re.findall(
                    r"python-version:\s*['\"]([^'\"\s]+)", workflow.read_text(encoding="utf-8")
                )
            )
        )
    checks = {}
    if run_checks:
        for name in ("check_environment.py", "verify_lock.py"):
            checks[name] = _command(root, [sys.executable, str(root / "scripts" / name)])
    return {
        "schema": "paper-research-environment/v1",
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": platform.platform(),
        "machine": platform.machine(),
        "ci_python_versions": ci_versions,
        "reference_ci_python": CI_PYTHON,
        "python_matches_ci_minor": sys.version_info[:2] == (3, 11),
        "difference": None
        if sys.version_info[:2] == (3, 11)
        else "Local Python differs from CI 3.11; this receipt certifies the local runtime only",
        "dependencies": versions,
        "blas": blas,
        "solvers": solvers,
        "checks": checks,
        "checks_executed": run_checks,
        "status": "pass"
        if run_checks and all(row.get("exit_code") == 0 for row in checks.values())
        else "not_run"
        if not run_checks
        else "fail",
        "optional_dependency_policy": "Missing ML/solver extras are recorded, not silently installed",
    }


def replay_fixed_account() -> dict:
    """Exercise production matching/fees/FIFO and independently check every bar."""
    import pandas as pd
    from core.analytics.accounting_check import AccountingReconciler, unrealized_pnl_for_lots
    from core.broker import Broker
    from core.trading.portfolio import Portfolio

    portfolio = Portfolio(initial_capital=FIXTURE_CONFIG["initial_capital"])
    broker = Broker(
        portfolio,
        commission_rate=FIXTURE_CONFIG["commission_rate"],
        slippage=0.0,
        random_slip=False,
        max_participation_rate=1.0,
        exchange_id="offline_fixture",
        account_id="pr25_fixture",
        timeframe="1d",
    )
    reconciler = AccountingReconciler(initial_capital=10000.0)
    symbol = FIXTURE_CONFIG["symbol"]
    actual, differences = [], []
    for index, raw_bar in enumerate(FIXTURE_DATA["bars"]):
        for order in FIXTURE_DATA["orders"]:
            if order["fill_bar"] == index:
                broker.submit_order(
                    symbol,
                    order["side"],
                    order["qty"],
                    price=order["reference_price"],
                    order_type="market",
                    timestamp=pd.Timestamp(order["timestamp"]),
                    strategy_id="PR25_fixed",
                )
        bar = pd.Series(
            {key: value for key, value in raw_bar.items() if key != "timestamp"},
            name=pd.Timestamp(raw_bar["timestamp"]),
        )
        broker.process_orders({symbol: bar})
        mark = float(bar["close"])
        row = {
            "cash": portfolio.cash,
            "qty": portfolio.get_position(symbol)["qty"],
            "fees": sum(trade["commission"] for trade in broker.trades),
            "equity": portfolio.get_equity({symbol: mark}),
            "realized_net_pnl": sum(event.realized_pnl for event in broker.close_events),
            "unrealized_net_pnl": unrealized_pnl_for_lots(portfolio.open_lots(symbol), mark),
        }
        reconciler.check_bar(
            index, bar.name, row["equity"], portfolio, {symbol: mark}, broker.close_events
        )
        actual.append({"timestamp": raw_bar["timestamp"], **row})
        for metric, expected in EXPECTED_ROWS[index].items():
            difference = float(row[metric]) - expected
            if (
                not math.isfinite(difference)
                or abs(difference) > FIXTURE_CONFIG["absolute_tolerance"]
            ):
                differences.append(
                    {
                        "bar": index,
                        "metric": metric,
                        "actual": row[metric],
                        "expected": expected,
                        "difference": difference,
                    }
                )
    fills = [
        {
            key: (str(trade[key]) if key == "fill_time" else trade[key])
            for key in ("order_id", "side", "qty", "fill_price", "commission", "fill_time")
        }
        for trade in broker.trades
    ]
    check = reconciler.result().to_dict()
    passed = (
        not differences
        and check["ok"]
        and check["checks_performed"] == 3
        and len(fills) == 2
        and len(broker.close_events) == 1
    )
    return {
        "schema": "paper-research-baseline-diff/v1",
        "engineering": "pass" if passed else "fail",
        "research": "not_evaluated",
        "admission": "unchanged",
        "external_evidence": "pending",
        "interpretation": "Synthetic engineering only; no market alpha or execution calibration claim",
        "fixture_id": FIXTURE_CONFIG["schema"],
        "input": deepcopy(FIXTURE_DATA),
        "config": deepcopy(FIXTURE_CONFIG),
        "expected": deepcopy(EXPECTED_ROWS),
        "actual": actual,
        "differences": differences,
        "fills": fills,
        "accounting_check": check,
        "facts_sha256": _digest({"actual": actual, "fills": fills}),
        "expected_basis": "buy 2*100 + .20 fee; sell 1*110 - .11 fee; remaining unit marked 105",
        "unmodeled": ["slippage", "market impact", "real venue execution", "funding", "borrow"],
    }


def snapshot_sources(root: Path, destination: Path, files: dict) -> dict:
    """Persist each bound file once and verify it still matches the live manifest."""
    destination.mkdir(parents=True, exist_ok=False)
    for name, expected in files.items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise ValueError("Source manifest path escapes repository")
        source = (root / name).resolve()
        if not source.is_relative_to(root.resolve()):
            raise ValueError("Source manifest path escapes repository")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise ValueError("Source changed while snapshotting: " + name)
    return {
        "directory": destination.name,
        "files_verified": len(files),
        "source_manifest_sha256": _digest(files),
    }


def capture(output: Path, *, root: Path = ROOT) -> dict:
    """Create a new bundle; distinguish missing dependencies and execution failures."""
    root = root.resolve()
    if root != ROOT:
        raise ValueError(
            "Capture root must be this script's actual repository; cannot mix code and another root"
        )
    output.mkdir(parents=True, exist_ok=False)
    _write(output / "fixture.json", FIXTURE_DATA)
    _write(output / "config.json", FIXTURE_CONFIG)
    environment = capture_environment(root)
    _write(output / "environment.json", environment)
    try:
        state = capture_current_state(root)
        _write(output / "current_state.json", state)
        snapshot = snapshot_sources(root, output / "source", state["source_hashes"])
        baseline = replay_fixed_account()
        after = capture_current_state(root)
        baseline["source_unchanged_during_capture"] = (
            state["source_hashes"] == after["source_hashes"]
        )
        if not baseline["source_unchanged_during_capture"]:
            baseline["engineering"] = "fail"
            baseline["source_drift"] = sorted(
                name
                for name in set(state["source_hashes"]) | set(after["source_hashes"])
                if state["source_hashes"].get(name) != after["source_hashes"].get(name)
            )
        baseline["source_manifest_sha256"] = state["source_manifest_sha256"]
        baseline["config_sha256"] = state["config_sha256"]
        baseline["data_manifest_sha256"] = state["data_manifest_sha256"]
        baseline["source_snapshot"] = snapshot
    except ImportError as exc:
        if not (output / "current_state.json").exists():
            _write(
                output / "current_state.json",
                {
                    "status": "missing_dependency",
                    "error_type": type(exc).__name__,
                    "missing_module": exc.name,
                },
            )
        baseline = {
            "engineering": "pending",
            "failure_kind": "missing_dependency",
            "error_type": type(exc).__name__,
            "missing_module": exc.name,
        }
    except Exception as exc:
        if not (output / "current_state.json").exists():
            _write(
                output / "current_state.json",
                {"status": "current_execution_failure", "error_type": type(exc).__name__},
            )
        baseline = {
            "engineering": "fail",
            "failure_kind": "current_execution_failure",
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
    baseline["environment_status"] = environment["status"]
    _write(output / "baseline_diff.json", baseline)
    return baseline


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = capture(args.output)
    print(
        _json(
            {
                "output": str(args.output),
                "engineering": result["engineering"],
                "environment_status": result["environment_status"],
            }
        )
    )
    return 0 if result["engineering"] == "pass" and result["environment_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
