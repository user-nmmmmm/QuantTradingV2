"""B8 (ADR 0026): every reason a manifest replay is refused keeps its exact message, and exit code 7."""
from __future__ import annotations

import pytest

from cli import replay


def _refusal(execution):
    with pytest.raises(replay._ReplayRefused) as caught:
        replay._restore_research_policies(execution)
    return str(caught.value)


ENABLED_P1 = {"enabled": True}
P0 = {"enabled": True, "horizons": [5]}


@pytest.mark.parametrize("execution, message", [
    ({"signal_meta_layer": ENABLED_P1}, "enabled P1 policy has no recorded research digest."),
    ({"signal_adaptive": {"enabled": "maybe-not-a-bool", "horizon_bars": "x"}, "signal_meta_layer": {"enabled": False}},
     "invalid P2/P3 research policy"),
    ({"signal_meta_layer": ENABLED_P1, "signal_meta_layer_digest": "d", "signal_adaptive": {"enabled": True},
      "signal_observation": P0},
     "enabled signal_adaptive policy has no recorded research digest."),
    ({"signal_meta_layer": ENABLED_P1, "signal_meta_layer_digest": "d", "signal_meta_replay": {"enabled": True, "horizon_bars": 5},
      "signal_meta_replay_digest": "d", "signal_observation": P0},
     "P2/P3 research dependencies were not recorded as enabled."),
    ({"signal_meta_layer": {"enabled": False}, "signal_adaptive": {"enabled": True}, "signal_adaptive_digest": "d"},
     "P2/P3 research dependencies were not recorded as enabled."),
    ({"signal_meta_layer": ENABLED_P1, "signal_meta_layer_digest": "d", "signal_adaptive": {"enabled": True},
      "signal_adaptive_digest": "d", "signal_observation": {"enabled": False}},
     "P2 requires a recorded enabled P0 policy and digest."),
    ({"signal_meta_layer": ENABLED_P1, "signal_meta_layer_digest": "d", "signal_adaptive": {"enabled": True},
      "signal_adaptive_digest": "d", "signal_observation": P0},
     "P2 requires a recorded enabled P0 policy and digest."),                       # enabled, but no recorded digest
])
def test_each_research_precondition_refuses_with_its_message(execution, message):
    assert _refusal(execution).startswith(message)


def test_the_p3_horizon_must_be_one_of_the_recorded_p0_horizons():
    execution = {"signal_meta_layer": ENABLED_P1, "signal_meta_layer_digest": "d",
                 "signal_adaptive": {"enabled": True}, "signal_adaptive_digest": "d",
                 "signal_meta_replay": {"enabled": True, "horizon_bars": 9}, "signal_meta_replay_digest": "d",
                 "signal_observation": P0, "signal_observation_digest": "d"}
    assert _refusal(execution) == "P3 horizon is absent from the recorded P0 horizons."
    execution["signal_meta_replay"]["horizon_bars"] = 5
    meta, adaptive, replay_policy = replay._restore_research_policies(execution)
    assert meta["enabled"] and adaptive.enabled and replay_policy.enabled


def test_a_manifest_without_research_is_accepted_and_does_not_inherit_a_later_enabled_model():
    meta, adaptive, replay_policy = replay._restore_research_policies({})
    assert meta == {"enabled": False} and not adaptive.enabled and not replay_policy.enabled


def test_symbol_order_must_match_the_snapshots_and_is_applied_to_them():
    snapshots = {"A": 1, "B": 2, "C": 3}
    assert replay._in_recorded_symbol_order({}, snapshots) is snapshots                  # nothing recorded: unchanged
    ordered = replay._in_recorded_symbol_order({"data_symbol_order": ["C", "A", "B"]}, snapshots)
    assert list(ordered) == ["C", "A", "B"] and ordered == snapshots
    for recorded in (["A", "B"], ["A", "B", "D"], ["A", "A", "B"]):
        with pytest.raises(replay._ReplayRefused, match="input symbol order does not match snapshots."):
            replay._in_recorded_symbol_order({"data_symbol_order": recorded}, snapshots)


def test_replay_manifest_turns_a_refusal_into_stderr_text_and_exit_code_7(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(replay, "load_manifest", lambda path: {
        "code": {}, "config": {"sha256": "x"}, "data_snapshots": {}, "run_id": "r",
        "execution": {"signal_meta_layer": ENABLED_P1}})
    monkeypatch.setattr(replay, "sha256_file", lambda path: "x")
    monkeypatch.setattr(replay, "load_data_snapshots", lambda *args, **kwargs: {})
    monkeypatch.setattr(replay, "_restore_temporal_replay_inputs", lambda *args: None)
    monkeypatch.setattr(replay, "_restore_temporal_financing", lambda *args: None)
    assert replay.replay_manifest(str(tmp_path / "manifest.json")) == 7
    assert capsys.readouterr().err.strip() == "Replay refused: enabled P1 policy has no recorded research digest."
