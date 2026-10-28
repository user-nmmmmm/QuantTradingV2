"""Run a preregistered offline study; never open a final holdout implicitly."""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap():
    sys.path.insert(0, str(ROOT))
    from analysis.paper_research_orchestration import run_study
    from analysis.search_scope import _load_json
    from analysis.paper_research_typed_inputs import load_typed_data_manifest

    return run_study, _load_json, load_typed_data_manifest


run_study, _load_json, load_typed_data_manifest = _bootstrap()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", required=True, type=Path)
    parser.add_argument("--jobs", required=True, type=Path)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument(
        "--data", type=Path, help="finite JSON data_map; data hashes must match protocol"
    )
    inputs.add_argument(
        "--data-manifest", type=Path, help="safe typed JSON/CSV manifest with exact/v2 identities"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    data_map = (
        load_typed_data_manifest(args.data_manifest)
        if args.data_manifest
        else _load_json(args.data.read_text(encoding="utf-8"))
    )
    report = run_study(
        protocol=args.protocol,
        jobs=_load_json(args.jobs.read_text(encoding="utf-8")),
        data_map=data_map,
        output=args.output,
        source_root=ROOT,
        resume=args.resume,
    )
    print(
        json.dumps(
            {key: report[key] for key in ("engineering", "research", "coverage", "fatal_error")}
        )
    )
    return 0 if report["engineering"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
