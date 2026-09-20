from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from analysis.strategy_review import cohort_evidence, evaluate_gates, freeze_prospective, open_prospective
from scripts.run_strategy_review import matrix, validation_specs, windows


def test_registered_matrix_has_52_unique_frozen_configs_without_mutation():
    config = yaml.safe_load((Path(__file__).parents[1] / "config/params.yaml").read_text(encoding="utf-8"))
    before = deepcopy(config)
    arms = matrix(config)
    assert len(arms) == len({a["parameters_sha256"] for a in arms}) == 52
    assert config == before
    assert arms[0]["arm"] == "baseline"
    assert arms[0]["parameters"] == config
    assert len([a for a in arms if "stops" in a["families"]]) == 36
    assert len([a for a in arms if "timing" in a["families"]]) == 8
    assert all(a["parameters"]["strategy_governance"]["TrendBreakout"] == "paused_revalidation" for a in arms)


def test_windows_use_saved_indices_and_missing_seeds_are_fixed():
    reference = dict(timeline=[f"2020-01-{i:02d}" for i in range(1, 20)], rolling_windows=[dict(test_start=4, test_end=9)],
                     requested_start="2016-01-01", requested_end_inclusive_utc="2026-06-30", segments={})
    assert windows(reference)[0]["start"] == "2020-01-05"
    assert windows(reference)[0]["end"] == "2020-01-09"
    reference["timeline"] += ["2030-01-01"]
    assert windows(reference)[0]["end"] == "2020-01-09"
    assert [r["seed"] for r in validation_specs(reference) if "seed" in r] == list(range(42, 62))


def _close(i, day, pnl, reason="protective_stop", action=None):
    return dict(close_event_id=str(i), timestamp=day, opening_strategy_id="TrendBreakout",
                realized_pnl=pnl, exit_reason=reason, risk_action_id=action)


def test_cohort_evaluator_matches_health_grouping_and_excludes_valuation():
    events = [_close(i, "2020-01-01", -10, action=str(i)) for i in range(3)]
    events.append(_close(4, "2020-01-02", 500, "EndOfBacktest"))
    evidence = cohort_evidence(events)
    assert evidence["cohort_count"] == 1
    assert evidence["scenarios"]["0"]["remaining_net_closed_pnl"] == -30
    assert evaluate_gates(dict(return_pct=-1, max_drawdown_pct=1), evidence)["profit_factor"] == "insufficient"
    with pytest.raises(ValueError, match="Duplicate"):
        cohort_evidence(events + [events[0]])


def test_zero_loss_bootstrap_is_explicit_and_json_finite():
    events = [_close(i, f"2020-01-{i+1:02d}", 1) for i in range(30)]
    evidence = cohort_evidence(events)
    assert evidence["scenarios"]["0"]["profit_factor_unbounded"]
    assert evidence["scenarios"]["0"]["lower_unbounded"]
    json.dumps(evidence, allow_nan=False)
    assert evaluate_gates(dict(return_pct=1, max_drawdown_pct=1), evidence)["profit_factor"] == "pass"


def test_forward_protocol_dates_identity_maturity_and_single_open(tmp_path):
    path = tmp_path / "forward.json"
    record = freeze_prospective(path, code_hash="code", config_hash="config", registry_hash="registry", frozen_at="2026-09-19T04:00:00Z")
    assert record["test_start"] == "2026-10-20T00:00:00+00:00"
    kwargs = dict(code_hash="code", config_hash="config", registry_hash="registry", data_hash="data", labels_complete=True)
    with pytest.raises(PermissionError, match="mature"):
        open_prospective(path, now="2026-09-20T00:00:00Z", **kwargs)
    with pytest.raises(PermissionError, match="identity"):
        open_prospective(path, now="2028-01-01T00:00:00Z", **{**kwargs, "code_hash": "wrong"})
    result = open_prospective(path, now="2028-01-01T00:00:00Z", **kwargs)
    assert result["status"] == "opened_for_final_evaluation"
    with pytest.raises(PermissionError, match="already"):
        open_prospective(path, now="2028-01-01T00:00:00Z", **kwargs)
