"""Causal daily selection features and independent *proxy* trade labels.

Inputs are candle OPEN timestamps. A decision uses a candle only after its
scheduled close and any supplied availability time. These labels assume a
small, independent fully filled long trade; they are not portfolio returns.
Observed first/last candles do not establish historical exchange membership.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import deque
import math

import numpy as np
import pandas as pd


DAY = pd.Timedelta(days=1)
MIN_FEATURE_HISTORY = 61
FEATURE_COLUMNS = (
    "return_1d", "return_5d", "return_20d", "return_60d",
    "volatility_20d", "volatility_60d", "atr_14_pct",
    "sma_20_distance", "sma_60_distance", "volume_ratio_20d",
    "volume_5_to_20", "log_quote_volume", "breakout_20d",
    "drawdown_60d", "range_pct", "close_location",
    "btc_return_5d", "btc_return_20d", "btc_volatility_20d",
    "relative_return_20d", "benchmark_available",
)
_OUTPUT_COLUMNS = (
    "as_of", "bar_time", "symbol", "eligible", "exclusion_reason",
    "membership_basis", "contiguous_history", "history_available",
    *FEATURE_COLUMNS, "label_net_return", "label_mae", "label_available_at",
    "label_exit_reason", "label_basis",
)


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("A finite timestamp is required")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _positive_int(value, name, minimum=1):
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("Every symbol must map to a DataFrame")
    required = {"open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Missing OHLCV columns: {sorted(required - set(frame.columns))}")
    result = frame.copy()
    result.index = pd.to_datetime(result.index, utc=True, errors="raise")
    if result.index.hasnans or result.index.has_duplicates:
        raise ValueError("Candle open timestamps must be finite and unique")
    result = result.sort_index()
    if len(result) and (result.index != result.index.normalize()).any():
        raise ValueError("Daily candles must have UTC midnight open timestamps")
    for column in required | ({"quote_volume"} & set(result.columns)):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    scheduled = pd.Series(result.index + DAY, index=result.index)
    available = scheduled.copy()
    for column in ("close_time", "available_at"):
        if column in result:
            supplied = pd.to_datetime(result[column], utc=True, errors="coerce")
            # Explicit missing availability evidence fails closed.
            available = available.where(supplied.notna())
            later = supplied.notna() & available.notna() & (supplied > available)
            available.loc[later] = supplied.loc[later]
    result["_available_at"] = available
    values = result[list(sorted(required))].to_numpy(dtype=float)
    good = np.isfinite(values).all(axis=1)
    good &= (result[["open", "high", "low", "close"]] > 0).all(axis=1).to_numpy()
    good &= (result["volume"] >= 0).to_numpy()
    good &= (result["high"] >= result[["open", "close", "low"]].max(axis=1)).to_numpy()
    good &= (result["low"] <= result[["open", "close", "high"]].min(axis=1)).to_numpy()
    if "quote_volume" in result:
        good &= np.isfinite(result["quote_volume"]) & (result["quote_volume"] >= 0)
    result["_valid"] = good
    for column in ("entry_blocked", "scheduled_exit"):
        if column in result:
            # Unknown lifecycle flags must not permit an entry.
            result[column] = result[column].fillna(True).astype(bool)
        else:
            result[column] = False
    return result


def _history_count(frame):
    count = np.zeros(len(frame), dtype=np.int32)
    valid = frame["_valid"].to_numpy(dtype=bool)
    times = frame.index.asi8
    for i in range(len(frame)):
        if valid[i]:
            count[i] = count[i - 1] + 1 if i and times[i] - times[i - 1] == DAY.value else 1
    return count


def _history_available(frame, window):
    # Keep exact integer nanoseconds. Floating rolling maxima can round a
    # timestamp one nanosecond after a decision down to the decision boundary.
    available = frame["_available_at"].astype("int64").to_numpy(copy=True)
    available[available == pd.NaT.value] = np.iinfo(np.int64).max
    cutoffs = frame.index.asi8 + DAY.value
    result = np.zeros(len(frame), dtype=bool)
    maxima = deque()
    for i, stamp in enumerate(available):
        while maxima and maxima[0] <= i - window:
            maxima.popleft()
        while maxima and available[maxima[-1]] <= stamp:
            maxima.pop()
        maxima.append(i)
        if i + 1 >= window:
            result[i] = available[maxima[0]] <= cutoffs[i]
    return result


def _feature_table(frame: pd.DataFrame) -> pd.DataFrame:
    """One shared rolling implementation for offline and decision-time use."""
    close, high, low, volume = (frame[c].where(frame["_valid"]) for c in
                                ("close", "high", "low", "volume"))
    daily = close.pct_change(fill_method=None)
    table = pd.DataFrame(index=frame.index)
    for period in (1, 5, 20, 60):
        table[f"return_{period}d"] = close.pct_change(period, fill_method=None)
    for period in (20, 60):
        table[f"volatility_{period}d"] = daily.rolling(period).std(ddof=0)
        table[f"sma_{period}_distance"] = close / close.rolling(period).mean() - 1
    true_range = pd.concat((high - low, (high - close.shift(1)).abs(),
                            (low - close.shift(1)).abs()), axis=1).max(axis=1)
    table["_atr"] = true_range.rolling(14).mean()
    table["atr_14_pct"] = table["_atr"] / close
    volume_mean = volume.rolling(20).mean().replace(0, np.nan)
    table["volume_ratio_20d"] = volume / volume_mean
    table["volume_5_to_20"] = volume.rolling(5).mean() / volume_mean
    quote_volume = frame.get("quote_volume", volume * close).where(frame["_valid"])
    table["log_quote_volume"] = np.log1p(quote_volume)
    table["_quote_volume"] = quote_volume
    table["breakout_20d"] = close / high.shift(1).rolling(20).max() - 1
    table["drawdown_60d"] = close / close.rolling(60).max() - 1
    table["range_pct"] = (high - low) / close
    table["close_location"] = ((close - low) / (high - low).replace(0, np.nan)).fillna(0.5)
    table["contiguous_history"] = _history_count(frame)
    table["history_available"] = _history_available(frame, MIN_FEATURE_HISTORY)
    return table


def _attach_benchmark(table, benchmark):
    if benchmark is None:
        for column in ("btc_return_5d", "btc_return_20d", "btc_volatility_20d",
                       "relative_return_20d"):
            table[column] = 0.0
        table["benchmark_available"] = 0.0
        return table
    benchmark_table = _feature_table(benchmark)
    usable = (benchmark_table["contiguous_history"] >= 21) & _history_available(benchmark, 21)
    for source, target in (("return_5d", "btc_return_5d"),
                           ("return_20d", "btc_return_20d"),
                           ("volatility_20d", "btc_volatility_20d")):
        table[target] = benchmark_table[source].where(usable).reindex(table.index)
    table["relative_return_20d"] = table["return_20d"] - table["btc_return_20d"]
    table["benchmark_available"] = table["btc_return_20d"].notna().astype(float)
    return table


def feature_snapshot(frame: pd.DataFrame, *, as_of, benchmark=None) -> dict:
    """Return the latest scheduled closed bar's features, never future candles.

    Availability/history flags must be checked by callers. An unavailable last
    candle returns NaN features rather than silently substituting an older bar.
    """
    cutoff = _utc(as_of)
    own = _normalise(frame)
    own = own.loc[own.index + DAY <= cutoff].copy()
    if own.empty:
        return {**dict.fromkeys(FEATURE_COLUMNS, float("nan")), "as_of": cutoff,
                "bar_time": pd.NaT, "contiguous_history": 0, "history_available": False}
    # Mask values not yet available; retain their slots so gaps cannot disappear.
    own.loc[own["_available_at"].isna() | (own["_available_at"] > cutoff), "_valid"] = False
    bench = None
    if benchmark is not None:
        bench = _normalise(benchmark)
        bench = bench.loc[bench.index + DAY <= cutoff].copy()
        bench.loc[bench["_available_at"].isna() | (bench["_available_at"] > cutoff), "_valid"] = False
    table = _attach_benchmark(_feature_table(own), bench)
    row = table.iloc[-1]
    # _history_available uses the latest bar's nominal close; delayed but now
    # known observations may be used by serving at a later explicit cutoff.
    last = own.tail(MIN_FEATURE_HISTORY)
    history_available = (len(last) == MIN_FEATURE_HISTORY
                         and last["_available_at"].notna().all()
                         and (last["_available_at"] <= cutoff).all())
    result = {column: float(row[column]) for column in FEATURE_COLUMNS}
    if not history_available or row["contiguous_history"] < MIN_FEATURE_HISTORY:
        result = dict.fromkeys(FEATURE_COLUMNS, float("nan"))
    return {**result, "as_of": cutoff, "bar_time": own.index[-1],
            "contiguous_history": int(row["contiguous_history"]),
            "history_available": bool(history_available),
            "entry_blocked": bool(own["entry_blocked"].iloc[-1]),
            "scheduled_exit": bool(own["scheduled_exit"].iloc[-1])}


def _membership_at(membership, symbol, dates):
    if membership is None:
        return np.ones(len(dates), dtype=bool), "observed_history_only"
    if isinstance(membership, pd.DataFrame):
        if "symbol" not in membership:
            raise ValueError("membership requires a symbol column")
        selected = membership.loc[membership["symbol"] == symbol]
        if len(selected) > 1:
            raise ValueError("membership supports one listing interval per symbol")
        item = selected.iloc[0].to_dict() if len(selected) else None
    elif isinstance(membership, Mapping):
        item = membership.get(symbol)
    else:
        raise ValueError("membership must be a DataFrame or symbol metadata mapping")
    if item is None:
        return np.zeros(len(dates), dtype=bool), "membership_unknown"
    if not isinstance(item, Mapping) or not {"listed_at", "available_at"}.issubset(item):
        raise ValueError("membership facts require listed_at and available_at")
    listed, available = _utc(item["listed_at"]), _utc(item["available_at"])
    active = (dates >= listed) & (dates >= available)
    delisted = item.get("delisted_at")
    if delisted is not None and pd.notna(delisted):
        # Delisting may only influence a decision after its publication.
        if item.get("delisting_available_at") is None:
            raise ValueError("delisted_at requires delisting_available_at")
        inactive = (dates >= _utc(delisted)) & (dates >= _utc(item["delisting_available_at"]))
        active &= ~inactive
    return np.asarray(active, dtype=bool), "supplied_point_in_time_facts"


def _shadow_labels(frame, table, horizon, commission, slippage, stop_multiple):
    n = len(frame)
    net, mae = np.full(n, np.nan), np.full(n, np.nan)
    maturity = np.full(n, np.datetime64("NaT"), dtype="datetime64[ns]")
    reason = np.full(n, "future_unavailable", dtype=object)
    if not n:
        return net, mae, maturity, reason
    opens, lows, closes = (frame[c].to_numpy(dtype=float) for c in ("open", "low", "close"))
    valid = frame["_valid"].to_numpy(dtype=bool)
    available = frame["_available_at"].astype("int64").to_numpy()
    times = frame.index.asi8
    atr = table["_atr"].to_numpy(dtype=float)
    entry = np.roll(opens, -1)
    entry[-1] = np.nan
    entry_fill = entry * (1 + slippage)
    stop = entry - atr * stop_multiple
    active = valid & np.isfinite(atr) & np.isfinite(entry)
    active &= ~frame["entry_blocked"].to_numpy() & ~frame["scheduled_exit"].to_numpy()
    adverse = np.zeros(n)
    latest = np.zeros(n, dtype=np.int64)
    forced = frame["scheduled_exit"].to_numpy(dtype=bool)
    for step in range(1, horizon + 1):
        indices = np.flatnonzero(active)
        future = indices + step
        in_range = future < n
        active[indices[~in_range]] = False
        indices, future = indices[in_range], future[in_range]
        if not len(indices):
            break
        observable = (valid[future] & (available[future] != pd.NaT.value)
                      & (times[future] == times[indices] + step * DAY.value))
        active[indices[~observable]] = False
        indices, future = indices[observable], future[observable]
        if not len(indices):
            continue
        latest[indices] = np.maximum(latest[indices], available[future])
        gap_stop = opens[future] <= stop[indices]
        intrabar_stop = lows[future] <= stop[indices]
        stopped = gap_stop | intrabar_stop
        # Intrabar stop orders precede forced close; MAE ends at stop execution.
        adverse_price = np.where(gap_stop, opens[future],
                                 np.where(intrabar_stop, stop[indices], lows[future]))
        adverse[indices] = np.minimum(adverse[indices], adverse_price / entry_fill[indices] - 1)
        finishing = stopped | forced[future] | (step == horizon)
        finish_indices, finish_future = indices[finishing], future[finishing]
        if len(finish_indices):
            raw_exit = np.where(gap_stop, opens[future],
                                np.where(intrabar_stop, stop[indices], closes[future]))[finishing]
            exit_fill = raw_exit * (1 - slippage)
            net[finish_indices] = (exit_fill * (1 - commission)
                                   / (entry_fill[finish_indices] * (1 + commission)) - 1)
            mae[finish_indices] = adverse[finish_indices]
            maturity[finish_indices] = latest[finish_indices].astype("datetime64[ns]")
            reason[finish_indices] = np.where(stopped[finishing], "initial_atr_stop",
                                              np.where(forced[finish_future], "scheduled_exit", "horizon_close"))
            active[finish_indices] = False
    return net, mae, maturity, reason


def forward_proxy_outcome(frame: pd.DataFrame, *, entry_not_before,
                          decision_atr_absolute, horizon_bars=20,
                          commission_rate=0.001, slippage_bps=5.0,
                          stop_atr_multiple=2.0, mature_as_of) -> dict:
    """Resolve one frozen small-long proxy observed in real clock time.

    A midday observation can first enter at the next UTC daily open. The exact
    expected entry and consecutive daily execution bars must exist; missing
    bars never move the entry forward or manufacture an outcome. The ATR is
    provided by the original observation, not recomputed using later candles.
    All required OHLCV must have matured by the explicit information cutoff.
    This proxy assumes a small full fill and is not a portfolio return.
    """
    _positive_int(horizon_bars, "horizon_bars")
    parameters = {"decision_atr_absolute": decision_atr_absolute,
                  "commission_rate": commission_rate, "slippage_bps": slippage_bps,
                  "stop_atr_multiple": stop_atr_multiple}
    if any(isinstance(value, bool) or not isinstance(value, (int, float, np.number))
           or not math.isfinite(value) or value < 0 for value in parameters.values()):
        raise ValueError("Frozen ATR and cost parameters must be finite and nonnegative")
    if commission_rate >= 1 or slippage_bps >= 10000 or stop_atr_multiple <= 0:
        raise ValueError("Commission/slippage must be below 100%; stop multiple must be positive")
    entry_time = _utc(entry_not_before).ceil("D")
    cutoff = _utc(mature_as_of)
    prepared = _normalise(frame)
    result = {"status": "pending", "pending_reason": None,
              "entry_time": entry_time, "exit_time": None,
              "label_available_at": None, "label_net_return": None,
              "label_stop_hit": None, "label_mae": None, "exit_reason": None,
              "label_basis": "independent_shadow_proxy_not_portfolio"}

    def pending(reason):
        return {**result, "pending_reason": reason}

    if entry_time > cutoff:
        return pending("entry_not_reached")
    if entry_time not in prepared.index:
        return pending("entry_bar_unavailable")
    if bool(prepared.loc[entry_time, "entry_blocked"]) or bool(prepared.loc[entry_time, "scheduled_exit"]):
        return pending("entry_lifecycle_blocked")
    entry_row = prepared.loc[entry_time]
    if not bool(entry_row["_valid"]):
        return pending("invalid_execution_bar")
    slippage = slippage_bps / 10000
    raw_entry = float(entry_row["open"])
    entry_fill = raw_entry * (1 + slippage)
    stop = raw_entry - decision_atr_absolute * stop_atr_multiple
    maturity, adverse = entry_time, 0.0
    for step in range(horizon_bars):
        bar_time = entry_time + step * DAY
        if bar_time not in prepared.index:
            return pending("missing_execution_history")
        bar = prepared.loc[bar_time]
        if not bool(bar["_valid"]):
            return pending("invalid_execution_bar")
        if float(bar["volume"]) <= 0:
            return pending("execution_liquidity_unavailable")
        available = bar["_available_at"]
        if pd.isna(available) or available > cutoff:
            return pending("execution_bar_not_mature")
        maturity = max(maturity, available)
        gap_stop = float(bar["open"]) <= stop
        stop_hit = gap_stop or float(bar["low"]) <= stop
        raw_exit = float(bar["open"]) if gap_stop else stop if stop_hit else float(bar["close"])
        adverse_price = raw_exit if stop_hit else float(bar["low"])
        adverse = min(adverse, adverse_price / entry_fill - 1)
        forced = bool(bar["scheduled_exit"])
        if stop_hit or forced or step == horizon_bars - 1:
            exit_fill = raw_exit * (1 - slippage)
            return {**result, "status": "resolved", "pending_reason": None,
                    # A daily intrabar stop has no provable exact fill time;
                    # its candle close is a conservative observation boundary.
                    "exit_time": bar_time if gap_stop else bar_time + DAY,
                    "label_available_at": maturity,
                    "label_net_return": exit_fill * (1 - commission_rate)
                        / (entry_fill * (1 + commission_rate)) - 1,
                    "label_stop_hit": bool(stop_hit), "label_mae": adverse,
                    "exit_reason": "initial_atr_stop" if stop_hit else "scheduled_exit" if forced else "horizon_close"}
    raise AssertionError("Validated positive horizon must terminate or return pending")


def build_dataset(frames: Mapping[str, pd.DataFrame], *, horizon_bars=20,
                  min_history=61, min_quote_volume=0.0, commission_rate=0.001,
                  slippage_bps=5.0, stop_atr_multiple=2.0, membership=None) -> pd.DataFrame:
    """Keep every observed candidate row; eligibility is a separate decision.

    A horizon of H trades from the next candle open to that H-th candle close,
    unless the fixed entry ATR stop or a known scheduled exit closes it earlier.
    Missing future bars produce missing labels, never a fabricated flat result.
    """
    _positive_int(horizon_bars, "horizon_bars")
    _positive_int(min_history, "min_history", MIN_FEATURE_HISTORY)
    parameters = {"min_quote_volume": min_quote_volume, "commission_rate": commission_rate,
                  "slippage_bps": slippage_bps, "stop_atr_multiple": stop_atr_multiple}
    if any(isinstance(value, bool) or not isinstance(value, (int, float, np.number))
           or not math.isfinite(value) or value < 0 for value in parameters.values()):
        raise ValueError("Cost, liquidity and stop parameters must be finite and nonnegative")
    if commission_rate >= 1 or slippage_bps >= 10000 or stop_atr_multiple <= 0:
        raise ValueError("Commission/slippage must be below 100%; stop multiple must be positive")
    if not isinstance(frames, Mapping):
        raise ValueError("frames must be a symbol -> DataFrame mapping")
    prepared = {str(symbol): _normalise(frame) for symbol, frame in frames.items()}
    benchmark_symbol = next((s for s in sorted(prepared)
                             if s.upper().replace("/", "").replace("-", "") in {"BTCUSDT", "BTCUSD", "BTC"}), None)
    benchmark = prepared.get(benchmark_symbol) if benchmark_symbol else None
    results = []
    for symbol, frame in prepared.items():
        table = _attach_benchmark(_feature_table(frame), benchmark)
        dates = frame.index + DAY
        active_member, membership_basis = _membership_at(membership, symbol, dates)
        history_available = _history_available(frame, min_history)
        finite = np.isfinite(table[list(FEATURE_COLUMNS)].to_numpy()).all(axis=1)
        reason = np.full(len(frame), "", dtype=object)
        # First matching exclusion is deterministic and easy to audit.
        conditions = (
            (~frame["_valid"].to_numpy(), "invalid_ohlcv"),
            (frame["entry_blocked"].to_numpy() | frame["scheduled_exit"].to_numpy(), "lifecycle_entry_blocked"),
            (~active_member, "membership_inactive_or_unknown"),
            (np.arange(len(frame)) + 1 < min_history, "insufficient_history"),
            (table["contiguous_history"].to_numpy() < min_history, "missing_daily_history"),
            (~history_available, "history_unavailable"),
            (~finite, "feature_unavailable"),
            ((table["_quote_volume"].to_numpy() < min_quote_volume)
             | (table["_quote_volume"].to_numpy() <= 0) | (frame["volume"].to_numpy() <= 0), "insufficient_liquidity"),
        )
        for mask, text in conditions:
            reason[(reason == "") & mask] = text
        net, mae, maturity, exit_reason = _shadow_labels(frame, table, horizon_bars,
                                                        commission_rate, slippage_bps / 10000,
                                                        stop_atr_multiple)
        output = table[list(FEATURE_COLUMNS)].copy()
        output["as_of"], output["bar_time"], output["symbol"] = dates, frame.index, symbol
        output["eligible"], output["exclusion_reason"] = reason == "", reason
        output["membership_basis"] = membership_basis
        output["contiguous_history"] = table["contiguous_history"]
        output["history_available"] = history_available
        # Excluded/unavailable historical values cannot be exposed as serving inputs.
        output.loc[~history_available, list(FEATURE_COLUMNS)] = np.nan
        output["label_net_return"], output["label_mae"] = net, mae
        output["label_available_at"] = pd.to_datetime(maturity, utc=True)
        output["label_exit_reason"] = exit_reason
        output["label_basis"] = "independent_shadow_proxy_not_portfolio"
        results.append(output.reset_index(drop=True))
    if not results:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)
    return (pd.concat(results, ignore_index=True).sort_values(["as_of", "symbol"])
            .reset_index(drop=True).loc[:, list(_OUTPUT_COLUMNS)])


def chronological_split(dataset: pd.DataFrame, *, train_end, validation_end,
                        test_end=None) -> dict[str, pd.DataFrame]:
    """Split date groups and purge labels that have not matured before a boundary.

    Ranges are [start, end). Training and validation labels must be strictly
    earlier than their respective end; test labels must mature before test_end
    when supplied. If any eligible member's outcome is unavailable, nonfinite
    or immature, its entire decision-date cohort is withheld. This prevents
    early stopped losers from remaining while later winners are censored.
    """
    required = {"as_of", "eligible", "label_net_return", "label_available_at", *FEATURE_COLUMNS}
    if not required.issubset(dataset):
        raise ValueError(f"Dataset missing split columns: {sorted(required - set(dataset))}")
    train_end, validation_end = _utc(train_end), _utc(validation_end)
    test_end = _utc(test_end) if test_end is not None else None
    if not train_end < validation_end or (test_end is not None and not validation_end < test_end):
        raise ValueError("Split boundaries must be strictly increasing")
    as_of = pd.to_datetime(dataset["as_of"], utc=True, errors="coerce")
    maturity = pd.to_datetime(dataset["label_available_at"], utc=True, errors="coerce")
    eligible = dataset["eligible"].eq(True) & as_of.notna()
    label_valid = (maturity.notna() & (maturity > as_of)
                   & np.isfinite(dataset["label_net_return"])
                   & np.isfinite(dataset[list(FEATURE_COLUMNS)].to_numpy()).all(axis=1))
    ranges = {"train": (None, train_end), "validation": (train_end, validation_end),
              "test": (validation_end, test_end)}
    result = {}
    for name, (start, end) in ranges.items():
        selected = eligible.copy()
        if start is not None:
            selected &= as_of >= start
        if end is not None:
            selected &= as_of < end
        mature = label_valid & (maturity < end) if end is not None else label_valid
        bad_dates = as_of.loc[selected & ~mature].unique()
        selected &= ~as_of.isin(bad_dates)
        result[name] = dataset.loc[selected].copy().sort_values(["as_of", "symbol"]).reset_index(drop=True)
    return result
