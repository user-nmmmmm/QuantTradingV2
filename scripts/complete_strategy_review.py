"""Finish registered validation, meta and cross-market jobs within two slots."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    batch = args.batch.resolve()
    completion = batch / "matrix_completion.json"
    print("Waiting for the already-running two-slot matrix", flush=True)
    while not completion.exists():
        time.sleep(10)
    record = json.loads(completion.read_text(encoding="utf-8"))
    if record["failed"] or len(record["completed"]) != 572:
        raise ValueError("Matrix must complete before subsequent engine jobs")

    def execute(name, arguments):
        print("START " + name, flush=True)
        with (batch / (name + "_driver.log")).open("w", encoding="utf-8") as log:
            completed = subprocess.run([sys.executable, *arguments, "--batch", str(batch)],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        print(f"DONE {name}, exit={completed.returncode}", flush=True)
        return completed.returncode

    if execute("validation", ["scripts/run_strategy_review.py", "validation", "--workers", "2"]):
        return 1
    # Each runner is internally sequential. Together they occupy exactly two
    # experiment slots, never overlapping with the two-slot parameter matrix.
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(execute, "meta", ["scripts/run_strategy_review_meta.py"]),
                pool.submit(execute, "cross_market", ["scripts/run_strategy_review_cross_market.py"])]
        codes = [future.result() for future in jobs]
    return int(any(codes))


if __name__ == "__main__":
    raise SystemExit(main())
