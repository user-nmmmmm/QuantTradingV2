"""Verify the PR25 phase A engineering contract without market data or orders.

The source snapshot and full local attempt journal are retained in a new run.
This is a deterministic account check, not a strategy or admission evaluation.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import time


from analysis.research_evidence import source_identity
from analysis.search_scope import SearchScope, data_manifest
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file
from scripts.capture_paper_research_baseline import (
    FIXTURE_CONFIG,
    FIXTURE_DATA,
    EXPECTED_ROWS,
    capture,
)


ROOT = Path(__file__).resolve().parents[1]


def _digest(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )


def _record_failure(scope, output, candidate, error):
    """Preserve the original failure even when drift prevents journal writes."""
    journal_error = None
    try:
        scope.record(
            candidate=candidate,
            status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
            reason=f"{type(error).__name__}: {error}",
        )
    except Exception as secondary:
        journal_error = f"{type(secondary).__name__}: {secondary}"
    _write(output / "search_scope_audit.json", scope.audit())
    _write(
        output / "failure.json",
        {
            "engineering": "fail",
            "candidate": candidate,
            "error_type": type(error).__name__,
            "reason": str(error),
            "journal_error": journal_error,
            "research": "not_evaluated",
            "live_authorization": False,
        },
    )


def decimal_account_bridge():
    """Independently derive cash/fees/quantity/equity from literal economic facts.

    No Broker or Portfolio calculation is used by this reference. Fees on the
    remaining lot stay in unrealized PnL until that unit is sold.
    """
    amount = Decimal
    cash = amount("10000") - amount("2") * amount("100") - amount("0.20")
    after_sale = cash + amount("110") - amount("0.11")
    realized = amount("110") - amount("100") - amount("0.10") - amount("0.11")
    return [
        {
            "cash": float(cash),
            "qty": 2.0,
            "fees": 0.2,
            "equity": float(cash + amount("200")),
            "realized_net_pnl": 0.0,
            "unrealized_net_pnl": -0.2,
        },
        {
            "cash": float(after_sale),
            "qty": 1.0,
            "fees": 0.31,
            "equity": float(after_sale + amount("110")),
            "realized_net_pnl": float(realized),
            "unrealized_net_pnl": 9.9,
        },
        {
            "cash": float(after_sale),
            "qty": 1.0,
            "fees": 0.31,
            "equity": float(after_sale + amount("105")),
            "realized_net_pnl": float(realized),
            "unrealized_net_pnl": 4.9,
        },
    ]


def verify(output: Path, *, clock=time.monotonic) -> dict:
    """Register before replay, preserve all attempts, and snapshot actual bytes."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    config_path = ROOT / "config/paper_research_phase_a.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    inputs = {
        "account_fixture": deepcopy(FIXTURE_DATA),
        "account_config": deepcopy(FIXTURE_CONFIG),
        "expected_rows": deepcopy(EXPECTED_ROWS),
    }
    sources = source_identity(ROOT)
    protocol = {
        **config,
        "schema": "paper-research-protocol/v1",
        "study_id": "phase-a-" + output.name,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest_sha256": _digest(sources),
        "config_sha256": sha256_file(config_path),
        "data_manifest_sha256": _digest(data_manifest(inputs)),
    }
    scope = SearchScope.register(
        protocol=protocol, data_map=inputs, output=output / "evidence", source_root=ROOT
    )
    _write(output / "protocol.json", protocol)
    _write(output / "inputs.json", inputs)
    started = clock()
    benchmark, candidate = config["benchmark_ids"][0], config["candidate_ids"][0]
    scope.record(candidate=benchmark, status="started")
    try:
        reference = decimal_account_bridge()
        if reference != EXPECTED_ROWS:
            raise ValueError("Decimal reference differs from the frozen fixture expectation")
        scope.record(candidate=benchmark, status="completed", reference_sha256=_digest(reference))
    except BaseException as exc:
        _record_failure(scope, output, benchmark, exc)
        raise
    _write(output / "decimal_reference.json", reference)
    scope.record(candidate=candidate, status="started")
    try:
        baseline = capture(output / "baseline")
        if baseline.get("engineering") != "pass":
            raise ValueError("Fixed account verification failed; inspect baseline bundle")
        if baseline["source_manifest_sha256"] != protocol["source_manifest_sha256"]:
            raise ValueError("Source identity changed between registration and account replay")
        if len(baseline["actual"]) != len(reference):
            raise ValueError("Account replay omitted a declared bar")
        differences = [
            abs(actual[key] - expected[key])
            for actual, expected in zip(baseline["actual"], reference)
            for key in expected
        ]
        maximum = max(differences)
        if maximum > config["economic_or_precision_target"]["threshold"]:
            raise ValueError("Actual account bridge exceeds declared precision target")
        elapsed = clock() - started
        if elapsed > config["resource_budget"]["wall_time_seconds"]:
            raise ValueError("Fixed verification exceeded declared wall time budget")
        scope.record(
            candidate=candidate,
            status="completed",
            max_absolute_account_bridge_error=maximum,
            baseline_diff_sha256=sha256_file(output / "baseline/baseline_diff.json"),
        )
    except BaseException as exc:
        _record_failure(scope, output, candidate, exc)
        raise
    audit = scope.audit()
    _write(output / "search_scope_audit.json", audit)
    receipt = {
        "schema": "paper-research-phase-a-engineering/v1",
        "engineering": "pass"
        if audit["status"] == "ok"
        and audit["results_complete"]
        and baseline["environment_status"] == "pass"
        else "fail",
        "account_engineering": baseline["engineering"],
        "environment_status": baseline["environment_status"],
        "research": "not_evaluated",
        "external_evidence": "pending",
        "admission": "unchanged",
        "live_authorization": False,
        "max_absolute_account_bridge_error": maximum,
        "wall_time_seconds": clock() - started,
        "resource_limits": "Sequential fixed fixture; elapsed time checked. No general memory or process enforcement; study runner belongs to PR25-18.",
        "local_family_complete": audit["local_family_complete"],
        "historical_global_search_complete": False,
        "identity": scope.run.identity,
        "source_manifest_sha256": protocol["source_manifest_sha256"],
        "source_snapshot": "baseline/source",
        "checks": {
            "independent_arithmetic": "pass",
            "account_reconciliation": "pass",
            "local_environment": baseline["environment_status"],
            "journal": audit["status"],
        },
        "limitations": [
            "No alpha inference, historical search reconstruction, final-sample access or admission decision.",
            "Local runtime differs from CI; CI execution is not claimed.",
            "Literature and Excel evidence are reported separately.",
        ],
    }
    receipt["artifact_hashes"] = {
        path.relative_to(output).as_posix(): sha256_file(path)
        for path in sorted(output.rglob("*"))
        if path.is_file() and not path.relative_to(output).as_posix().startswith("baseline/source/")
    }
    _write(output / "acceptance.json", receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    receipt = verify(args.output)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "engineering": receipt["engineering"],
                "max_absolute_account_bridge_error": receipt["max_absolute_account_bridge_error"],
            }
        )
    )
    return 0 if receipt["engineering"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
