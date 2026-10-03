"""Compare two frozen main/paper report sets without executing a backtest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analysis.backtest_comparison import generate_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-main", "new-main", "old-paper", "new-paper", "output-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--merge-sha", required=True)
    args = parser.parse_args()
    result = generate_comparison(args.old_main, args.new_main, args.old_paper, args.new_paper,
                                 args.output_dir, args.merge_sha)
    print(json.dumps({"status": result["comparison"]["status"], "output_dir": result["output_dir"],
        "main_changed": result["comparison"]["main_changed"], "paper_changed": result["comparison"]["paper_changed"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
