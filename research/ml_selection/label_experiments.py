"""Frozen proxy/real-exit contracts and bounded original-engine label probes.

Each probe uses a fresh flat cash account and one reproduced original signal.
Actual filled return, independent intervention value and portfolio marginal
value are different estimands. Unknown execution/exit outcomes stay NaN.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from types import MethodType

import numpy as np
import pandas as pd

from config.config import config
from research.ml_selection.environment import FullEngineEnvironment


@dataclass(frozen=True)
class ExitLabelContract:
    initial_capital: float = 10000.0
    nominal_notional: float = 1000.0
    risk_budget_fraction: float = 0.01
    horizon_bars: int = 60
    entry_intervention: str = "add_to_flat_research_account"
    exit_rule: str = "original_strategy_and_engine"
    cost_basis: str = "original_engine_actual_net_fills"
    require_original_candidate: bool = True

    def __post_init__(self):
        for name in ("initial_capital", "nominal_notional", "risk_budget_fraction"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.risk_budget_fraction > 1 or self.nominal_notional > self.initial_capital:
            raise ValueError("probe notional/risk budget must fit the fixed research account")
        if isinstance(self.horizon_bars, bool) or not isinstance(self.horizon_bars, int) or self.horizon_bars < 1:
            raise ValueError("horizon_bars must be a positive integer")
        if self.entry_intervention != "add_to_flat_research_account" or self.exit_rule != "original_strategy_and_engine" or self.cost_basis != "original_engine_actual_net_fills" or self.require_original_candidate is not True:
            raise ValueError("unsupported real-exit intervention contract")

    def to_dict(self):
        return {**asdict(self), "account_state": "fresh_cash_no_positions_or_orders",
                "funding_competition": "single_candidate_no_portfolio_competitors",
                "sizing": "min_original_strategy_size_nominal_cap_and_stop_risk_cap_then_original_risk",
                "label_basis": "independent_original_engine_exit_intervention_not_portfolio_marginal",
                "unknown_target": "NaN_for_unfilled_unclosed_risk_terminated_or_unreproduced_candidate"}


def frozen_label_contracts(*, proxy_options=None, exit_contract=None):
    proxy = dict(proxy_options or {})
    contract = exit_contract or ExitLabelContract()
    if isinstance(contract, dict):
        contract = ExitLabelContract(**contract)
    payload = {"schema": "ml-selection-label-contracts/v1",
        "proxy": {"label_basis": "independent_fixed_window_atr_proxy_not_actual_or_portfolio",
                  "horizon_bars": proxy.get("horizon_bars", 20),
                  "stop_atr_multiple": proxy.get("stop_atr_multiple", 2),
                  "commission_rate": proxy.get("commission_rate", .001),
                  "slippage_bps": proxy.get("slippage_bps", 5),
                  "entry": "next_executable_daily_open_after_decision",
                  "atr": "frozen_at_decision", "sizing": "independent_fully_filled_small_long"},
        "original_exit": contract.to_dict(),
        "actual_execution": {"basis": "original_engine_observed_fills", "unfilled_is_zero_return": False},
        "portfolio_marginal": {"basis": "paired_same_account_same_budget_same_execution_intervention",
                               "status": "requires_separate_paired_portfolio_experiment"}}
    payload["contract_id"] = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return payload


def _utc(value):
    stamp = pd.to_datetime(value, utc=True, errors="raise")
    if pd.isna(stamp):
        raise ValueError("probe timestamps must be finite")
    return stamp


def _same_signal(actual, frozen):
    if not actual:
        return False
    # The frozen signal must be complete. Research cannot inject a candidate
    # that the registered strategy did not produce from this account/history.
    return json.dumps(dict(actual), sort_keys=True, default=str) == json.dumps(dict(frozen), sort_keys=True, default=str)


def _intervention_strategy(original, *, symbol, as_of, signal, contract, audit):
    strategy = deepcopy(original)
    enter = type(strategy).should_enter
    size = type(strategy).initial_entry_quantity

    def limited_entry(self, current_symbol, i, df, state, portfolio):
        if current_symbol != symbol or _utc(df.index[i]) + pd.Timedelta(days=1) != as_of:
            return None
        audit["original_signal_evaluated"] = True
        audit["signal_bar_time"] = _utc(df.index[i]).isoformat()
        generated = enter(self, current_symbol, i, df, state, portfolio)
        audit["original_signal_reproduced"] = _same_signal(generated, signal)
        audit["original_signal"] = generated
        return generated if audit["original_signal_reproduced"] else None

    def capped_size(self, **kwargs):
        original_quantity = float(size(self, **kwargs))
        current = float(kwargs["current_price"])
        stop = float(kwargs["stop_loss"])
        nominal = float(contract.nominal_notional) / current
        risk_cap = float(kwargs["equity"]) * contract.risk_budget_fraction / abs(current - stop) if 0 < stop < current else 0.0
        return min(original_quantity, nominal, risk_cap)

    strategy.should_enter = MethodType(limited_entry, strategy)
    strategy.initial_entry_quantity = MethodType(capped_size, strategy)
    return strategy


def exit_label_from_episode(episode, *, candidate, contract, intervention_id, signal_audit=None) -> dict:
    """Use full actual filled cash flows only after a non-risk flat exit."""
    symbol = candidate["symbol"]
    actual = [row for row in episode.result.get("trades", [])
              if row.get("symbol") == symbol and row.get("exit_reason") != "EndOfBacktest"]
    buys = [row for row in actual if row.get("side") == "buy"]
    sells = [row for row in actual if row.get("side") == "sell"]
    bought = sum(float(row["qty"]) for row in buys)
    sold = sum(float(row["qty"]) for row in sells)
    notional = sum(float(row["qty"]) * float(row["fill_price"]) for row in buys)
    summary = episode.summary
    row = {**deepcopy(dict(candidate)), "intervention_id": intervention_id, "symbol": symbol, "as_of": _utc(candidate["as_of"]),
           "strategy": candidate["strategy"], "label_basis": contract.to_dict()["label_basis"],
           "entry_filled_qty": bought, "exit_filled_qty": sold, "actual_entry_notional": notional,
           "actual_fill_count": len(actual), "actual_commission": sum(float(trade.get("commission", 0.) or 0.) for trade in actual),
           "label_net_return": float("nan"), "actual_execution_net_return": float("nan"),
           "independent_intervention_net_return": float("nan"), "portfolio_marginal_net_return": float("nan"),
           "portfolio_marginal_status": "unmeasured_requires_paired_portfolio_intervention",
           "label_available_at": pd.NaT, "label_exit_reason": None,
           "frozen_signal": deepcopy(candidate.get("signal")),
           "signal_audit": signal_audit or {}, "episode_summary": summary}
    if summary.get("terminated_by_risk"):
        row["outcome_status"] = "risk_terminated_unknown_target"
    elif not summary.get("accounting_ok"):
        row["outcome_status"] = "accounting_unverified_unknown_target"
    elif not bought and signal_audit is not None and (
            signal_audit.get("original_signal_evaluated") or "original_signal" in signal_audit
        ) and not signal_audit.get("original_signal_reproduced"):
        row["outcome_status"] = "signal_not_reproduced_unknown_target"
    elif not bought:
        row["outcome_status"] = "unfilled_or_ineligible_unknown_target"
    elif not sells or abs(bought - sold) > max(1e-9, bought * 1e-9) or summary.get("pending_orders") or summary.get("unresolved_positions"):
        row["outcome_status"] = "unclosed_or_pending_unknown_target"
    else:
        proceeds = sum(float(trade["qty"]) * float(trade["fill_price"]) for trade in sells)
        net = (proceeds - notional - row["actual_commission"]) / notional
        row.update({"outcome_status": "closed_actual_filled_exit", "label_net_return": net,
                    "actual_execution_net_return": net,
                    "independent_intervention_net_return": net,
                    "label_available_at": max(_utc(trade["fill_time"]).normalize() + pd.Timedelta(days=1) for trade in sells),
                    "label_exit_reason": str(sells[-1].get("exit_reason", "original_engine_exit"))})
    return row


def run_exit_label_probe(frames, candidates, *, strategies, contract=None,
                         parameters=None, engine_options=None, max_candidates=8):
    """Replay a bounded, reproducible real-exit intervention per candidate.

    Candidates require as_of/symbol/strategy/signal. Eligibility and the exact
    original signal must be reproduced by the engine; no signal is forced.
    The chosen strategy retains its original exits, stops, health and risk.
    """
    contract = contract or ExitLabelContract()
    if isinstance(contract, dict):
        contract = ExitLabelContract(**contract)
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
        raise ValueError("max_candidates must be a positive integer")
    rows = candidates.to_dict("records") if isinstance(candidates, pd.DataFrame) else list(candidates)
    frozen = frozen_label_contracts(exit_contract=contract)
    outcomes = []
    for candidate in rows[:max_candidates]:
        if not {"symbol", "as_of", "strategy", "signal"} <= set(candidate):
            raise ValueError("probe candidates require symbol/as_of/strategy/signal")
        symbol, strategy_name = candidate["symbol"], candidate["strategy"]
        as_of = _utc(candidate["as_of"])
        if symbol not in frames or strategy_name not in strategies:
            raise ValueError("probe candidate must have registered market data and original strategy")
        signal = dict(candidate["signal"])
        if signal.get("action") != "buy" or not 0 < float(signal.get("stop_loss", 0)):
            raise ValueError("real-exit probe requires an original long signal with a protective stop")
        if "available_at" in candidate and _utc(candidate["available_at"]) > as_of:
            raise ValueError("frozen candidate signal is unavailable at its decision")
        identity = hashlib.sha256(json.dumps({"contract_id": frozen["contract_id"], "symbol": symbol,
                                              "as_of": as_of.isoformat(), "strategy": strategy_name,
                                              "signal": signal}, sort_keys=True, default=str).encode()).hexdigest()
        source = frames[symbol].copy()
        source.index = pd.to_datetime(source.index, utc=True, errors="raise")
        # Warmup keeps strategy history but the account stays flat until this
        # one original proposal. Observing future bars never alters its signal.
        # Registration, health recovery and market conditions require the
        # complete registered universe. Isolating the *entry* does not shrink
        # that universe or lower its distinct-symbol recovery requirements.
        market_frames = {}
        observation_end = as_of + pd.Timedelta(days=contract.horizon_bars)
        for registered_symbol, registered_frame in frames.items():
            history = registered_frame.copy()
            history.index = pd.to_datetime(history.index, utc=True, errors="raise")
            before = history.loc[history.index < as_of]
            after = history.loc[(history.index >= as_of) & (history.index < observation_end)]
            market = pd.concat([before, after])
            market.index = market.index.tz_convert(None)
            market_frames[registered_symbol] = market
        audit = {"original_signal_reproduced": False, "original_signal_evaluated": False,
                 "input_history_start_by_symbol": {
                     registered_symbol: _utc(registered_frame.index.min()).isoformat() if len(registered_frame) else None
                     for registered_symbol, registered_frame in frames.items()},
                 "trading_start": (as_of - pd.Timedelta(days=1)).isoformat()}
        original = _intervention_strategy(strategies[strategy_name], symbol=symbol, as_of=as_of,
                                          signal=signal, contract=contract, audit=audit)
        options = deepcopy(dict(engine_options or {}))
        options.update({"initial_capital": float(contract.initial_capital), "timeframe": "1d",
                        "account_mode": "spot", "terminal_policy": "valuation_only",
                        "trading_start": (as_of - pd.Timedelta(days=1)).tz_convert(None)})
        settings = deepcopy(parameters if parameters is not None else config._config)
        # This intervention freezes the nominated strategy for all subsequent
        # position management. Protection settings are never disabled.
        from core.state import MarketState
        settings["routing"] = {state.name: strategy_name for state in MarketState}
        environment = FullEngineEnvironment(market_frames, strategies={strategy_name: original},
                                              parameters=settings, engine_options=options,
                                              drawdown_penalty=0., turnover_penalty=0.)
        episode = environment.run_episode()
        outcome = exit_label_from_episode(episode, candidate=candidate, contract=contract,
                                          intervention_id=identity, signal_audit=audit)
        registered_strategy = episode.engine.event_processor.router.strategies[strategy_name]
        outcome.update({"registered_symbols": sorted(market_frames),
                        "registered_symbol_count": len(market_frames),
                        "health_registration": deepcopy(getattr(getattr(registered_strategy, "health", None), "registration", None)),
                        "observation_end": observation_end})
        if not outcome["entry_filled_qty"]:
            outcome["entry_rejection_reasons"] = {
                "original_signal": "reproduced" if audit["original_signal_reproduced"] else (
                    "not_reproduced" if audit["original_signal_evaluated"] else "not_reached_original_entry_eligibility"),
                "allocation": [row for row in episode.result.get("allocation_audit", []) if row.get("symbol") == symbol],
                "entry_observations": [row for row in episode.result.get("entry_observations", []) if row.get("symbol") == symbol
                                       and _utc(row.get("as_of", _utc(row["timestamp"]) + pd.Timedelta(days=1))) == as_of],
                "execution": [row for row in episode.result.get("execution_audit", []) if row.get("symbol") == symbol]}
        if np.isfinite(outcome["label_net_return"]):
            availability = [outcome["label_available_at"]]
            exits = [trade for trade in episode.result.get("trades", [])
                     if trade.get("symbol") == symbol and trade.get("side") == "sell"
                     and trade.get("exit_reason") != "EndOfBacktest"]
            information_unknown = False
            for trade in exits:
                bar = source.loc[source.index == _utc(trade["fill_time"]).normalize()]
                for column in ("available_at", "close_time"):
                    if column in source:
                        if bar.empty:
                            information_unknown = True
                        else:
                            stamp = pd.to_datetime(bar.iloc[-1][column], utc=True, errors="coerce")
                            if pd.isna(stamp):
                                information_unknown = True
                            else:
                                availability.append(stamp)
            if information_unknown:
                outcome.update({"outcome_status": "exit_information_availability_unknown_target",
                                "label_available_at": pd.NaT, "label_net_return": float("nan"),
                                "actual_execution_net_return": float("nan"),
                                "independent_intervention_net_return": float("nan")})
            else:
                outcome["label_available_at"] = max(availability)
        outcomes.append(outcome)
    return pd.DataFrame(outcomes), {"schema": "ml-selection-real-exit-probe/v1",
        "contract": frozen, "candidate_count": len(rows), "probed_candidates": len(outcomes),
        "unprobed_due_to_budget": max(0, len(rows) - max_candidates),
        "known_targets": sum(np.isfinite(row["label_net_return"]) for row in outcomes),
        "unknown_targets": sum(not np.isfinite(row["label_net_return"]) for row in outcomes),
        "statuses": pd.Series([row["outcome_status"] for row in outcomes], dtype=object).value_counts().to_dict(),
        "registered_symbols": sorted(frames), "registered_symbol_count": len(frames),
        "portfolio_marginal_status": "not_measured", "production_protections": "preserved"}
