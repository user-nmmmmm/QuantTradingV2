"""Capture current_state.json, environment.json and baseline_diff.json into a NEW directory (PR25-01).

  python scripts/capture_research_baseline.py --output <new dir> [--previous <earlier current_state.json>]
                                              [--known-failure TEXT ...]

Offline and read-only with respect to the repository: nothing is traded, trained or downloaded. The output
directory must not exist, so an earlier baseline is never overwritten.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from analysis.research_baseline import current_state, diff_states, environment_state
from core.foundation.reproducibility import canonical_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--known-failure", action="append", default=[])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)

    previous = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous else None
    state = current_state(args.root, known_failures=args.known_failure)
    args.output.mkdir(parents=True, exist_ok=False)
    for name, payload in (("current_state.json", state), ("environment.json", environment_state(args.root)),
                          ("baseline_diff.json", diff_states(previous, state))):
        (args.output / name).write_text(canonical_json(payload) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote baseline to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
