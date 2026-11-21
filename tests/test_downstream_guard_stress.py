"""Synthetic stress scenarios that reach the downstream risk guards (reality-gap RG-12).

On the historical sample the strategy-health gate and the drawdown budget act first, so the
correlated-risk caps, the post-fill gap check, the leverage limit and the delisting exit were
never the binding constraint: ablating them changed nothing. These scenarios are built so
that each one *is* the binding constraint, run the production engine end to end, and assert
on the audit row that guard writes. Every scenario also has to keep the accounting identity.

The markets are synthetic fixtures, never performance evidence. Expected counts are derived
from the loaded configuration, so a registered change to a limit moves the expectation with it.
"""
from __future__ import annotations

import copy
import logging
import math
from collections import Counter
from contextlib import contextmanager

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from config.config import config
from core.market.universe import PointInTimeUniverse, UniverseMembership

CAPITAL = 10_000.0
SYMBOLS = [f"C{i:02d}-USDT" for i in range(30)]
RISK = config.get("risk")
CAPS = config.get("portfolio_risk")
RISK_PER_TRADE = float(RISK["risk_per_trade"])
SESSION_CAP = float(CAPS["max_same_session_entry_risk"])
CLUSTER_STOP_CAP = float(CAPS["max_correlated_stop_risk"])
# Whole positions that fit each budget when thirty symbols signal together.
SESSION_SLOTS = math.floor(SESSION_CAP / RISK_PER_TRADE + 1e-9)
CLUSTER_SLOTS = math.floor(CLUSTER_STOP_CAP / RISK_PER_TRADE + 1e-9)


@contextmanager
def configured(patch):
    """Run with in-memory configuration overrides; the loaded configuration is restored."""
    original = copy.deepcopy(config._config)

    def merge(base, extra):
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                merge(base[key], value)
            else:
                base[key] = copy.deepcopy(value)

    silenced = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        merge(config._config, patch)
        yield
    finally:
        config._config = original
        logging.disable(silenced)


def trend(*, flat=90, rising=45, daily=0.012, start=100.0, volume=5e9, seed=7):
    """A quiet range, then a steady rise: one TrendBreakout entry once the regime confirms."""
    rng = np.random.default_rng(seed)
    returns = np.concatenate([rng.normal(0, 0.001, flat), np.full(rising, daily) + rng.normal(0, 0.0005, rising)])
    close = start * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[start], close[:-1]])
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.001,
                         "low": np.minimum(open_, close) * 0.999, "close": close, "volume": volume},
                        index=pd.date_range("2024-01-01", periods=flat + rising, freq="D"))


def extend(frame, bars):
    """Append ``bars`` as (open, high, low, close) multiples of the last close."""
    last, volume = float(frame["close"].iloc[-1]), float(frame["volume"].iloc[-1])
    rows = [{"open": last * o, "high": last * h, "low": last * low, "close": last * c, "volume": volume}
            for o, h, low, c in bars]
    index = pd.date_range(frame.index[-1] + pd.Timedelta(days=1), periods=len(bars), freq="D")
    return pd.concat([frame, pd.DataFrame(rows, index=index)])


FLAT_AFTER = (0.75, 0.751, 0.749, 0.75)


def crash_market():
    """Thirty identical trends at different price levels, then every one gaps down 25% and stays there."""
    return {symbol: extend(trend(start=50.0 + 10 * i), [FLAT_AFTER] * 5) for i, symbol in enumerate(SYMBOLS)}


def wick_market():
    """The same trends, then one bar trades down to half and closes back where it opened."""
    return {symbol: extend(trend(start=50.0 + 10 * i), [(1.0, 1.001, 0.5, 1.0)] + [(1.0, 1.001, 0.999, 1.0)] * 3)
            for i, symbol in enumerate(SYMBOLS)}


def run(data, patch=None, **engine_kwargs):
    with configured(patch or {}):
        engine = BacktestEngine(initial_capital=engine_kwargs.pop("initial_capital", CAPITAL), timeframe="1d",
                                calculate_benchmarks=False, **engine_kwargs)
        return engine.run(data, routing_log_enabled=False)


NO_CAPS = {"portfolio_risk": {"enabled": False}}
NO_CAPS_NO_BUDGET = {"portfolio_risk": {"enabled": False}, "drawdown_budget": {"enabled": False}}
SPOT = {"account": {"mode": "spot"},
        "execution": {"fee_schedule": {"venue": "binance", "market_type": "spot", "source": "stress fixture"}}}


@pytest.fixture(scope="module")
def guarded():
    return run(crash_market())


@pytest.fixture(scope="module")
def without_caps():
    return run(crash_market(), NO_CAPS)


@pytest.fixture(scope="module")
def unguarded():
    return run(crash_market(), NO_CAPS_NO_BUDGET)


def day(value):
    return str(value)[:10]


def buys_by_day(result):
    return Counter(day(t["fill_time"]) for t in result["trades"] if t["side"] == "buy")


def exposure_ratio(result):
    curve = result["equity_curve"]
    return curve["gross_exposure"].astype(float) / curve["equity"].astype(float)


def crash_day_loss(result):
    """(fraction of equity lost on the gap bar, gross exposure / equity carried into it)."""
    curve = result["equity_curve"]
    crash = curve.index.get_loc(pd.Timestamp("2024-05-15"))
    before, after = curve["equity"].iloc[crash - 1], curve["equity"].iloc[crash]
    return float(1 - after / before), float(curve["gross_exposure"].iloc[crash - 1] / before)


# ---- S1: thirty symbols signal on one bar, then all gap down 25% ------------------------------------------------

def test_s1_same_session_budget_admits_only_what_fits(guarded):
    audit = guarded["correlated_risk_audit"]
    first_session = audit[0]["session"]
    first = [row for row in audit if row["session"] == first_session]
    assert Counter(row["reason"] for row in first) == {
        "within_budget": SESSION_SLOTS, "same_session_entry_risk_exhausted": len(SYMBOLS) - SESSION_SLOTS}
    assert max(row["session_risk_after"] for row in first) <= SESSION_CAP * CAPITAL + 1e-6
    assert all(row["allowed_risk"] == 0.0 for row in first if not row["allowed"])
    assert buys_by_day(guarded).most_common(1)[0][1] == SESSION_SLOTS
    assert guarded["accounting_check"]["ok"] is True


def test_s1_cluster_stop_risk_cap_limits_the_total_that_is_ever_open(guarded):
    reasons = Counter(row["reason"] for row in guarded["correlated_risk_audit"])
    assert reasons["correlated_stop_risk_scaled"] > 0
    assert sum(buys_by_day(guarded).values()) == CLUSTER_SLOTS
    assert int(guarded["equity_curve"]["priced_symbols"].max()) == CLUSTER_SLOTS
    assert float(exposure_ratio(guarded).max()) <= float(CAPS["max_cluster_exposure_pct"])


def test_s1_the_caps_are_what_binds_on_the_first_session(guarded, without_caps):
    assert {row["reason"] for row in without_caps["correlated_risk_audit"]} == {"no_risk_cap"}
    # Without the caps the drawdown budget is the next limit and lets more entries through at once.
    assert buys_by_day(without_caps).most_common(1)[0][1] > buys_by_day(guarded).most_common(1)[0][1]
    assert without_caps["accounting_check"]["ok"] is True


def test_s1_crash_loss_is_bounded_and_the_breaker_flattens_the_book(guarded):
    loss, carried = crash_day_loss(guarded)
    assert 0 < loss <= carried * 0.25 + 0.005              # no worse than the whole book gapping 25%
    assert loss < 2 * float(config.get("drawdown")["daily_loss_limit"])
    events = [row for row in guarded["breaker_audit"] if row.get("event") == "daily_loss_triggered"]
    assert len(events) == 1 and events[0]["positions_after"] == 0 and events[0]["executed"] is True
    curve = guarded["equity_curve"]
    assert float(curve["gross_exposure"].loc["2024-05-15":].abs().max()) == 0.0
    assert not [t for t in guarded["trades"] if day(t["fill_time"]) > "2024-05-15"]


def test_s1_without_the_downstream_guards_the_same_crash_is_several_times_worse(guarded, unguarded):
    guarded_loss, _ = crash_day_loss(guarded)
    loss, carried = crash_day_loss(unguarded)
    assert loss > 5 * guarded_loss and loss > 0.40
    assert loss <= carried * 0.25 + 0.01
    assert unguarded["accounting_check"]["ok"] is True
    assert unguarded["breaker_audit"][0]["to"] == "locked"   # the account-level breaker is the last line


# ---- S3: every symbol signals at once with the correlated caps and the drawdown budget switched off -------------

def test_s3_margin_account_stops_at_the_leverage_limit(unguarded):
    limit = float(RISK["max_leverage"])
    before_crash = exposure_ratio(unguarded).loc[:"2024-05-14"]
    assert 0.9 * limit < float(before_crash.max()) <= limit
    ledger = [row for row in unguarded["margin_ledger"] if day(row["timestamp"]) < "2024-05-15"]
    assert all(row["available_margin"] >= -1e-6 and not row["liquidation_required"] for row in ledger)
    assert all(row["gross_notional"] <= limit * row["equity"] * (1 + 1e-9) for row in ledger)
    first_session = unguarded["allocation_audit"][0]["timestamp"]
    decisions = Counter(row["reason"] for row in unguarded["allocation_audit"] if row["timestamp"] == first_session)
    assert sum(decisions.values()) == len(SYMBOLS)
    assert 0 < decisions["accepted"] < len(SYMBOLS) and decisions["risk_or_execution_rejected"] > 0


def test_s3_spot_account_cannot_exceed_its_cash():
    result = run(crash_market(), {**NO_CAPS_NO_BUDGET, **SPOT}, account_mode="spot")
    curve = result["equity_curve"]
    assert float(exposure_ratio(result).max()) <= 1.0 + 1e-9
    assert float(curve["cash"].min()) >= -1e-6
    assert result["accounting_check"]["ok"] is True
    first_session = result["allocation_audit"][0]["timestamp"]
    decisions = Counter(row["reason"] for row in result["allocation_audit"] if row["timestamp"] == first_session)
    assert 0 < decisions["accepted"] < len(SYMBOLS)


# ---- S2: the entry fills at an open that gapped 15% away from the signal close ----------------------------------

QUIET = {f"Q{i}-USDT": trend(flat=150, rising=0, seed=20 + i) for i in range(2)}   # the health policy needs 3 symbols
ENTRY_FILL = pd.Timestamp("2024-04-10")


def gap_run(gap):
    frame = trend(rising=60)
    frame.loc[frame.index >= ENTRY_FILL, ["open", "high", "low", "close"]] *= 1 + gap
    return run({"GAP-USDT": frame, **QUIET})


def test_s2_the_fixture_fills_on_the_expected_bar_without_a_breach():
    result = gap_run(0.0)
    first = next(t for t in result["trades"] if t["side"] == "buy")
    assert pd.Timestamp(first["fill_time"]).normalize() == ENTRY_FILL
    assert [row["breached"] for row in result["risk_budget_reconciliation"]] == [False]
    assert not [t for t in result["trades"] if t.get("exit_reason") == "GapRiskResize"]


def test_s2_gap_up_fill_is_resized_back_to_the_approved_risk():
    result = gap_run(0.15)
    tolerance = float(config.get("entry_risk")["tolerance"])
    row = result["risk_budget_reconciliation"][0]
    assert row["breached"] is True and row["action"] == "resize" and row["reason"] == "gap_risk_resize"
    assert row["risk_ratio"] > 1 + tolerance
    assert row["risk_budget"] == pytest.approx(RISK_PER_TRADE * CAPITAL)
    resize = [t for t in result["trades"] if t.get("exit_reason") == "GapRiskResize"]
    assert len(resize) == 1 and resize[0]["side"] == "sell"
    assert resize[0]["qty"] == pytest.approx(row["resize_qty"])
    remaining = row["filled_qty"] - resize[0]["qty"]
    assert remaining == pytest.approx(row["affordable_qty"])
    assert remaining * row["actual_risk_per_unit"] <= row["risk_budget"] * (1 + tolerance)
    assert result["accounting_check"]["ok"] is True


def test_s2_gap_down_fill_below_its_stop_is_closed_by_the_resident_stop_in_the_same_bar():
    result = gap_run(-0.15)
    entry, exit_ = result["trades"][0], result["trades"][1]
    assert entry["side"] == "buy" and exit_["side"] == "sell" and exit_["exit_reason"] == "protective_stop"
    assert day(entry["fill_time"]) == day(exit_["fill_time"]) == day(ENTRY_FILL)
    assert exit_["qty"] == pytest.approx(entry["qty"])
    loss = entry["qty"] * (entry["fill_price"] - exit_["fill_price"])
    assert 0 <= loss < RISK_PER_TRADE * CAPITAL
    assert result["protective_stop_summary"]["unprotected_position_bars"] == 0
    assert result["accounting_check"]["ok"] is True


# ---- resident stops while the drawdown budget trims the positions every bar -------------------------------------

def test_stops_stay_armed_while_positions_are_trimmed_and_fill_on_an_intrabar_wick():
    result = run(wick_market())
    wick_day = "2024-05-15"
    assert Counter(t.get("exit_reason") for t in result["trades"] if t["side"] == "sell")["DrawdownBudgetReduce"] > 100
    stopped = [t for t in result["trades"] if day(t["fill_time"]) == wick_day and t.get("exit_reason") == "protective_stop"]
    assert len(stopped) == CLUSTER_SLOTS
    summary = result["protective_stop_summary"]
    assert summary["triggered_stops"] == CLUSTER_SLOTS and summary["unprotected_position_bars"] == 0
    armed = {row["symbol"]: row for row in result["stop_order_audit"]
             if day(row["timestamp"]) == wick_day and row["reason"] == "stop_triggered"}
    for trade in stopped:
        row = armed[trade["symbol"]]
        assert row["state"] == "armed" and row["protected_qty"] == pytest.approx(trade["qty"])
        assert trade["fill_price"] <= row["effective_stop"]      # filled at the stop less costs, not at the close
    curve = result["equity_curve"]
    assert float(curve["gross_exposure"].loc[wick_day:].abs().max()) == 0.0
    assert result["accounting_check"]["ok"] is True


# ---- S4: a position is still open when the symbol's membership ends ---------------------------------------------

DELISTED_AT = pd.Timestamp("2024-04-25")
LAST_TRADABLE = "2024-04-24"


def delisting_run(*, volume, collapse):
    frame = trend(rising=70, volume=volume)
    frame = frame.loc[frame.index < DELISTED_AT].copy()
    frame.loc[frame.index >= DELISTED_AT - pd.Timedelta(days=5), "volume"] *= collapse   # liquidity dries up first
    universe = PointInTimeUniverse(
        [UniverseMembership("DEL-USDT", pd.Timestamp("2024-01-01"), DELISTED_AT)]
        + [UniverseMembership(symbol, pd.Timestamp("2024-01-01")) for symbol in QUIET])
    return run({"DEL-USDT": frame, **{s: trend(flat=160, rising=0, seed=20 + i) for i, s in enumerate(QUIET)}},
               universe=universe)


def test_s4_position_is_force_closed_on_the_last_tradable_bar_despite_a_ninety_percent_volume_drop():
    result = delisting_run(volume=5e9, collapse=0.1)
    trades = [t for t in result["trades"] if t["symbol"] == "DEL-USDT"]
    assert [t["side"] for t in trades] == ["buy", "sell"]
    assert day(trades[0]["fill_time"]) < LAST_TRADABLE
    assert day(trades[1]["fill_time"]) == LAST_TRADABLE and trades[1]["exit_reason"] == "AnnouncedMarginDelisting"
    assert trades[1]["qty"] == pytest.approx(trades[0]["qty"])
    assert Counter(row["outcome"] for row in result["execution_audit"])["forced_liquidation"] == 1
    curve = result["equity_curve"]
    assert float(curve["gross_exposure"].loc[LAST_TRADABLE:].abs().max()) == 0.0
    assert (result.get("terminal_valuation") or {}).get("status") != "insufficient"   # no stale mark is carried
    assert result["accounting_check"]["ok"] is True


def test_s4_engine_refuses_to_invent_liquidity_when_the_exit_cannot_complete_before_the_cutoff():
    """Current behaviour, and a limitation: the exit is attempted on the last bar only.

    When that bar's volume cannot absorb the position the run stops with an explicit error. It
    does not carry a dead position at a stale price, and it does not model selling down over
    the bars before the cutoff as a desk would after the delisting announcement.
    """
    with pytest.raises(ValueError, match="Insufficient real liquidity to exit DEL-USDT before announced cutoff"):
        delisting_run(volume=2000.0, collapse=0.1)
