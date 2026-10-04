"""Append public, immutable, closed daily spot inputs for ML shadow work."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research.ml_selection.forward_evidence import collect_public_forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/ml_selection_public_forward")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--history-days", type=int, default=90)
    args = parser.parse_args()
    result = collect_public_forward(args.output, args.symbols, history_days=args.history_days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
