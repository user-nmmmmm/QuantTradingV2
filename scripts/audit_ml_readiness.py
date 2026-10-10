"""Audit existing selector evidence without training, downloads, or backtests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import pandas as pd

from core.reproducibility import canonical_json, sha256_file
from research.ml_selection.readiness import audit_readiness, describe_serving_drift


def _records(path):
    source = Path(path)
    if source.suffix.lower() == ".csv":
        return pd.read_csv(source).to_dict("records")
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ValueError("drift input JSON must be an array of record mappings")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=ROOT / "config/ml_selection_next.yaml")
    parser.add_argument("--bundle", type=Path, default=ROOT / "config/ml_selector_current.json")
    parser.add_argument("--root", type=Path, default=ROOT, help="root for registration paths")
    parser.add_argument("--protocol", type=Path, help="original source protocol JSON hash anchor")
    parser.add_argument("--account-mode", choices=("spot", "spot_margin"))
    parser.add_argument("--as-of", help="UTC watermark reporting clock; does not update evidence")
    parser.add_argument("--reference", type=Path, help="existing training feature records (CSV or JSON array)")
    parser.add_argument("--observations", type=Path, help="existing serving rows (CSV or JSON array)")
    parser.add_argument("--activity", type=Path, help="separate actual account date/fills/status records")
    parser.add_argument("--features", nargs="+", help="explicit features for drift; defaults to frozen model order")
    parser.add_argument("--stale-after-days", type=float, default=1.)
    parser.add_argument("--forward-store", type=Path, help="verify existing preregistered forward store with existing evaluator")
    parser.add_argument("--output", type=Path, help="new report path; existing paths are never overwritten")
    args = parser.parse_args(argv)
    if bool(args.reference) != bool(args.observations):
        parser.error("--reference and --observations must be supplied together")
    if args.activity and not args.observations:
        parser.error("--activity requires --reference and --observations")
    if args.forward_store and not args.protocol:
        parser.error("--forward-store requires --protocol")
    if args.output and args.output.exists():
        parser.error("--output already exists; choose a new immutable report path")
    report = audit_readiness(args.settings, args.bundle, root=args.root, protocol_path=args.protocol,
                            serving_account_mode=args.account_mode, as_of=args.as_of)
    if args.observations:
        features = args.features or report.get("models", {}).get("model", {}).get("features")
        if not features:
            parser.error("--features required when frozen model feature order is unavailable")
        report["serving_drift"] = describe_serving_drift(
            _records(args.reference), _records(args.observations), features=features,
            activity=_records(args.activity) if args.activity else None,
            stale_after_days=args.stale_after_days,
            training_domain={"account_mode": next((item.get("verified_training_mode")
                for item in report["checks"] if item["name"] == "account_mode"), None)},
            serving_domain={"account_mode": args.account_mode} if args.account_mode else None)
        report["drift_input_hashes"] = {str(path): sha256_file(path) for path in
                                      (args.reference, args.observations, args.activity) if path}
    if args.forward_store:
        from research.ml_selection.forward_evidence import evaluate_forward_store
        protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
        report["forward_evidence"] = evaluate_forward_store(args.forward_store, protocol, as_of=args.as_of)
    payload = json.dumps(json.loads(canonical_json(report)), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(payload)
    print(payload, end="")
    return 2 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
