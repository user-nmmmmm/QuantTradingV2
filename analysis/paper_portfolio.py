"""Small, retrospective paper-method experiments with real Broker cash/fills.

These are cash-funded, long-only spot research controls, not production routing
or full paper replications. All targets are fixed after a closed daily bar and
sent to the existing Broker for the following bar. No weight-times-return
synthetic fills are used. The OHLCV liquidity model remains a bar-level proxy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import math
from typing import Mapping

import numpy as np
import pandas as pd

from core.broker import Broker
from core.planning.cost_aware_allocation import cost_aware_target
from core.trading.portfolio import Portfolio


@dataclass(frozen=True)
class PaperSpec:
    name: str = "trend_60"
    signal: str = "trend"  # cash, equal_weight, inverse_vol, trend
    lookback: int = 60
    vol_window: int = 30
    warmup_bars: int = 120
    scaling: str = "fixed"  # fixed, inverse_vol, inverse_variance
    rebalance: str = "immediate"  # immediate, partial, no_trade
    rebalance_every: int = 7
    partial_fraction: float = 0.5
    no_trade_band: float = 0.03
    max_gross: float = 0.9
    target_vol: float = 0.20
    volatility_floor: float = 0.10
    max_scale: float = 1.0
    volume_confirmation: bool = False
    regime_filter: bool = False
    stop_loss: float | None = None
    cost_aware: bool = False
    max_turnover_weight: float | None = None
    cost_penalty_scale: float = 1.0
    risk_aversion: float = 10.0
    holding_cost_annual: float = 0.0
    commission_rate: float = 0.001
    slippage_rate: float = 0.0005
    spread_bps: float = 2.0
    impact_coefficient: float = 0.10
    participation_rate: float = 0.01

    def __post_init__(self):
        if self.signal not in {"cash", "equal_weight", "inverse_vol", "trend"}:
            raise ValueError("unknown signal")
        if self.scaling not in {"fixed", "inverse_vol", "inverse_variance"}:
            raise ValueError("unknown scaling")
        if self.rebalance not in {"immediate", "partial", "no_trade"}:
            raise ValueError("unknown rebalance method")
        if min(self.lookback, self.vol_window, self.warmup_bars) < 2 or self.rebalance_every < 1:
            raise ValueError("lookback/vol_window/warmup >= 2 and rebalance_every >= 1 required")
        if not 0 < self.max_gross <= 1 or not 0 < self.max_scale <= 1:
            raise ValueError("cash-only gross and scaling caps must be in (0, 1]")
        if not 0 < self.partial_fraction <= 1 or not 0 < self.participation_rate <= 1:
            raise ValueError("invalid fraction or participation")
        if self.stop_loss is not None and not 0 < self.stop_loss < 1:
            raise ValueError("invalid stop_loss")
        if self.max_turnover_weight is not None and (
            not math.isfinite(self.max_turnover_weight) or self.max_turnover_weight < 0
        ):
            raise ValueError("max_turnover_weight must be finite and nonnegative")
        numeric = (self.target_vol, self.volatility_floor, self.risk_aversion)
        if any(not math.isfinite(x) or x <= 0 for x in numeric):
            raise ValueError("risk parameters must be finite and positive")
        costs = (
            self.holding_cost_annual,
            self.commission_rate,
            self.slippage_rate,
            self.spread_bps,
            self.impact_coefficient,
            self.no_trade_band,
            self.cost_penalty_scale,
        )
        if any(not math.isfinite(x) or x < 0 for x in costs):
            raise ValueError("costs and band must be finite and nonnegative")


def default_paper_specs() -> list[PaperSpec]:
    """A fixed small family; registering it does not make it an untouched test."""
    base = PaperSpec()
    return [
        replace(base, name="cash", signal="cash"),
        replace(base, name="equal_weight", signal="equal_weight"),
        replace(base, name="inverse_vol", signal="inverse_vol"),
        replace(base, name="trend_20", lookback=20),
        base,
        replace(base, name="trend_120", lookback=120),
        replace(base, name="trend_60_inverse_vol", scaling="inverse_vol"),
        replace(base, name="trend_60_inverse_variance", scaling="inverse_variance"),
        replace(base, name="trend_60_volume", volume_confirmation=True),
        replace(base, name="trend_60_regime", regime_filter=True),
        replace(base, name="trend_60_stop_10pct", stop_loss=0.10),
        replace(base, name="trend_60_partial", rebalance="partial"),
        replace(base, name="trend_60_no_trade", rebalance="no_trade"),
        replace(base, name="trend_60_cost_aware", cost_aware=True, max_turnover_weight=0.25),
    ]


def _utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _prepare_frames(frames):
    if not frames:
        raise ValueError("at least one asset is required")
    cleaned = {}
    required = ["open", "high", "low", "close", "volume"]
    for symbol, original in sorted(frames.items()):
        frame = original.copy()
        frame.index = pd.to_datetime(frame.index, utc=True)
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError(f"{symbol}: duplicate or unordered timestamps")
        values = frame[required].astype(float)
        if not np.isfinite(values.to_numpy()).all():
            raise ValueError(f"{symbol}: nonfinite OHLCV")
        if (values[["open", "high", "low", "close"]] <= 0).any().any() or (values.volume < 0).any():
            raise ValueError(f"{symbol}: nonpositive price or negative volume")
        if (
            (values.high < values[["open", "close", "low"]].max(axis=1))
            | (values.low > values[["open", "close", "high"]].min(axis=1))
        ).any():
            raise ValueError(f"{symbol}: inconsistent OHLC")
        cleaned[symbol] = frame
    common = next(iter(cleaned.values())).index
    for frame in cleaned.values():
        if not frame.index.equals(common):
            raise ValueError(
                "asset time axes differ; align and audit missing bars explicitly before replay"
            )
    if len(common) < 3:
        raise ValueError("insufficient common observations")
    # Never silently intersect or forward fill tradable bars. Gaps are visible and holding fees use
    # elapsed calendar time; signals explicitly use observation lookbacks.
    return {symbol: frame.loc[common] for symbol, frame in cleaned.items()}


def _target(spec, close, volume, signal_index, existing, cost_multiplier):
    history = close.iloc[: signal_index + 1]
    n = len(close.columns)
    returns = history.tail(spec.vol_window + 1).pct_change(fill_method=None).tail(spec.vol_window)
    annual_vol = returns.std(ddof=1).to_numpy() * np.sqrt(365.0)
    sufficient = len(history) > max(spec.lookback, spec.vol_window, spec.warmup_bars)
    momentum = (
        (history.iloc[-1] / history.iloc[-1 - spec.lookback] - 1).to_numpy()
        if sufficient
        else np.zeros(n)
    )
    regime = "warmup"
    if sufficient:
        market_vol = float(np.nanmean(annual_vol))
        regime = ("up" if float(np.mean(momentum)) > 0 else "down") + (
            "_high_vol" if market_vol > 0.60 else "_low_vol"
        )
    if spec.signal == "cash" or not sufficient:
        return np.zeros(n), regime, "cash" if spec.signal == "cash" else "warmup"
    sigma = np.maximum(annual_vol, spec.volatility_floor)
    if spec.signal == "inverse_vol":
        weights = 1 / sigma
        weights /= weights.sum()
    else:
        weights = np.ones(n) / n
    if spec.signal == "trend":
        weights *= momentum > 0
    if spec.volume_confirmation:
        recent = volume.iloc[max(0, signal_index - 6) : signal_index + 1].mean().to_numpy()
        baseline = volume.iloc[max(0, signal_index - 29) : signal_index + 1].mean().to_numpy()
        weights *= recent > baseline
    if spec.regime_filter and regime.startswith("down"):
        weights[:] = 0.0
    weights *= spec.max_gross
    if spec.scaling != "fixed":
        power = 1 if spec.scaling == "inverse_vol" else 2
        weights *= np.minimum(spec.max_scale, (spec.target_vol / sigma) ** power)
    status = "rule_target"
    if spec.cost_aware:
        mu = np.clip(np.log1p(momentum) / spec.lookback, -0.05, 0.05)
        penalty_scale = cost_multiplier * spec.cost_penalty_scale
        cost = penalty_scale * (spec.commission_rate + spec.slippage_rate + spec.spread_bps / 20000)
        weights, status = cost_aware_target(
            mu,
            returns.cov().to_numpy(),
            existing,
            gross_cap=min(spec.max_gross, float(weights.sum())),
            trading_cost=cost,
            holding_cost=spec.holding_cost_annual * penalty_scale / 365,
            risk_aversion=spec.risk_aversion,
            impact_penalty=spec.impact_coefficient * 0.01 * penalty_scale,
        )
        if "cash_fallback" in status:
            return np.zeros(n), regime, status
    return _rebalance_target(spec, weights, existing), regime, status


def _rebalance_target(spec, weights, existing):
    weights = np.array(weights, dtype=float, copy=True)
    if spec.rebalance == "partial":
        weights = existing + spec.partial_fraction * (weights - existing)
    elif spec.rebalance == "no_trade":
        weights = np.where(np.abs(weights - existing) < spec.no_trade_band, existing, weights)
    # Risk and cash caps take precedence over turnover smoothing.
    weights = np.maximum(weights, 0)
    if weights.sum() > spec.max_gross:
        weights *= spec.max_gross / weights.sum()
    return weights


def _limit_target_turnover(spec, targets, existing, stop_mask, *, mandatory_weights=None):
    """Cap decision L1 weight changes after mandatory stop/gross reductions.

    Mandatory reductions consume the budget first. If they exhaust it, only
    those reductions are requested. They can exceed the cap and are explicitly
    flagged; otherwise discretionary moves share the remaining weight budget.
    This constrains requested targets, never guarantees fills or realised risk.
    """
    existing = np.asarray(existing, dtype=float)
    desired = np.maximum(np.asarray(targets, dtype=float), 0.0).copy()
    stop_mask = np.asarray(stop_mask, dtype=bool)
    mandatory = existing.copy()
    provider_reduction = False
    if mandatory_weights is not None:
        envelope = np.asarray(mandatory_weights, dtype=float)
        if (
            envelope.shape != existing.shape
            or not np.isfinite(envelope).all()
            or (envelope < 0).any()
            or (envelope > existing + 1e-12).any()
        ):
            raise ValueError("mandatory callback envelope must only reduce existing exposure")
        provider_reduction = bool((envelope < existing - 1e-12).any())
        mandatory = np.minimum(mandatory, envelope)
        desired = np.minimum(desired, np.where(envelope < existing - 1e-12, envelope, np.inf))
    mandatory[stop_mask] = 0.0
    desired[stop_mask] = 0.0
    reasons = ["stop_loss"] if np.any(stop_mask & (existing > 0)) else []
    if provider_reduction:
        reasons.append("callback_native_risk_envelope")
    if mandatory.sum() > spec.max_gross:
        mandatory *= spec.max_gross / mandatory.sum()
        reasons.append("gross_cap")
    if desired.sum() > spec.max_gross:
        desired *= spec.max_gross / desired.sum()
    cap = spec.max_turnover_weight
    mandatory_change = float(np.abs(mandatory - existing).sum())
    binding = False
    if cap is not None:
        remaining = max(0.0, cap - mandatory_change)
        discretionary = float(np.abs(desired - mandatory).sum())
        fraction = min(1.0, remaining / discretionary) if discretionary else 1.0
        binding = fraction < 1.0
        desired = mandatory + fraction * (desired - mandatory)
    actual_change = float(np.abs(desired - existing).sum())
    override = cap is not None and actual_change > cap + 1e-12
    return desired, {
        "target_turnover_weight": actual_change,
        "mandatory_reduction_weight": mandatory_change,
        "turnover_constraint_binding": binding,
        "turnover_cap_override": override,
        "turnover_override_reasons": reasons if override else [],
    }


def _prepare_target_grids(spec, index, symbols, target_overrides, candidate_targets):
    """Validate target values and candidate ownership on the full common axis."""
    if candidate_targets is not None:
        candidate_targets = candidate_targets.copy()
        candidate_targets.index = pd.to_datetime(candidate_targets.index, utc=True)
        if (
            target_overrides is None
            or not candidate_targets.index.equals(index)
            or set(candidate_targets.columns) != set(symbols)
        ):
            raise ValueError("candidate ownership requires matching external target grid")
        candidate_targets = candidate_targets[symbols]
    target_hash = None
    if target_overrides is not None:
        target_overrides = target_overrides.copy()
        target_overrides.index = pd.to_datetime(target_overrides.index, utc=True)
        if not target_overrides.index.equals(index) or set(target_overrides.columns) != set(
            symbols
        ):
            raise ValueError("external targets must match the full common bar index and symbol set")
        target_overrides = target_overrides[symbols].astype(float)
        values = target_overrides.to_numpy()
        if (
            not np.isfinite(values).all()
            or (values < 0).any()
            or (values.sum(axis=1) > spec.max_gross + 1e-12).any()
        ):
            raise ValueError("external targets must be finite, long-only and within gross budget")
        target_hash = hashlib.sha256(
            target_overrides.to_csv(float_format="%.17g").encode()
        ).hexdigest()
    return target_overrides, candidate_targets, target_hash


def _paper_broker(portfolio, spec, cost_multiplier):
    """Use the same native spot account and explicit scenario cost assumptions."""
    return Broker(
        portfolio,
        commission_rate=spec.commission_rate * cost_multiplier,
        commission_rate_maker=spec.commission_rate * cost_multiplier,
        slippage=spec.slippage_rate * cost_multiplier,
        spread_bps=spec.spread_bps * cost_multiplier,
        use_impact_cost=True,
        impact_coefficient=spec.impact_coefficient * cost_multiplier,
        max_participation_rate=spec.participation_rate,
        timeframe="1d",
        account_id="paper_research",
        exchange_id="ohlcv_research",
    )


def _provider_target(target_provider, spec, close, signal_i, signal_stamp, symbols, existing):
    """Expose only a copied past prefix and validate native mandatory reductions."""
    provider_mandatory = None
    # The callback receives a copied past prefix and actual prior-mark
    # holdings. No execution-bar price/volume is exposed to its forecast.
    prefix = close.iloc[: signal_i + 1].copy()
    context = {
        "as_of": (signal_stamp + pd.Timedelta(days=1)).isoformat(),
        "source_bar": signal_stamp.isoformat(),
        "symbols": list(symbols),
        "returns": prefix.pct_change(fill_method=None).iloc[1:],
        "existing_weights": existing.copy(),
        "max_gross": spec.max_gross,
        "information_mode": "retrospective_nominal_daily_close",
    }
    proposed, provider_audit = target_provider(context)
    targets = np.asarray(proposed, dtype=float)
    if (
        targets.shape != existing.shape
        or not np.isfinite(targets).all()
        or (targets < 0).any()
        or targets.sum() > spec.max_gross + 1e-10
        or not isinstance(provider_audit, dict)
    ):
        raise ValueError("target provider violated the native long-only cash envelope")
    declared_mandatory = provider_audit.get("mandatory_reduction_weights", {})
    if not isinstance(declared_mandatory, Mapping) or not set(declared_mandatory) <= set(symbols):
        raise ValueError("callback mandatory reductions need known symbol targets")
    if declared_mandatory:
        provider_mandatory = existing.copy()
        for j, symbol in enumerate(symbols):
            if symbol in declared_mandatory:
                cap = float(declared_mandatory[symbol])
                if (
                    not math.isfinite(cap)
                    or cap < 0
                    or cap > existing[j] + 1e-12
                    or targets[j] > cap + 1e-10
                ):
                    raise ValueError(
                        "callback mandatory envelope is not an actual monotone reduction"
                    )
                provider_mandatory[j] = min(existing[j], cap)
    status = str(provider_audit.get("status", "provider_result"))
    record = {
        "signal_time": signal_stamp,
        "as_of": context["as_of"],
        "targets": targets.tolist(),
        "audit": provider_audit,
    }
    return targets, status, provider_mandatory, record


def _submit_replay_orders(
    broker,
    spec,
    symbols,
    signal_prices,
    qty_before,
    desired,
    signal_equity,
    candidate_targets,
    signal_i,
    signal_stamp,
    stamp,
    regime,
    existing,
    status,
    stop_symbols,
    gross_breach,
    turnover_audit,
    decisions,
    provider_context,
):
    """Submit sells before buys using the same frozen prior-close intentions."""
    quantities = desired * max(signal_equity, 0) / np.array(list(signal_prices.values()))
    delta = quantities - np.array(list(qty_before.values()))
    # Sell before buy; all orders remain frozen at the same prior close.
    for side in ("sell", "buy"):
        for j, symbol in enumerate(symbols):
            if (side == "sell" and delta[j] >= -1e-12) or (side == "buy" and delta[j] <= 1e-12):
                continue
            candidate_id = None
            if candidate_targets is not None and side == "buy":
                candidate_id = candidate_targets.iloc[signal_i][symbol]
                if not isinstance(candidate_id, str) or not candidate_id:
                    raise ValueError("positive opening target lacks a causal candidate owner")
            order = broker.submit_order(
                symbol,
                side,
                abs(float(delta[j])),
                price=signal_prices[symbol],
                timestamp=signal_stamp,
                strategy_id=spec.name,
                time_in_force="IOC",
                signal_id=candidate_id,
                exit_reason="paper_close_observed_stop"
                if symbol in stop_symbols
                else "paper_rebalance",
            )
            decisions.append(
                {
                    "signal_time": signal_stamp,
                    "execution_bar": stamp,
                    "symbol": symbol,
                    "regime": regime,
                    "target_weight": float(desired[j]),
                    "existing_weight": float(existing[j]),
                    "side": side,
                    "requested_qty": abs(float(delta[j])),
                    "order_id": order.id,
                    "candidate_id": candidate_id,
                    "optimizer_status": status,
                    "stop_triggered": symbol in stop_symbols,
                    "gross_cap_triggered": gross_breach,
                    **turnover_audit,
                }
            )
            if provider_context is not None:
                decisions[-1].update(
                    signal_bar_time=signal_stamp,
                    decision_cutoff=provider_context["as_of"],
                    order_timestamp_basis="legacy_signal_bar_open_label_not_actual_decision_clock",
                )


def _replay_order_targets(
    spec, portfolio, symbols, signal_prices, qty_before, existing, targets, provider_mandatory, step
):
    """Apply scheduling and native risk reductions before discretionary turnover."""
    stop_symbols = {
        s
        for s in symbols
        if spec.stop_loss is not None
        and qty_before[s] > 0
        and signal_prices[s] <= portfolio.get_position(s)["avg_price"] * (1 - spec.stop_loss)
    }
    scheduled = step % spec.rebalance_every == 0
    gross_breach = float(existing.sum()) > spec.max_gross + 1e-12
    desired = existing.copy()
    turnover_audit = {
        "target_turnover_weight": 0.0,
        "mandatory_reduction_weight": 0.0,
        "turnover_constraint_binding": False,
        "turnover_cap_override": False,
        "turnover_override_reasons": [],
    }
    callback_risk_exit = provider_mandatory is not None and bool(
        (provider_mandatory < existing - 1e-12).any()
    )
    should_submit = bool(scheduled or stop_symbols or gross_breach or callback_risk_exit)
    if should_submit:
        requested = (
            targets
            if scheduled
            else np.minimum(existing, provider_mandatory)
            if callback_risk_exit
            else existing
        )
        desired, turnover_audit = _limit_target_turnover(
            spec,
            requested,
            existing,
            [s in stop_symbols for s in symbols],
            mandatory_weights=provider_mandatory,
        )
    return desired, turnover_audit, stop_symbols, gross_breach, should_submit


def _process_replay_bar(
    data,
    symbols,
    i,
    signal_i,
    close,
    stamp,
    signal_stamp,
    broker,
    portfolio,
    spec,
    cost_multiplier,
    holding_total,
):
    """Process actual Broker fills and charge assumed carry on actual inventory."""
    bars = {}
    for symbol in symbols:
        bar = data[symbol].iloc[i].copy()
        previous = data[symbol].iloc[signal_i]
        # Bound the realised proxy by the last known volume. Changing a
        # future volume can reduce realised fills, never enlarge intentions.
        bar["volume"] = min(float(bar.volume), float(previous.volume))
        bar["volatility"] = max(float(previous.high - previous.low) / float(previous.close), 0.0)
        if "spread_bps" in bar:
            bar["spread_bps"] = float(bar.spread_bps) * cost_multiplier
        bars[symbol] = bar
    trades = broker.process_orders(bars)
    days = max((stamp - signal_stamp).total_seconds() / 86400, 0.0)
    for symbol in symbols:
        # A documented scenario cost on actual end-of-bar inventory.
        notional = portfolio.get_position(symbol)["qty"] * float(close.iloc[i][symbol])
        amount = notional * spec.holding_cost_annual * cost_multiplier * days / 365
        if amount:
            portfolio.apply_financing(
                timestamp=stamp,
                symbol=symbol,
                kind="assumed_spot_holding",
                rate=spec.holding_cost_annual * cost_multiplier,
                notional=notional,
                amount=amount,
                source="paper_spec_assumption_not_exchange_funding",
            )
        holding_total += amount
    return trades, holding_total


def _replay_summary(
    spec,
    equity_frame,
    final_equity,
    initial_capital,
    fills,
    broker,
    cost_multiplier,
    symbols,
    target_overrides,
    target_hash,
    target_provider,
):
    """Describe account facts without promoting retrospective assumptions."""
    summary = {
        "name": spec.name,
        "net_return": final_equity / initial_capital - 1,
        "net_pnl": final_equity - initial_capital,
        "fill_count": len(fills),
        "turnover_notional": float(sum(t["qty"] * t["fill_price"] for t in broker.trades)),
        "cost_multiplier": float(cost_multiplier),
        "initial_capital": float(initial_capital),
        "max_drawdown": float(
            (
                equity_frame.equity
                / np.maximum.accumulate(np.r_[initial_capital, equity_frame.equity])[1:]
                - 1
            ).min()
        ),
        "formal_routing_enabled": False,
        "paper_replication": False,
        "evaluation_status": "retrospective_method_transfer",
        "symbols": symbols,
        "target_source": "external_targets_research"
        if target_overrides is not None
        else "native_rule",
        "target_overrides_sha256": target_hash,
        "max_turnover_weight": spec.max_turnover_weight,
        "cost_penalty_scale": spec.cost_penalty_scale,
        "turnover_constraint_semantics": "sum(abs(target_weight-existing_weight)) per decision; not realised fill notional",
        "turnover_cap_override_decisions": int(equity_frame.turnover_cap_override.sum()),
        "max_decision_target_turnover_weight": float(equity_frame.target_turnover_weight.max()),
        "turnover_override_policy": "mandatory stop exits and drift gross-cap reductions take priority; remaining budget limits discretionary changes",
        "cost_penalty_semantics": "optimizer trading/holding/impact penalties only; realised Broker costs unchanged",
        "risk_budget_semantics": "common decision gross cap; realised mark weights can drift",
        "capacity_model": "min(previous_bar_volume,realised_bar_volume) participation proxy; no queue claim",
    }
    if target_provider is not None:
        summary.update(
            target_source="causal_callback_actual_Broker_holdings",
            callback_information_mode="retrospective_nominal_daily_close",
        )
    return summary


def _replay_result(
    spec,
    records,
    decisions,
    provider_decisions,
    broker,
    portfolio,
    initial_capital,
    cost_multiplier,
    market_pnl_total,
    fees_total,
    slippage_total,
    holding_total,
    symbols,
    target_overrides,
    target_hash,
    target_provider,
):
    """Reconcile the equity bridge and assemble immutable replay results."""
    equity_frame = pd.DataFrame(records).set_index("timestamp")
    final_equity = float(equity_frame.equity.iloc[-1])
    expected_equity = (
        initial_capital + market_pnl_total - fees_total - slippage_total - holding_total
    )
    residual = final_equity - expected_equity
    fills = pd.DataFrame(broker.trades)
    accounting = {
        "ok": bool(abs(residual) <= max(1e-7, initial_capital * 1e-9)),
        "initial_capital": float(initial_capital),
        "final_equity": final_equity,
        "market_pnl": market_pnl_total,
        "commission": fees_total,
        "slippage": slippage_total,
        "holding_cost": holding_total,
        "equity_bridge_residual": residual,
        "minimum_cash": float(equity_frame.cash.min()),
        "account_mode": "spot",
        "financing_status": "assumed_spot_holding_only",
        "terminal_inventory": "mark_to_market_no_forced_fill",
    }
    summary = _replay_summary(
        spec,
        equity_frame,
        final_equity,
        initial_capital,
        fills,
        broker,
        cost_multiplier,
        symbols,
        target_overrides,
        target_hash,
        target_provider,
    )
    result = {
        "equity": equity_frame,
        "returns": equity_frame["return"].rename(spec.name),
        "fills": fills,
        "decisions": pd.DataFrame(decisions),
        "accounting": accounting,
        "spec": asdict(spec),
        "summary": summary,
        "execution_audit": pd.DataFrame(broker.execution_audit),
        "financing": pd.DataFrame([asdict(entry) for entry in portfolio.financing_ledger]),
    }
    if target_provider is not None:
        result["target_provider_audit"] = provider_decisions
    return result


def _validate_replay_controls(
    spec, initial_capital, cost_multiplier, target_provider, target_overrides, candidate_targets
):
    """Validate account capital and mutually exclusive sources of intentions."""
    spec = PaperSpec(**spec) if isinstance(spec, dict) else spec
    if (
        not np.isfinite(initial_capital)
        or initial_capital <= 0
        or not np.isfinite(cost_multiplier)
        or cost_multiplier <= 0
    ):
        raise ValueError("positive finite capital and cost multiplier required")
    if target_provider is not None and (
        not callable(target_provider)
        or target_overrides is not None
        or candidate_targets is not None
    ):
        raise ValueError(
            "causal target provider must be callable and exclusive of precomputed overrides"
        )
    return spec


def run_paper_replay(
    frames: Mapping[str, pd.DataFrame],
    spec: PaperSpec | dict,
    *,
    start=None,
    end=None,
    initial_capital=10_000.0,
    cost_multiplier=1.0,
    target_overrides: pd.DataFrame | None = None,
    candidate_targets: pd.DataFrame | None = None,
    target_provider=None,
) -> dict:
    """Return equity/returns/fills/accounting/decisions without writing files.

    Holding costs are explicit assumed spot custody/carry costs, *not* invented
    perpetual funding. Stops are close-observed loss exits at the next open;
    neither intrabar threshold execution nor immediate guaranteed exit is claimed.
    Terminal inventory is marked, not synthetically liquidated.
    Turnover is an L1 target-weight cap per decision. Stop exits and reductions
    of drift above max_gross take precedence, including off-schedule decisions.
    """
    spec = _validate_replay_controls(
        spec, initial_capital, cost_multiplier, target_provider, target_overrides, candidate_targets
    )
    data = _prepare_frames(frames)
    symbols = list(data)
    index = data[symbols[0]].index
    close = pd.DataFrame({s: data[s].close for s in symbols})
    volume = pd.DataFrame({s: data[s].volume for s in symbols})
    target_overrides, candidate_targets, target_hash = _prepare_target_grids(
        spec, index, symbols, target_overrides, candidate_targets
    )
    indices = [
        i
        for i, stamp in enumerate(index)
        if i > 0 and (start is None or stamp >= _utc(start)) and (end is None or stamp <= _utc(end))
    ]
    if not indices:
        raise ValueError("empty evaluation window")
    portfolio = Portfolio(float(initial_capital), account_mode="spot")
    broker = _paper_broker(portfolio, spec, cost_multiplier)
    records, decisions, provider_decisions = [], [], []
    prior_equity = float(initial_capital)
    market_pnl_total = fees_total = slippage_total = holding_total = 0.0
    for step, i in enumerate(indices):
        signal_i, stamp = i - 1, index[i]
        signal_stamp = index[signal_i]
        signal_prices = {s: float(close.iloc[signal_i][s]) for s in symbols}
        signal_equity = float(portfolio.get_equity(signal_prices))
        qty_before = {s: float(portfolio.get_position(s)["qty"]) for s in symbols}
        existing = (
            np.array([qty_before[s] * signal_prices[s] / signal_equity for s in symbols])
            if signal_equity > 0
            else np.zeros(len(symbols))
        )
        targets, regime, status = _target(spec, close, volume, signal_i, existing, cost_multiplier)
        provider_mandatory = None
        provider_context = None
        if target_provider is not None:
            targets, status, provider_mandatory, provider_context = _provider_target(
                target_provider, spec, close, signal_i, signal_stamp, symbols, existing
            )
            provider_decisions.append(provider_context)
        if target_overrides is not None and signal_i >= max(
            spec.lookback, spec.vol_window, spec.warmup_bars
        ):
            targets = _rebalance_target(spec, target_overrides.iloc[signal_i].to_numpy(), existing)
            status = "external_targets_research"
        desired, turnover_audit, stop_symbols, gross_breach, should_submit = _replay_order_targets(
            spec,
            portfolio,
            symbols,
            signal_prices,
            qty_before,
            existing,
            targets,
            provider_mandatory,
            step,
        )
        if should_submit:
            _submit_replay_orders(
                broker,
                spec,
                symbols,
                signal_prices,
                qty_before,
                desired,
                signal_equity,
                candidate_targets,
                signal_i,
                signal_stamp,
                stamp,
                regime,
                existing,
                status,
                stop_symbols,
                gross_breach,
                turnover_audit,
                decisions,
                provider_context,
            )
        trades, holding_total = _process_replay_bar(
            data,
            symbols,
            i,
            signal_i,
            close,
            stamp,
            signal_stamp,
            broker,
            portfolio,
            spec,
            cost_multiplier,
            holding_total,
        )
        prices = {s: float(close.iloc[i][s]) for s in symbols}
        equity = float(portfolio.get_equity(prices))
        commissions = sum(float(t["commission"]) for t in trades)
        slip_cost = sum(float(t["qty"]) * float(t["slip"]) for t in trades)
        market_pnl = sum(qty_before[s] * (prices[s] - signal_prices[s]) for s in symbols)
        for trade in trades:
            signed = trade["qty"] * (1 if trade["side"] == "buy" else -1)
            market_pnl += signed * (prices[trade["symbol"]] - trade["theoretical_price"])
        fees_total += commissions
        slippage_total += slip_cost
        market_pnl_total += market_pnl
        gross = float(portfolio.get_total_exposure(prices))
        row = {
            "timestamp": stamp,
            "equity": equity,
            "cash": float(portfolio.cash),
            "gross_exposure": gross,
            "gross_weight": gross / equity if equity > 0 else 0.0,
            "return": equity / prior_equity - 1 if prior_equity > 0 else 0.0,
            "market_pnl": market_pnl,
            "commission": commissions,
            "slippage_cost": slip_cost,
            "holding_cost": portfolio.cumulative_financing_cost,
            "regime": regime,
            "target_gross": float(desired.sum()),
            "optimizer_status": status,
            **turnover_audit,
        }
        row.update({"qty_" + s: portfolio.get_position(s)["qty"] for s in symbols})
        records.append(row)
        prior_equity = equity
    return _replay_result(
        spec,
        records,
        decisions,
        provider_decisions,
        broker,
        portfolio,
        initial_capital,
        cost_multiplier,
        market_pnl_total,
        fees_total,
        slippage_total,
        holding_total,
        symbols,
        target_overrides,
        target_hash,
        target_provider,
    )


def capacity_cost_study(
    frames,
    spec,
    *,
    start=None,
    end=None,
    capitals=(10_000.0, 100_000.0, 1_000_000.0),
    cost_multipliers=(1.0, 1.5),
):
    rows = []
    for capital in capitals:
        for multiplier in cost_multipliers:
            result = run_paper_replay(
                frames,
                spec,
                start=start,
                end=end,
                initial_capital=capital,
                cost_multiplier=multiplier,
            )
            rows.append(
                {
                    **result["summary"],
                    "accounting_ok": result["accounting"]["ok"],
                    "commission": result["accounting"]["commission"],
                    "slippage": result["accounting"]["slippage"],
                }
            )
    return pd.DataFrame(rows)
