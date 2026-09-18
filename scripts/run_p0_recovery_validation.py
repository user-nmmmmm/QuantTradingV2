"""Fixed recovery-policy validation on the prior 60-coin frozen data."""
from copy import deepcopy
import argparse
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.config import config
from core.reproducibility import canonical_json
from scripts.run_revalidation60 import load_inputs, protocol, run_one, save, source_hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "reports/revalidation_60_20260908")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    logging.disable(logging.CRITICAL)
    frames, inventory, lifecycle = load_inputs(args.source)
    frozen = protocol(frames, lifecycle)
    old = json.loads((args.source / "frozen_protocol.json").read_text(encoding="utf-8"))["parameters"]
    for section in old:
        if section != "strategy_health":
            assert old[section] == json.loads(canonical_json(config._config[section])), section
    save(args.output / "protocol.json", {**frozen, "prior_health_policy": old["strategy_health"],
         "purpose": "P0 automatic recovery acceptance; retrospective, no new admission claim",
         "data_source": str(args.source), "data_manifest": inventory,
         "criteria": ["no new automatic manual locks", "extended cooldown followed by reduced-risk probation",
                      "later actual trades", "account liquidation remains binding", "deterministic replay", "accounting identity"]})
    summaries = []
    baseline = deepcopy(frozen)
    baseline["parameters"]["strategy_health"] = old["strategy_health"]
    _, row = run_one("legacy_policy", args.output, frames, baseline)
    summaries.append(row)
    results = []
    for name in ("recovery", "recovery_replay"):
        result, row = run_one(name, args.output, frames, frozen)
        summaries.append(row)
        results.append(result)
    health = results[0]["strategy_health"]["TrendBreakout"]
    transitions = results[0]["lifecycle"]["health_transition_log"]
    recovery_entries = [t for t in transitions if t["to"] == "probation" and t["risk_multiplier"] == .1]
    checks = {"no_automatic_manual_lock": health["status"] != "manual_lock" and not any(t["to"] == "manual_lock" for t in transitions),
              "reduced_risk_recovery_observed": bool(recovery_entries),
              "deterministic": results[0]["_digest"] == results[1]["_digest"],
              "accounting": all(r["accounting_check"]["ok"] for r in results),
              "health_inactivity_reported": results[0]["lifecycle"]["strategy_inactive_days"] > 0,
              "later_actual_trading": str(results[0]["lifecycle"]["last_fill_at"])[:10] > "2022-09-19",
              "source_unchanged": source_hashes() == frozen["source_hashes"]}
    save(args.output / "summary.json", summaries)
    save(args.output / "acceptance.json", {"checks": checks, "passed": all(checks.values()),
         "admission": "paused_revalidation", "research_admitted": False})
    if not all(checks.values()):
        raise RuntimeError(f"P0 acceptance failed: {checks}")


if __name__ == "__main__":
    main()
