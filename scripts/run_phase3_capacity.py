"""Generate the checked-in Phase 3 capacity verification report."""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.capacity import run_capacity_curve
from config.config import config
from tests.engine_baseline_harness import build_synthetic_data_map


def main() -> None:
    logging.disable(logging.CRITICAL)
    report = run_capacity_curve(
        build_synthetic_data_map(bars=180),
        capital_levels=config.require("capacity", "capital_levels"),
        engine_kwargs={"warmup_period": 30, "slippage": None},
    )
    payload = {
        "scope": "deterministic synthetic capacity verification; not a production AUM claim",
        "account_mode": config.require("account", "mode"),
        **report,
    }
    output = PROJECT_ROOT / "docs" / "phase3_capacity_report.json"
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    try:
        output.write_text(rendered, encoding="utf-8")
        print(output)
    except PermissionError:
        # Restricted verification sandboxes may be read-only to child
        # processes even though the source patching channel can still commit
        # the generated result.  Preserve the exact report on stdout.
        print("REPORT_JSON_BEGIN")
        print(rendered, end="")
        print("REPORT_JSON_END")


if __name__ == "__main__":
    main()
