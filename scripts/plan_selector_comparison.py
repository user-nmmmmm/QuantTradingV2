"""Write a selector ablation plan or compare existing paired account receipts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from core.reproducibility import canonical_json, sha256_file
from research.ml_selection.comparison import compare_account_ledgers, plan_comparison
from research.ml_selection.readiness import audit_readiness


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=ROOT / "config/ml_selection_next.yaml")
    parser.add_argument("--bundle", type=Path, default=ROOT / "config/ml_selector_current.json")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--controls", type=Path, help="complete CONTROL_FIELDS mapping; missing fields stay pending")
    parser.add_argument("--account-mode", choices=("spot", "spot_margin"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--left-ledger", type=Path, help="existing complete account ledger; requires --right-ledger")
    parser.add_argument("--right-ledger", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="new immutable report file")
    args = parser.parse_args(argv)
    if bool(args.left_ledger) != bool(args.right_ledger):
        parser.error("--left-ledger and --right-ledger must be supplied together")
    if args.output.exists():
        parser.error("--output already exists; choose a new immutable report path")
    readiness = audit_readiness(args.settings, args.bundle, root=args.root,
                               protocol_path=args.protocol, serving_account_mode=args.account_mode)
    controls = json.loads(args.controls.read_text(encoding="utf-8")) if args.controls else {}
    candidate = None
    if args.bundle.is_file():
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        candidate = {"bundle_sha256": sha256_file(args.bundle), "candidate": bundle.get("candidate"),
                     "files": bundle.get("files"), "selection": bundle.get("selection"),
                     "policy_threshold": readiness.get("models", {}).get("policy", {}).get("metadata", {}).get("evaluation_threshold")}
    report = plan_comparison(controls, frozen_candidate=candidate, readiness=readiness, seed=args.seed)
    report["readiness"] = readiness
    if args.left_ledger:
        left = json.loads(args.left_ledger.read_text(encoding="utf-8"))
        right = json.loads(args.right_ledger.read_text(encoding="utf-8"))
        report["existing_account_comparison"] = compare_account_ledgers(left, right)
        report["account_receipt_sha256"] = {"left": sha256_file(args.left_ledger),
                                            "right": sha256_file(args.right_ledger)}
    payload = json.dumps(json.loads(canonical_json(report)), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    print(payload, end="")
    return 2 if readiness["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
