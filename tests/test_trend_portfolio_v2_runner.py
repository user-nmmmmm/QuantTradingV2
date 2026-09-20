from copy import deepcopy
import json

import numpy as np
import pandas as pd
import pytest

from config.config import config
from scripts import run_trend_portfolio_v2 as runner


def market_frame(count=155):
    close = 100.0 + np.arange(count, dtype=float)
    return pd.DataFrame({"open": close - .1, "high": close + .2, "low": close - .3,
                         "close": close, "volume": np.full(count, 1_000_000.)},
                        index=pd.date_range("2023-01-01", periods=count, freq="D"))


def test_profiles_preserve_all_non_strategy_settings():
    original = deepcopy(config._config)
    profiles = runner.paired_configs(original)
    assert profiles["baseline"] == original
    for section in original.keys() - {"routing", "research", "strategy_governance"}:
        assert profiles["trend_portfolio_v2"][section] == original[section]
    assert all(profiles["trend_portfolio_v2"]["routing"][state] == runner.V2_NAME
               for state in runner.REGIMES)
    assert profiles["trend_portfolio_v2"]["strategy_governance"][runner.V2_NAME] == "isolated_research"
    profiles["trend_portfolio_v2"]["execution"]["slippage_bps"] += 1
    with pytest.raises(ValueError, match="outside the V2"):
        runner.assert_config_invariants(original, profiles["trend_portfolio_v2"])
    assert config._config == original


def test_failure_restores_configuration_and_isolates_each_arm(tmp_path, monkeypatch):
    prior = config._config
    original = deepcopy(prior)
    frame = market_frame()
    calls = []

    class FailingSecondArm:
        def __init__(self, **options):
            self.options = options

        def run(self, frames, *, strategies, routing_log_enabled):
            calls.append({"config": deepcopy(config._config), "options": self.options,
                          "strategies": set(strategies), "first_close": frames["TEST"]["close"].iloc[0]})
            assert not routing_log_enabled
            frames["TEST"].iloc[0, frames["TEST"].columns.get_loc("close")] = -1
            config._config["execution"]["slippage_bps"] = 999
            if len(calls) == 2:
                raise RuntimeError("deliberate second-arm failure")
            return {"equity_curve": pd.DataFrame({"equity": [10000.]}, index=[frame.index[120]]),
                    "trades": [], "accounting_check": {"ok": True}, "lifecycle": {}}

    monkeypatch.setattr(runner, "BacktestEngine", FailingSecondArm)
    with pytest.raises(RuntimeError, match="second-arm failure"):
        runner.run_paired_comparison({"TEST": frame}, tmp_path / "failure")
    assert config._config is prior
    assert config._config == original
    assert frame["close"].iloc[0] == 100.
    assert [call["first_close"] for call in calls] == [100., 100.]
    assert all(call["config"]["execution"] == original["execution"] for call in calls)
    assert runner.V2_NAME not in calls[0]["strategies"]
    assert runner.V2_NAME in calls[1]["strategies"]
    assert calls[0]["options"]["trading_start"] == calls[1]["options"]["trading_start"]
    assert all(call["options"]["warmup_period"] == 120 for call in calls)
    assert (tmp_path / "failure" / "comparison_failure.json").is_file()
    assert not (tmp_path / "failure" / "comparison.json").exists()


def test_paired_smoke_uses_real_engine_and_writes_auditable_artifacts(tmp_path):
    prior = config._config
    original = deepcopy(prior)
    frame = market_frame()
    output = tmp_path / "paired"
    comparison = runner.run_paired_comparison(
        {"TEST": frame}, output, input_facts={"source_kind": "synthetic_test_fixture"})
    assert config._config is prior
    assert config._config == original
    assert comparison["engineering_status"] == "completed"
    assert comparison["research_status"] == "descriptive_comparison_not_admission_evidence"
    for arm in ("baseline", "trend_portfolio_v2"):
        summary = comparison["arms"][arm]
        assert summary["accounting_check"]["ok"]
        # The shared engine may append its terminal valuation row.
        assert summary["equity_rows"] >= len(frame) - 120
        for filename in ("resolved_config.json", "effective_strategy_config.json", "trades.csv",
                         "equity.csv", "summary.json", "strategy_health.json", "account_cost_contract.json"):
            assert (output / arm / filename).stat().st_size > 0
        fills = pd.read_csv(output / arm / "trades.csv")
        equity = pd.read_csv(output / arm / "equity.csv", index_col=0, parse_dates=True)
        assert equity.index.min() == frame.index[120]
        assert not fills.empty
        entries = fills.loc[fills.side.eq("buy")]
        expected = "TrendBreakout" if arm == "baseline" else runner.V2_NAME
        assert set(entries.strategy_id) == {expected}
        assert pd.to_datetime(entries.fill_time).min() > frame.index[120]
    settings = json.loads((output / "trend_portfolio_v2" / "effective_strategy_config.json").read_text())
    actual = settings[runner.V2_NAME]
    assert actual["horizons"] == [20, 60, 120]
    assert actual["stop_policy"]["initial_stop_mode"] == "atr"
    assert actual["stop_policy"]["initial_atr_multiple"] == 2.
    assert actual["stop_policy"]["trailing_atr_multiple"] == 2.5
    assert actual["health_policy"]["enabled"] == original["strategy_health"]["enabled"]
    assert actual["market_risk_multipliers"]["TREND_DOWN"] == 0.
    baseline_cfg = json.loads((output / "baseline" / "resolved_config.json").read_text())
    v2_cfg = json.loads((output / "trend_portfolio_v2" / "resolved_config.json").read_text())
    runner.assert_config_invariants(baseline_cfg, v2_cfg)
    artifact_hashes = json.loads((output / "artifacts.json").read_text())
    for relative, digest in artifact_hashes.items():
        assert runner.sha256_file(output / relative) == digest
    assert runner.sha256_frame(frame) == json.loads((output / "inputs.json").read_text())["frames"]["TEST"]["sha256"]


def test_explicit_start_rejects_unequal_warmup_before_creating_outputs(tmp_path):
    frame = market_frame()
    with pytest.raises(ValueError, match="120 prior bars"):
        runner.run_paired_comparison({"TEST": frame}, tmp_path / "too_early", trading_start=frame.index[100])
    assert not (tmp_path / "too_early").exists()


def test_local_loader_retains_market_facts_and_records_file_hash(tmp_path):
    frame = market_frame()
    frame["funding_rate"] = .001
    path = tmp_path / "BTC_USDT.csv"
    frame.to_csv(path, index_label="timestamp")
    frames, facts = runner.load_local_frames(tmp_path, [" btc/usdt "])
    assert set(frames) == {"BTC-USDT"}
    assert frames["BTC-USDT"]["funding_rate"].eq(.001).all()
    assert facts["source_kind"] == "local_csv"
    assert facts["files"]["BTC-USDT"]["sha256"] == runner.sha256_file(path)


def test_caller_frames_use_canonical_symbol_keys():
    frames, _ = runner._prepare_inputs({" btc/usdt ": market_frame()}, None, None)
    assert set(frames) == {"BTC-USDT"}


@pytest.mark.parametrize("alias", ["BTC/USDT", "btc_usdt", "BTC-USDT"])
def test_local_loader_rejects_duplicate_symbol_aliases(tmp_path, alias):
    market_frame().to_csv(tmp_path / "BTC_USDT.csv", index_label="timestamp")
    with pytest.raises(ValueError, match="duplicate normalized symbol: BTC-USDT"):
        runner.load_local_frames(tmp_path, ["BTC-USDT", alias])


def test_caller_frames_reject_duplicate_aliases_before_creating_outputs(tmp_path):
    frame = market_frame()
    with pytest.raises(ValueError, match="duplicate normalized symbol: BTC-USDT"):
        runner.run_paired_comparison({"BTC-USDT": frame, "BTC/USDT": frame}, tmp_path / "aliases")
    assert not (tmp_path / "aliases").exists()


def six_markets(count=155):
    return {symbol: market_frame(count) for symbol in runner.SUITE_SYMBOLS}


def test_suite_profiles_keep_account_health_and_execution_and_isolate_arms():
    original = deepcopy(config._config)
    profiles = runner.suite_profiles(original)
    assert tuple(profiles) == runner.SUITE_VARIANTS
    for current in profiles.values():
        for section in ("account", "execution", "risk", "drawdown", "strategy_health", "backtest", "stops"):
            assert current[section] == original[section]
    for name in ("V2-A", "V2-B", "V2-C"):
        assert profiles[name]["portfolio_risk"] == original["portfolio_risk"]
        assert profiles[name]["research"]["trend_portfolio_v2"]["asset_base_weight"] == .5
    opts = [profiles[name]["research"]["trend_portfolio_v2"] for name in runner.SUITE_VARIANTS[1:]]
    assert [item["market_state_mode"] for item in opts] == ["hard_gate", "risk_multiplier", "risk_multiplier", "risk_multiplier"]
    assert [item["exit_mode"] for item in opts] == ["baseline", "baseline", "atr", "atr"]
    assert all(item["volatility_sizing"] for item in opts)
    assert opts[-1]["asset_base_weight"] == 1 / 6
    assert profiles["V2-D"]["portfolio_risk"]["max_crypto_beta_stop_risk"] == .03
    assert profiles["V2-D"]["portfolio_risk"]["max_same_session_entry_risk"] == .02
    assert config._config == original


def test_suite_plan_is_finite_disjoint_and_does_not_select_a_winner():
    specs = runner.suite_specs(["binance", "okx"], "2022-01-01", "2026-09-18")
    assert len(specs) == 128
    assert len({row["run_id"] for row in specs}) == len(specs)
    assert {row["cost_multiplier"] for row in specs} == {1., 1.5, 2.}
    neighbors = [row for row in specs if row["role"] == "neighbor"]
    assert {(row["stability"], row["cooldown"]) for row in neighbors} == {(2, 0), (2, 2), (3, 2), (5, 0), (5, 2)}
    assert all(row["variant"] == "V2-C" and row["timeframe"] == "1d" for row in neighbors)
    rolling = [row for row in specs if row["variant"] == "V2-C" and row["venue"] == "binance" and row["role"] == "rolling"]
    assert all(first["end"] < second["start"] for first, second in zip(rolling, rolling[1:]))


def test_suite_registration_precedes_execution_and_failures_remain_in_ledger(tmp_path, monkeypatch):
    prior = config._config
    original = deepcopy(prior)
    frames = {"test": six_markets()}
    root = tmp_path / "suite"
    monkeypatch.setattr(runner, "_source_hashes", lambda: {"test.py": "frozen"})
    class BrokenEngine:
        def __init__(self, **kwargs):
            protocol = json.loads((root / "preregistration.json").read_text())
            assert protocol["acceptance_thresholds"]["minimum_cohorts"] == 30
            assert (root / "attempts.jsonl").exists()
            config._config["execution"]["slippage_bps"] = 999
            raise RuntimeError("deliberate suite failure")
    monkeypatch.setattr(runner, "BacktestEngine", BrokenEngine)
    report = runner.run_research_suite(frames, root, trading_start="2023-05-01", end="2023-06-04")
    assert report["status"] == "incomplete"
    assert config._config is prior and config._config == original
    ledger = [json.loads(line) for line in (root / "attempts.jsonl").read_text().splitlines()]
    count = report["trial_metadata"]["registered_run_count"]
    assert [row["event"] for row in ledger[:count]] == ["registered"] * count
    assert sum(row["event"] == "started" for row in ledger) == count
    assert sum(row["event"] == "failed" for row in ledger) == count
    before = (root / "attempts.jsonl").read_text()
    resumed = runner.run_research_suite(frames, root, trading_start="2023-05-01", end="2023-06-04", resume=True)
    assert resumed["status"] == "incomplete"
    assert (root / "attempts.jsonl").read_text() == before


def test_suite_resume_rejects_changed_data_before_new_attempt(tmp_path, monkeypatch):
    frames = {"test": six_markets()}
    root = tmp_path / "frozen"
    monkeypatch.setattr(runner, "_source_hashes", lambda: {"test.py": "frozen"})
    runner.run_research_suite(frames, root, trading_start="2023-05-01", end="2023-06-04", register_only=True)
    before = (root / "attempts.jsonl").read_text()
    frames["test"]["BTC-USDT"].iloc[-1, 0] += .01
    with pytest.raises(ValueError, match="changed; refusing resume"):
        runner.run_research_suite(frames, root, trading_start="2023-05-01", end="2023-06-04", resume=True)
    assert (root / "attempts.jsonl").read_text() == before


def test_suite_later_listing_has_no_synthetic_history():
    frames = six_markets(250)
    frames["SOL-USDT"] = frames["SOL-USDT"].iloc[150:].copy()
    spec = {"variant": "V2-D", "start": "2023-05-01", "end": "2023-09-07", "cost_multiplier": 1.}
    current = runner._suite_market_frames(frames, spec)
    assert current["SOL-USDT"].index[0] == frames["SOL-USDT"].index[0]
    assert len(current["SOL-USDT"]) == len(frames["SOL-USDT"])


def test_suite_cost_replays_scale_all_contract_components_without_mutation():
    profiles = runner.suite_profiles(config._config)
    original = deepcopy(profiles)
    current = runner._suite_settings(profiles, {"variant": "V2-C", "cost_multiplier": 2.})
    for field in ("commission_rate_taker", "commission_rate_maker", "slippage_bps", "spread_bps", "volatility_slippage_factor", "impact_coefficient"):
        assert current["execution"][field] == 2 * profiles["V2-C"]["execution"][field]
    assert current["account"]["default_borrow_rate_annual"] == 2 * profiles["V2-C"]["account"]["default_borrow_rate_annual"]
    assert current["strategy_health"] == profiles["V2-C"]["strategy_health"]
    assert profiles == original


def test_real_suite_arm_exports_authoritative_closes_and_verified_benchmark(tmp_path):
    frames = six_markets(180)
    universe = runner.PointInTimeUniverse(runner.UniverseMembership(symbol, frame.index[0])
                                         for symbol, frame in frames.items())
    spec = {"run_id": "fixture__V2-C__main", "variant": "V2-C", "venue": "fixture", "timeframe": "1d",
            "role": "primary", "start": "2023-05-01", "end": "2023-06-29", "recent_start": "2023-06-01",
            "cost_multiplier": 1.}
    prior = config._config
    result = runner._execute_suite_run(spec, runner.suite_profiles(prior), frames, universe, tmp_path / "arm", 10000.)
    assert config._config is prior
    assert result["accounting_check"]["ok"]
    assert result["sample_classification"] == "previously_viewed_retrospective_research"
    assert not result["unseen_oos"]
    for name in ("round_trips.csv", "close_events.json", "exit_day_cohorts.json", "benchmark_diagnostic.json", "summary.json"):
        assert (tmp_path / "arm" / name).is_file()
    benchmark = result["benchmark_diagnostic"]
    assert benchmark["risk_match_verified"]
    assert abs(benchmark["risk_match_annualized_volatility_gap"]) < 1e-8
    assert all(row.get("exit_reason") != "EndOfBacktest" for row in result["trades"])
    assert result["financing_gross"] >= abs(result["financing_total"])
    assert result["cohort_financing_allocated"] is False


def test_paired_non_daily_requires_independent_protocol_before_output(tmp_path):
    with pytest.raises(ValueError, match="daily only"):
        runner.run_paired_comparison({"TEST": market_frame()}, tmp_path / "not_daily", timeframe="4h")
    assert not (tmp_path / "not_daily").exists()
