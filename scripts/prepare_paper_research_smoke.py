"""Prepare exact local inputs for the bounded two-account engineering smoke.

This prepares a contract only. Run the resulting files with the study CLI after
source edits have stopped. It evaluates no market hypothesis or final sample.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path


from analysis.research_evidence import source_identity
from analysis.search_scope import data_manifest, validate_protocol
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file
from scripts.capture_paper_research_baseline import FIXTURE_CONFIG, FIXTURE_DATA, EXPECTED_ROWS


ROOT = Path(__file__).resolve().parents[1]


def prepare(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    config_path = ROOT / "config/paper_research_smoke_20261027.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    jobs = [
        {"id": name, "kind": "account_fixture"}
        for name in config["candidate_ids"] + config["benchmark_ids"]
    ]
    inputs = {
        "account_fixture": FIXTURE_DATA,
        "account_config": FIXTURE_CONFIG,
        "expected_rows": EXPECTED_ROWS,
    }

    def digest(value):
        return sha256_bytes(canonical_json(value).encode("utf-8"))

    protocol = validate_protocol(
        {
            **config,
            "schema": "paper-research-protocol/v1",
            "study_id": "pr25-bounded-smoke-" + output.parent.name + "-" + output.name,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest_sha256": digest(source_identity(ROOT)),
            "config_sha256": sha256_file(config_path),
            "data_manifest_sha256": digest(data_manifest(inputs)),
            "job_table_sha256": digest(jobs),
        }
    )
    for name, value in [("protocol", protocol), ("jobs", jobs), ("data", inputs)]:
        (output / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "protocol": str(output / "protocol.json"),
        "jobs": str(output / "jobs.json"),
        "data": str(output / "data.json"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.output), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
