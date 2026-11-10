"""config-change-statement/v2: behaviour changes are declared and evidenced, never passed off as disabled additions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from scripts.register_bound_successor import (
    STATEMENT_SCHEMA_V2, behavior_changes, check_config_change, config_hash_at,
)

ROOT = Path(__file__).resolve().parents[1]
DRAFT = ROOT / "docs/research/config_change_c1_c2_20261110.json"

PARENT = b"state:\n  adx_threshold: 25\nscoring:\n  adx_threshold: 25\n  weight: 1\n"
NEW = (b"state:\n  adx_threshold: 25\nscoring:\n  weight: 1\nentry:\n  use_obv: false\n"
       b"extra:\n  enabled: false\n")
REGISTRY = (json.dumps({"experiment_id": "X-1", "status": "completed_stage_C1_candidate"}) + "\n"
            + json.dumps({"experiment_id": "X-2", "status": "registered"}) + "\n").encode()
FILES = {"docs/research/current_strategy_experiment_registry.jsonl": REGISTRY, "docs/r.md": b"# result\n"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tagged(path: str):
    return FILES.get(path)


def statement(changes):
    return {"schema": STATEMENT_SCHEMA_V2, "reason": "fixture", "parent_config_sha256": sha(PARENT),
            "new_config_sha256": sha(NEW), "changes": changes}


GOOD = [
    {"kind": "removed", "path": "scoring.adx_threshold", "justification": "equivalent", "equals": "state.adx_threshold"},
    {"kind": "added", "path": "entry", "justification": "behavior_change",
     "evidence": {"experiment_id": "X-1", "results": "docs/r.md"}},
    {"kind": "added", "path": "extra", "justification": "disabled_by_default"},
]


def check(changes, *, read=tagged):
    check_config_change(statement(changes), parent_config=PARENT, new_config=NEW,
                        parent_config_hash=sha(PARENT), new_config_hash=sha(NEW), read_tagged=read)


def test_every_change_justified_passes_and_behaviour_changes_are_reported():
    check(GOOD)
    assert behavior_changes(statement(GOOD)) == ["entry"]
    assert behavior_changes({"schema": "config-change-statement/v1", "added": ["x"]}) == []


def test_an_undeclared_or_extra_change_is_rejected():
    with pytest.raises(ValueError, match="differs from the statement"):
        check(GOOD[:2])
    with pytest.raises(ValueError, match="differs from the statement"):
        check(GOOD + [{"kind": "added", "path": "other", "justification": "disabled_by_default"}])


def test_a_behaviour_change_cannot_pass_as_a_disabled_addition():
    disguised = [GOOD[0], {**GOOD[1], "justification": "disabled_by_default"}, GOOD[2]]
    with pytest.raises(ValueError, match="disabled by default"):
        check(disguised)


@pytest.mark.parametrize("evidence,message", [
    ({"experiment_id": "X-2", "results": "docs/r.md"}, "not a completed"),
    ({"experiment_id": "X-9", "results": "docs/r.md"}, "not a completed"),
    ({"experiment_id": "X-1", "results": "docs/missing.md"}, "not in the tagged tree"),
    ({"experiment_id": "X-1"}, "must cite"),
])
def test_behaviour_change_evidence_is_read_from_the_tagged_tree(evidence, message):
    with pytest.raises(ValueError, match=message):
        check([GOOD[0], {**GOOD[1], "evidence": evidence}, GOOD[2]])
    with pytest.raises(ValueError, match="tagged tree"):
        check(GOOD, read=None)


def test_equivalence_must_preserve_the_old_value():
    with pytest.raises(ValueError, match="not preserved"):
        check([{**GOOD[0], "equals": "scoring.weight"}, GOOD[1], GOOD[2]])
    with pytest.raises(ValueError, match="justification must be"):
        check([{**GOOD[0], "justification": "trust_me"}, GOOD[1], GOOD[2]])


def test_the_c1_c2_draft_matches_the_real_v3_1_1_to_current_configuration():
    probe = subprocess.run(["git", "rev-parse", "--verify", "-q", "v3.1.1^{commit}"], cwd=ROOT,
                           capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("v3.1.1 tag is not available in this checkout")
    parent_hash, parent = config_hash_at(ROOT, probe.stdout.strip())
    current = (ROOT / "config/params.yaml").read_bytes().replace(b"\r\n", b"\n")
    draft = json.loads(DRAFT.read_text(encoding="utf-8"))
    check_config_change(draft, parent_config=parent, new_config=current, parent_config_hash=parent_hash,
                        new_config_hash=sha(current),
                        read_tagged=lambda path: (ROOT / path).read_bytes() if (ROOT / path).is_file() else None)
    assert behavior_changes(draft) == ["data.live_min_lookback_bars", "trend_breakout"]
