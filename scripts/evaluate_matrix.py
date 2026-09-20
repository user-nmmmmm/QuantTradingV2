"""Read-only engineering verdict for pinned matrix artifacts; never strategy admission."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.verify_roadmap_completion import load_json, object_value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return value


def contained(base: Path, name: str) -> Path:
    if not isinstance(name, str) or not name:
        raise ValueError("artifact path required")
    path = (base / name).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ValueError("artifact missing or outside report: " + name)
    return path


def evaluate_report(report: Path, *, maximum_drawdown=0.25) -> dict:
    """Accept valid null metrics, reject invalid facts and mismatched sealed outputs."""
    maximum_drawdown = finite(maximum_drawdown, "maximum_drawdown")
    if not 0 < maximum_drawdown <= 1:
        raise ValueError("maximum_drawdown must be in (0, 1]")
    report = Path(report)
    manifest = object_value(load_json(report / "run_manifest.json"), "manifest")
    if manifest.get("schema_version") != "2.0":
        raise ValueError("full run_manifest schema 2.0 required")
    required = {"metrics.json", "reconciliation.json", "closed_trades.csv", "equity.csv"}
    artifacts = object_value(manifest.get("artifacts"), "artifacts")
    if not required.issubset(artifacts):
        raise ValueError("full audit artifacts are missing from manifest")
    for name, record in artifacts.items():
        object_value(record, "artifact")
        if digest(contained(report, name)) != record.get("sha256"):
            raise ValueError("artifact digest mismatch: " + name)
    snapshots = manifest.get("data_snapshots")
    if not isinstance(snapshots, dict) or not snapshots:
        raise ValueError("fixed data snapshots required")
    for record in snapshots.values():
        object_value(record, "snapshot")
        if digest(contained(report / "data_inputs", record["path"])) != record.get("sha256"):
            raise ValueError("data snapshot digest mismatch")
    if manifest.get("audit", {}).get("coverage", {}).get("status") != "ok":
        raise ValueError("event audit coverage failed")
    reconciliation_document = object_value(load_json(report / "reconciliation.json"), "reconciliation")
    if reconciliation_document.get("schema_version") != "quanttrading.metrics/v1":
        raise ValueError("standard reconciliation envelope required")
    reconciliation = object_value(reconciliation_document.get("metrics"), "reconciliation facts")
    if (reconciliation.get("schema_version") != "closed-trade-reconciliation/v1"
            or reconciliation.get("status") != "pass"):
        raise ValueError("trade reconciliation is not pass")
    doc = object_value(load_json(report / "metrics.json"), "metrics")
    if doc.get("schema_version") != "quanttrading.metrics/v1":
        raise ValueError("standard metrics schema required")
    values = object_value(doc.get("metrics"), "metric values")
    if object_value(values.get("TradeInputIntegrity"), "trade integrity").get("status") != "ok":
        raise ValueError("invalid authoritative trade inputs")
    metrics = doc.get("metric_results")
    if not isinstance(metrics, list) or not metrics:
        raise ValueError("standard metric results required")
    names = set()
    for row in metrics:
        object_value(row, "metric result")
        if not isinstance(row.get("name"), str) or not row["name"] or row["name"] in names:
            raise ValueError("metric result names must be present and unique")
        names.add(row["name"])
        if row.get("status") not in {"ok", "undefined", "insufficient", "insufficient_data", "not_modeled", "invalid_input"}:
            raise ValueError("unknown metric result status")
        if row.get("status") == "invalid_input":
            raise ValueError("invalid metric facts: " + str(row.get("name")))
        if row.get("status") == "ok":
            finite(row.get("value"), row["name"])
        if row.get("status") != "ok" and row.get("value") is not None:
            raise ValueError("unavailable metric must use null")
    total_return = finite(values.get("TotalReturn"), "TotalReturn")
    dd = finite(values.get("MaxDrawdownPct"), "MaxDrawdownPct")
    trades = finite(values.get("TotalTrades"), "TotalTrades")
    # The standard metric is a signed return from peak: -0.20 means a 20% loss.
    # Compare its magnitude with the positive policy budget; keep the raw sign.
    if trades < 0 or int(trades) != trades or not -1 <= dd <= 0:
        raise ValueError("invalid trade count or drawdown unit")
    drawdown_magnitude = -dd
    warnings = []
    if trades < 30:
        warnings.append("insufficient_trade_support_no_strategy_validity_claim")
    if total_return < 0:
        warnings.append("negative_research_return")
    return {"status": "pass" if drawdown_magnitude <= maximum_drawdown else "fail",
            "reason": None if drawdown_magnitude <= maximum_drawdown else "drawdown_threshold_exceeded",
            "report_dir": str(report.resolve()), "manifest_sha256": digest(report / "run_manifest.json"),
            "result_digest": manifest.get("execution", {}).get("result_digest"),
            "total_return": total_return, "max_drawdown": dd,
            "max_drawdown_magnitude": drawdown_magnitude, "trades": int(trades),
            "warnings": warnings, "unavailable_metrics": [row.get("name") for row in doc.get("metric_results", [])
                                                          if row.get("status") != "ok"],
            "live_admission": False}


def evaluate_matrix(directory: Path, *, maximum_drawdown=0.25) -> dict:
    directory = Path(directory)
    config = object_value(load_json(directory / "config.json"), "matrix config")
    rows = load_json(directory / "results.json")
    subjects = config.get("symbols", []) if config.get("per_symbol") else ["ALL"]
    expected = {(tf, window, subject) for tf in config.get("timeframes", [])
                for window in config.get("windows", []) for subject in subjects}
    if not expected or not isinstance(rows, list) or not rows:
        raise ValueError("matrix dimensions and results must be nonempty")
    for row in rows:
        object_value(row, "matrix cell")
    keys = [(r.get("timeframe"), r.get("window"), r.get("subject")) for r in rows]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("missing, duplicate or unexpected matrix cells")
    evaluated = []
    for row in rows:
        try:
            if type(row.get("exit_code")) is not int or row["exit_code"] != 0 or row.get("error"):
                raise ValueError("matrix child process failed")
            report = Path(row["report_dir"]).resolve()
            if not report.is_relative_to(directory.resolve()):
                raise ValueError("report must belong to this exclusive matrix directory")
            result = evaluate_report(report, maximum_drawdown=maximum_drawdown)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            result = {"status": "fail", "reason": str(exc)}
        evaluated.append({"timeframe": row["timeframe"], "window": row["window"],
                          "subject": row["subject"], **result})
    return {"schema_version": "automation-matrix-verdict/v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "pass" if all(r["status"] == "pass" for r in evaluated) else "fail",
            "scope": "artifact_integrity_and_fixed_engineering_thresholds",
            "strategy_validity": "not_evaluated", "live_admission": False,
            "input_sha256": {p.name: digest(p) for p in (directory / "config.json", directory / "results.json")},
            "cells": evaluated}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--maximum-drawdown", type=float, default=0.25)
    args = parser.parse_args(argv)
    try:
        report = evaluate_matrix(args.directory, maximum_drawdown=args.maximum_drawdown)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        report = {"status": "fail", "reason": str(exc), "live_admission": False}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
