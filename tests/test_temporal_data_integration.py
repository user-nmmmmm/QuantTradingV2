"""Temporal boundaries tested through CSV, collector, historical and live paths."""
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from core.data_fetcher import DataFetcher
from core.data_versions import DataVersionStore
from core.market_data import HistoricalMarketDataAdapter, LiveMarketDataAdapter
from core.temporal_data import TemporalDataError, TemporalOHLCV, freeze_ohlcv


def raw(periods=4):
    return pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 1000.},
                        index=pd.date_range("2024-01-01", periods=periods, tz="UTC"))


def documented(periods=4):
    frame = raw(periods)
    frame["available_at"] = frame.index+pd.Timedelta(days=1)
    frame["observed_at"] = frame["available_at"]
    frame["revision_id"] = "original"
    frame.attrs["availability_evidence"] = {"kind": "source_publication", "reference": "archive:verified"}
    return frame


def strict(tmp_path, **changes):
    return {"mode": "strict", "store_path": str(tmp_path / "versions"), **changes}


def test_unknown_old_data_stays_retrospective_and_strict_skips_strategy_not_matching(tmp_path):
    legacy = HistoricalMarketDataAdapter({"BTC/USDT": raw()}, timeframe="1d", calculate_indicators=False)
    assert legacy.temporal_audit["symbols"]["BTC/USDT"]["historical_availability"] == "unknown"
    adapter = HistoricalMarketDataAdapter({"BTC/USDT": raw()}, timeframe="1d",
        calculate_indicators=False, temporal_policy=strict(tmp_path))
    events = list(adapter.stream())
    assert len(events) == 4 and all("BTC/USDT" in event.bars for event in events)
    assert all(event.histories["BTC/USDT"].empty and not event.positions for event in events)
    assert adapter.temporal_audit["symbols"]["BTC/USDT"]["excluded_unknown"] > 0
    with pytest.raises(TemporalDataError, match="availability unknown"):
        list(HistoricalMarketDataAdapter({"BTC/USDT": raw()}, timeframe="1d",
            calculate_indicators=False, temporal_policy=strict(tmp_path, unknown="raise")).stream())


def test_decision_uses_candle_close_and_late_bar_is_not_traded_at_earlier_date(tmp_path):
    frame = documented()
    frame.loc[frame.index[1], "available_at"] += pd.Timedelta(seconds=1)
    frame.loc[frame.index[1], "observed_at"] += pd.Timedelta(seconds=1)
    adapter = HistoricalMarketDataAdapter({"BTC/USDT": frame}, timeframe="1d",
        calculate_indicators=False, temporal_policy=strict(tmp_path))
    events = list(adapter.stream())
    assert events[0].timestamp == pd.Timestamp("2024-01-01")
    assert events[0].positions == {"BTC/USDT": 0}  # known at Jan 2 close boundary
    assert "BTC/USDT" in events[1].bars and not events[1].positions
    assert pd.Timestamp("2024-01-02") not in events[1].histories["BTC/USDT"].index
    assert pd.Timestamp("2024-01-02") in events[2].histories["BTC/USDT"].index
    delayed = HistoricalMarketDataAdapter({"BTC/USDT": frame}, timeframe="1d",
        calculate_indicators=False, temporal_policy=strict(tmp_path, decision_delay_seconds=1))
    assert list(delayed.stream())[1].positions == {"BTC/USDT": 1}


def test_late_revision_only_changes_future_visible_history_and_recomputed_indicators(tmp_path, monkeypatch):
    frame = documented()
    amended = frame.iloc[[0]].copy()
    amended.loc[:, ["open", "high", "low", "close"]] = [900., 901., 899., 900.]
    amended["revision_id"] = "revised"
    amended["available_at"] = amended["observed_at"] = pd.Timestamp("2024-01-04", tz="UTC")
    versions = pd.concat([frame, amended])
    versions.attrs = frame.attrs.copy()
    versions["future_indicator"] = 12345.
    monkeypatch.setattr("core.market_data.Indicators.calculate_all",
        lambda visible: visible.__setitem__("causal_mean", visible.close.rolling(2, min_periods=1).mean()))
    adapter = HistoricalMarketDataAdapter({"BTC/USDT": versions}, timeframe="1d", temporal_policy=strict(tmp_path))
    events = list(adapter.stream())
    assert events[0].bars["BTC/USDT"]["close"] == 900  # realised execution frame is separate
    assert events[0].histories["BTC/USDT"].iloc[0].close == 100
    assert events[1].histories["BTC/USDT"].iloc[-1].causal_mean == 100
    assert events[2].histories["BTC/USDT"].iloc[1].causal_mean == 500
    assert "future_indicator" not in events[2].histories["BTC/USDT"]
    assert events[0].histories["BTC/USDT"].iloc[0].close == 100  # prior emitted view did not mutate
    assert len(adapter.temporal_identity["BTC/USDT"]["snapshot_ids"]) == 1


def test_publication_and_collector_history_are_explicitly_different(tmp_path):
    frame = documented(1)
    frame["observed_at"] = pd.Timestamp("2024-02-01", tz="UTC")
    local = HistoricalMarketDataAdapter({"BTC/USDT": frame}, timeframe="1d",
        calculate_indicators=False, temporal_policy=strict(tmp_path))
    published = HistoricalMarketDataAdapter({"BTC/USDT": frame}, timeframe="1d",
        calculate_indicators=False, temporal_policy=strict(tmp_path, knowledge="published"))
    assert not list(local.stream())[0].positions
    assert list(published.stream())[0].positions


def test_timestamp_column_without_provenance_is_still_unknown(tmp_path):
    frame = documented(1)
    frame.attrs = {}
    versions = TemporalOHLCV("csv", timeframe="1d", policy=strict(tmp_path))
    versions.ingest(frame)
    assert versions.records[0]["available_at"] is None
    assert versions.as_of("2024-03-01").empty


def test_incremental_capture_reuses_unchanged_observations_and_recovers_after_restart(tmp_path):
    policy = strict(tmp_path)
    versions = TemporalOHLCV("live", timeframe="1d", policy=policy)
    frame = raw(2)
    versions.ingest(frame, observed_at="2024-01-03T00:00:03Z", local_receipt=True)
    versions.ingest(frame, observed_at="2024-01-03T00:00:04Z", local_receipt=True)
    assert len(versions.snapshot_ids) == 1
    revised = frame.copy()
    revised.iloc[0, revised.columns.get_loc("close")] = 100.5
    versions.ingest(revised, observed_at="2024-01-04T00:00:03Z", local_receipt=True)
    assert len(versions.snapshot_ids) == 2
    assert DataVersionStore(policy["store_path"]).read_snapshot(versions.snapshot_ids[-1])["manifest"]["record_count"] == 1
    restored = TemporalOHLCV("live", timeframe="1d", policy=policy)
    assert restored.as_of("2024-01-03T00:00:04Z").iloc[0].close == 100
    assert restored.as_of("2024-01-04T00:00:04Z").iloc[0].close == 100.5


def test_unclosed_capture_is_not_a_published_closed_bar(tmp_path):
    versions = TemporalOHLCV("live", timeframe="1d", policy=strict(tmp_path, unknown="raise"))
    versions.ingest(raw(1), observed_at="2024-01-01T12:00:00Z", local_receipt=True)
    assert versions.records[0]["available_at"] is None
    versions.ingest(raw(1), observed_at="2024-01-02T00:00:01Z", local_receipt=True)
    assert len(versions.as_of("2024-01-02T00:00:02Z")) == 1


def test_frozen_identity_detects_changed_ohlcv(tmp_path):
    frame = freeze_ohlcv(documented(2), symbol="BTC/USDT", timeframe="1d", policy=strict(tmp_path))
    frame.iloc[0, frame.columns.get_loc("close")] = 100.2
    with pytest.raises(TemporalDataError, match="differ from frozen"):
        HistoricalMarketDataAdapter({"BTC/USDT": frame}, timeframe="1d", temporal_policy=strict(tmp_path))


def test_main_csv_path_preserves_raw_bytes_and_unknown_history(tmp_path):
    from main import get_data
    source = tmp_path / "BTC_USDT.csv"
    raw(4).to_csv(source, index_label="timestamp")
    original = source.read_bytes()
    policy = strict(tmp_path)
    frame = get_data("BTC/USDT", "2024-01-01", "2024-01-04", source="local",
                     data_dir=str(tmp_path), temporal_policy=policy)
    store = DataVersionStore(policy["store_path"])
    assert store.read_file(frame.attrs["temporal_raw_file_snapshot"], source.name) == original
    assert frame.attrs["temporal_audit"]["unknown_rows"] == 4


def test_ccxt_fake_source_uses_real_response_receipt_not_nominal_close(tmp_path):
    client = MagicMock()
    client.fetch_ohlcv.return_value = [[1704067200000, 100., 101., 99., 100., 1000.]]
    with patch("ccxt.binance", return_value=client):
        fetcher = DataFetcher(proxy_url=None, temporal_policy=strict(tmp_path),
            clock=lambda: pd.Timestamp("2024-01-10T00:00:00Z"))
        frame = fetcher.fetch_ccxt("BTC/USDT", limit=1, exchange_id="binance")
    identity = frame.attrs["temporal_identity"]
    records = DataVersionStore(identity["store_path"]).read_snapshot(identity["snapshot_ids"][0])["records"]
    assert records[0]["available_at"] == "2024-01-10T00:00:00.000000Z"
    assert records[0]["availability_evidence"]["kind"] == "local_receipt"
    assert records[0]["published_at"] is None


def test_live_fake_refresh_and_historical_poll_cutoff_do_not_leak_revision(tmp_path):
    class Fake:
        frame = raw(2)
        def fetch_ccxt(self, *args, **kwargs):
            return self.frame.copy()
    source = Fake()
    now = [pd.Timestamp("2024-01-03T00:00:03Z")]
    adapter = LiveMarketDataAdapter(["BTC/USDT"], source, timeframe="1d", temporal_policy=strict(tmp_path),
                                   clock=lambda: now[0])
    assert len(adapter.poll(now[0])) == 2
    source.frame.iloc[0, source.frame.columns.get_loc("close")] = 100.5
    now[0] = pd.Timestamp("2024-01-04T00:00:03Z")
    adapter.refresh()
    assert adapter.data_map["BTC/USDT"].iloc[0].close == 100.5
    assert adapter._temporal_histories["BTC/USDT"].as_of("2024-01-03T00:00:04Z").iloc[0].close == 100
    assert len(adapter.temporal_identity["BTC/USDT"]["snapshot_ids"]) == 2


def test_frozen_source_identity_does_not_silently_follow_new_store_head(tmp_path):
    versions = TemporalOHLCV("frozen", timeframe="1d", policy=strict(tmp_path))
    old = versions.ingest(raw(1), observed_at="2024-01-02T00:00:00Z", local_receipt=True)
    revised = raw(1)
    revised["close"] = 100.5
    versions.ingest(revised, observed_at="2024-01-03T00:00:00Z", local_receipt=True)
    reader = TemporalOHLCV("frozen", timeframe="1d", policy=strict(tmp_path))
    assert len(reader.records) == 2
    reader.import_identity(old)
    assert len(reader.records) == 1
    assert reader.as_of("2024-02-01").iloc[0].close == 100.


def test_reimport_changed_legacy_csv_appends_version_without_rewriting_original(tmp_path):
    from main import get_data
    path = tmp_path / "BTC_USDT.csv"
    frame = raw(2)
    frame.to_csv(path)
    arguments = dict(source="local", data_dir=str(tmp_path), temporal_policy=strict(tmp_path))
    original = get_data("BTC/USDT", "2024-01-01", "2024-01-02", **arguments)
    frame.iloc[0, frame.columns.get_loc("close")] = 100.5
    frame.to_csv(path)
    changed = get_data("BTC/USDT", "2024-01-01", "2024-01-02", **arguments)
    assert len(changed.attrs["temporal_identity"]["snapshot_ids"]) == 2
    identity = original.attrs["temporal_identity"]
    first = DataVersionStore(identity["store_path"]).read_snapshot(identity["snapshot_ids"][0])
    assert first["records"][0]["data"]["close"] == 100
    assert all(record["available_at"] is None for record in first["records"])


def test_p0_skips_unavailable_current_without_error_and_uses_true_cutoff(tmp_path):
    from types import SimpleNamespace
    from tests.test_signal_observation import observer_for
    from core.portfolio import Portfolio
    from core.risk import RiskManager

    observer = observer_for()
    adapter = HistoricalMarketDataAdapter({"X": raw()}, timeframe="1d", temporal_policy=strict(tmp_path))
    event = next(iter(adapter.stream()))
    observer.observe(event, "X", portfolio=Portfolio(10000), risk_manager=RiskManager(),
                     router=SimpleNamespace(regime_map={}), audit={})
    assert not observer.errors and not observer.candidates
    assert observer.coverage["temporal:unavailable_current_bar"] == 1
    known = HistoricalMarketDataAdapter({"X": documented()}, timeframe="1d",
        temporal_policy=strict(tmp_path / "documented", decision_delay_seconds=3))
    event = list(known.stream())[1]
    observer.observe(event, "X", portfolio=Portfolio(10000), risk_manager=RiskManager(),
                     router=SimpleNamespace(regime_map={}), audit={})
    assert not observer.errors and observer.candidates
    assert pd.Timestamp(observer.candidates[-1].context.available_at) == pd.Timestamp("2024-01-03T00:00:03Z")


@pytest.mark.parametrize("held,weight", [(False, .1), (True, .1), (True, 0.)])
def test_portfolio_controller_blocks_ordinary_changes_using_unavailable_current(held, weight):
    from dataclasses import replace
    from types import SimpleNamespace
    from tests.test_v3_portfolio_execution import SYMBOL, event, setup_controller

    controller, broker, risk, strategy = setup_controller({SYMBOL: weight})
    if held:
        broker.portfolio.update_position(SYMBOL, 2, 100, 0, strategy_id=strategy.name,
                                        stop_price=95, approved_risk_amount=10)
    realised = event()
    market = replace(realised, source="historical_strict", positions={},
        histories={SYMBOL: realised.histories[SYMBOL].iloc[:0]})
    orders = controller.process(event=market, portfolio=broker.portfolio, broker=broker,
        risk_manager=risk, current_prices={SYMBOL: 100.}, risk_decision=SimpleNamespace(allow_new_entries=True))
    assert orders == []
    assert "temporal_current_bar_unavailable" in controller.audit[-1]["symbol_decisions"][SYMBOL]["reasons"]


def test_portfolio_forced_exit_is_allowed_despite_unavailable_current():
    from dataclasses import replace
    from types import SimpleNamespace
    from tests.test_v3_portfolio_execution import SYMBOL, event, setup_controller, bars

    controller, broker, risk, strategy = setup_controller()
    broker.portfolio.update_position(SYMBOL, 2, 100, 0, strategy_id=strategy.name,
                                    stop_price=95, approved_risk_amount=10)
    provider = controller.target_provider
    controller.target_provider = lambda **kwargs: {**provider(**kwargs), "forced_exits": [SYMBOL]}
    realised = event()
    market = replace(realised, source="historical_strict", positions={},
        histories={SYMBOL: realised.histories[SYMBOL].iloc[:0]})
    orders = controller.process(event=market, portfolio=broker.portfolio, broker=broker,
        risk_manager=risk, current_prices={SYMBOL: 100.}, risk_decision=SimpleNamespace(allow_new_entries=True))
    assert len(orders) == 1 and orders[0].side == "sell"
    broker.process_orders(bars(1))
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0


def test_mandatory_risk_reduction_remains_executable_with_unknown_current():
    from dataclasses import replace
    from types import SimpleNamespace
    from tests.test_cost_aware_allocation import cost_controller
    from tests.test_v3_portfolio_execution import SYMBOL, event, bars

    controller, broker, risk, strategy = cost_controller(max_turnover=.001, fail=True)
    opening = broker.submit_order(SYMBOL, "buy", 20, 100, timestamp=bars(-2)[SYMBOL].name,
                                  stop_loss=95, approved_risk_amount=100, strategy_id=strategy.name)
    broker.process_orders(bars(-1))
    assert opening.filled_qty == 20
    realised = event()
    market = replace(realised, source="historical_strict", positions={},
        histories={SYMBOL: realised.histories[SYMBOL].iloc[:0]})
    orders = controller.process(event=market, portfolio=broker.portfolio, broker=broker,
        risk_manager=risk, current_prices={SYMBOL: 100.}, risk_decision=SimpleNamespace(allow_new_entries=True))
    assert len(orders) == 1 and orders[0].exit_reason == "v3_risk_reduction"
    broker.process_orders(bars(1))
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0


@pytest.mark.parametrize("through_runtime", [False, True])
@pytest.mark.parametrize("visible_volume,expected_qty", [(10000., 10.), (2., 2.)])
def test_strategy_order_price_volume_and_risk_ignore_later_realised_revision(through_runtime, visible_volume, expected_qty):
    from dataclasses import replace
    from types import SimpleNamespace
    from core.runtime import EventProcessor
    from tests.test_v3_portfolio_execution import SYMBOL, event, setup_controller

    controller, broker, risk, _ = setup_controller()
    original = event(volume=visible_volume)
    realised = event(price=900., volume=1000000.)
    market = replace(original, bars=realised.bars, source="historical_strict")
    if through_runtime:
        runtime = EventProcessor(portfolio=broker.portfolio, execution=broker, risk_manager=risk,
            state_machine=SimpleNamespace(get_state=lambda *args: "normal"),
            router=SimpleNamespace(collect_candidate=lambda *args: None),
            allocator=SimpleNamespace(), portfolio_controller=controller)
        result = runtime.process(market)
        assert result.prices[SYMBOL] == 900.
        orders = list(broker.pending_orders)
    else:
        orders = controller.process(event=market, portfolio=broker.portfolio, broker=broker,
            risk_manager=risk, current_prices={SYMBOL: 900.}, risk_decision=SimpleNamespace(allow_new_entries=True))
    assert len(orders) == 1
    assert orders[0].qty == pytest.approx(expected_qty)
    assert orders[0].price == 100.
    assert controller._state["targets"][SYMBOL]["reference_price"] == 100.


def test_runtime_risk_budget_and_daily_baseline_use_visible_marks_but_report_realised_equity():
    from dataclasses import replace
    from types import SimpleNamespace
    from core.runtime import EventProcessor
    from tests.test_v3_portfolio_execution import SYMBOL, event, setup_controller

    _, broker, risk, strategy = setup_controller()
    broker.portfolio.update_position(SYMBOL, 2, 100, 0, strategy_id=strategy.name)
    checks, updates, routed, allocated = [], [], [], []
    risk.check_circuit_breaker = lambda equity, baseline, **kwargs: checks.append((equity, baseline)) or False
    risk.drawdown_budget = SimpleNamespace(bind=lambda *args: None,
        update=lambda prices, bars, stamp: updates.append((dict(prices), dict(bars))))
    router = SimpleNamespace(collect_candidate=lambda *args: routed.append(args[-1]) or None)
    runtime = EventProcessor(portfolio=broker.portfolio, execution=broker, risk_manager=risk,
        state_machine=SimpleNamespace(get_state=lambda *args: "normal"), router=router,
        allocator=SimpleNamespace(allocate=lambda *args, **kwargs: allocated.append(kwargs["current_prices"])),
        initial_equity=10000.)
    for day in (0, 1):
        original = event(day)
        market = replace(original, bars=event(day, price=900.).bars, source="historical_strict")
        result = runtime.process(market)
        assert result.equity == 11600.
    assert checks == [(10000., 10000.), (10000., 10000.)]
    assert all(prices[SYMBOL] == 100. and bars[SYMBOL].close == 100. for prices, bars in updates)
    assert all(prices[SYMBOL] == 100. for prices in routed + allocated)


def test_runtime_missing_held_mark_blocks_new_risk_without_using_realised_revision():
    from dataclasses import replace
    from types import SimpleNamespace
    from core.runtime import EventProcessor
    from tests.test_v3_portfolio_execution import SYMBOL, event, setup_controller

    _, broker, risk, strategy = setup_controller()
    broker.portfolio.update_position(SYMBOL, 2, 100, 0, strategy_id=strategy.name)
    risk.check_circuit_breaker = MagicMock(return_value=False)
    allocator = SimpleNamespace(allocate=MagicMock())
    market = event(price=900.)
    market = replace(market, source="historical_strict", histories={SYMBOL: market.histories[SYMBOL].iloc[:0]}, positions={})
    runtime = EventProcessor(portfolio=broker.portfolio, execution=broker, risk_manager=risk,
        state_machine=SimpleNamespace(get_state=MagicMock()),
        router=SimpleNamespace(collect_candidate=MagicMock()), allocator=allocator)
    result = runtime.process(market)
    assert result.equity == 11600. and not result.risk_decision.allow_new_entries
    assert "temporal_held_mark_unavailable" in result.risk_decision.reason_codes
    assert runtime.temporal_audit[-1]["symbols"] == [SYMBOL]
    risk.check_circuit_breaker.assert_not_called()
    allocator.allocate.assert_not_called()


@pytest.mark.parametrize("persistent", [False, True])
def test_report_csv_replay_restores_identical_strict_versions(tmp_path, persistent):
    from types import SimpleNamespace
    from main import _temporal_report_payload, _restore_temporal_replay_inputs
    from core.backtest_audit import write_json_report
    from core.reproducibility import save_data_snapshots, load_data_snapshots, sha256_file

    policy = strict(tmp_path) if persistent else {"mode": "strict"}
    frames = {"BTC/USDT": documented()}
    adapter = HistoricalMarketDataAdapter(frames, timeframe="1d", temporal_policy=policy)
    engine = SimpleNamespace(temporal_policy=adapter.temporal_policy, market_data_adapter=adapter)
    payload = _temporal_report_payload(engine, {"temporal_data": {"audit": adapter.temporal_audit}}, frames)
    write_json_report(tmp_path / "temporal_data.json", payload)
    entries = save_data_snapshots(frames, tmp_path / "data_inputs")
    reloaded = load_data_snapshots(tmp_path / "data_inputs", entries)
    assert "temporal_identity" not in reloaded["BTC/USDT"].attrs
    execution = {"temporal_policy": payload["policy"],
                 "temporal_data_sha256": sha256_file(tmp_path / "temporal_data.json")}
    restored = _restore_temporal_replay_inputs(reloaded, execution, tmp_path)
    replay = HistoricalMarketDataAdapter(reloaded, timeframe="1d", temporal_policy=restored)
    assert replay.temporal_identity == adapter.temporal_identity
    assert list(replay.stream())[0].histories["BTC/USDT"].iloc[0].close == 100.
    if persistent:
        identity = adapter.temporal_identity["BTC/USDT"]
        (DataVersionStore(identity["store_path"]).root / "snapshots" / (identity["snapshot_ids"][0]+".json")).unlink()
        with pytest.raises((OSError, ValueError)):
            _restore_temporal_replay_inputs(reloaded, execution, tmp_path)


def test_strict_replay_rejects_missing_or_changed_temporal_evidence(tmp_path):
    import json
    from types import SimpleNamespace
    from main import _temporal_report_payload, _restore_temporal_replay_inputs
    from core.backtest_audit import write_json_report
    from core.reproducibility import sha256_file

    frame = documented()
    frames = {"BTC/USDT": frame}
    adapter = HistoricalMarketDataAdapter(frames, timeframe="1d", temporal_policy="strict")
    engine = SimpleNamespace(temporal_policy=adapter.temporal_policy, market_data_adapter=adapter)
    payload = _temporal_report_payload(engine, {}, frames)
    path = tmp_path / "temporal_data.json"
    write_json_report(path, payload)
    execution = {"temporal_policy": payload["policy"]}
    with pytest.raises(TemporalDataError, match="no evidence digest"):
        _restore_temporal_replay_inputs(frames, execution, tmp_path)
    execution["temporal_data_sha256"] = sha256_file(path)
    payload["inputs"]["BTC/USDT"]["records"][0]["available_at"] = "2024-01-03T00:00:00.000000Z"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TemporalDataError, match="report hash mismatch"):
        _restore_temporal_replay_inputs(frames, execution, tmp_path)
    execution["temporal_data_sha256"] = sha256_file(path)
    with pytest.raises(TemporalDataError, match="record digest mismatch"):
        _restore_temporal_replay_inputs(frames, execution, tmp_path)


def test_main_full_strict_csv_report_and_replay_preserve_raw_input_identity(tmp_path, monkeypatch):
    import json
    import main as entrypoint

    frame = documented(15)
    frame["availability_source"] = "source_publication"
    frame["availability_reference"] = "archive:fixture"
    for filename in ("BTC_USDT.csv", "ETH_USDT.csv", "SOL_USDT.csv"):
        frame.to_csv(tmp_path / filename)
    output = tmp_path / "report"
    monkeypatch.setattr(entrypoint.ReportGenerator, "generate", lambda *args, **kwargs: {})
    monkeypatch.setattr(entrypoint, "format_primary_metrics", lambda *args, **kwargs: "fixture metrics")
    assert entrypoint.main(["--source", "local", "--symbols", "BTC/USDT", "ETH/USDT", "SOL/USDT", "--data-dir", str(tmp_path),
        "--start", "2024-01-01", "--end", "2024-01-15", "--temporal-mode", "strict",
        "--data-version-store", str(tmp_path / "versions"), "--report-profile", "full",
        "--output-dir", str(output), "--disable-routing-log"]) == 0
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution"]["temporal_policy"]["mode"] == "strict"
    assert "temporal_data.json" in manifest["artifacts"]
    evidence = json.loads((output / "temporal_data.json").read_text(encoding="utf-8"))
    assert evidence["inputs"]["BTC-USDT"]["raw_file_snapshot"]
    assert entrypoint.replay_manifest(str(output / "run_manifest.json")) == 0


def test_live_strict_controller_uses_actual_receipt_cutoff_for_order_decision():
    from dataclasses import replace
    from types import SimpleNamespace
    from tests.test_v3_portfolio_execution import SYMBOL, event, setup_controller

    controller, broker, risk, _ = setup_controller()
    market = event()
    actual = (market.timestamp.tz_localize("UTC")+pd.Timedelta(days=1, seconds=3)).isoformat()
    market.histories[SYMBOL].attrs["temporal_audit"] = {"mode": "strict", "as_of": actual}
    market = replace(market, source="live")
    orders = controller.process(event=market, portfolio=broker.portfolio, broker=broker,
        risk_manager=risk, current_prices={SYMBOL: 900.}, risk_decision=SimpleNamespace(allow_new_entries=True))
    assert len(orders) == 1 and controller.audit[-1]["as_of"] == actual
