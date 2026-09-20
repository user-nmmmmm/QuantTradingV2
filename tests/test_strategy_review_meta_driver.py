"""Required research controls cannot be silently omitted from acceptance."""
from copy import deepcopy

import pytest

from scripts.run_strategy_review_meta import required_replay_checks


def _complete():
    report = {"status": "complete", "errors": [], "accounts": [
        {"arm": arm, "metrics_valid": True, "activity": "inactive" if arm == "gate" else "active"}
        for arm in ("baseline", "gate", "sizing")
    ]}
    return {name: deepcopy(report) for name in ("p3", "p3_primary", "p3_fixed_quarter")}


def test_inactive_gate_is_valid_engineering_result():
    checks = required_replay_checks(_complete())
    assert all(all(group.values()) for group in checks.values())


@pytest.mark.parametrize("name", ["p3_primary", "p3_fixed_quarter"])
@pytest.mark.parametrize("failure", ["missing", "incomplete", "no_accounts", "invalid_metrics", "duplicate_arm"])
def test_primary_and_quarter_failures_prevent_complete_acceptance(name, failure):
    components = _complete()
    if failure == "missing":
        del components[name]
    elif failure == "incomplete":
        components[name].update(status="incomplete", errors=[{"reason": "invalid_replay_input"}])
    elif failure == "no_accounts":
        components[name]["accounts"] = []
    elif failure == "invalid_metrics":
        components[name]["accounts"][0]["metrics_valid"] = False
    else:
        components[name]["accounts"][1]["arm"] = "baseline"
    checks = required_replay_checks(components)
    assert not all(checks[name].values())
    assert all(checks["p3"].values())
