"""Offline, causal derivative features and exact funding-settlement replay.

The bar execution engine does not know intrabar position changes. Funding
settlements therefore stay on their own event clock, never on the daily bar
clock. Position events are *post-fill* contract-count snapshots, and a fill
at the same timestamp as settlement takes effect after that settlement.
All input timestamps are UTC; naive input is interpreted as UTC explicitly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from core.timeframes import as_utc_timestamp
from core.universe import normalize_symbol


@dataclass(frozen=True)
class LinearContractSpec:
    venue: str
    contract_id: str
    symbol: str
    base_currency: str
    quote_currency: str
    settlement_currency: str
    contract_multiplier: float
    linear: bool
    market_type: str
    funding_interval_hours: float
    settlement_anchor: str = "1970-01-01T00:00:00Z"

    def __post_init__(self):
        for name in ("venue", "contract_id", "symbol", "base_currency", "quote_currency", "settlement_currency"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"contract {name} must be an explicit nonempty string")
        if self.linear is not True or self.market_type != "perpetual":
            raise ValueError("only explicit linear perpetual contracts are supported; inverse/quanto/futures are unsupported")
        if self.quote_currency != self.settlement_currency:
            raise ValueError("only quote-settled linear contracts are supported")
        if self.base_currency == self.quote_currency:
            raise ValueError("base and quote currencies must differ")
        for name in ("contract_multiplier", "funding_interval_hours"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"contract {name} must be finite and positive")
        expected_symbol = f"{self.base_currency}/{self.quote_currency}:{self.settlement_currency}"
        if normalize_symbol(self.symbol) != normalize_symbol(expected_symbol):
            raise ValueError("contract symbol must explicitly identify base/quote:settlement currencies")
        anchor = as_utc_timestamp(self.settlement_anchor)
        if pd.isna(anchor):
            raise ValueError("settlement_anchor is required")
        object.__setattr__(self, "settlement_anchor", anchor.isoformat())

    @classmethod
    def from_json(cls, path: str | Path) -> "LinearContractSpec":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8-sig")))

    @property
    def identity(self) -> str:
        return f"{self.venue}:{self.contract_id}"

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode("utf-8")).hexdigest()

    def validate_request(self, symbol: str, venue: str, market_type: str):
        if (normalize_symbol(symbol) != normalize_symbol(self.symbol)
                or venue != self.venue or market_type != self.market_type):
            raise ValueError("requested symbol/venue/market_type does not match derivative contract identity")


@dataclass(frozen=True)
class DerivativesBundle:
    spec: LinearContractSpec
    settlements: pd.DataFrame
    observations: pd.DataFrame
    provenance: Mapping[str, Any]
    quality: Mapping[str, Any]


def _timestamp_column(frame: pd.DataFrame, column: str):
    if column not in frame:
        raise ValueError(f"missing derivative column: {column}")
    frame[column] = pd.to_datetime(frame[column], utc=True, errors="raise")
    if frame[column].isna().any():
        raise ValueError(f"missing derivative timestamp: {column}")


def _identity(frame: pd.DataFrame, spec: LinearContractSpec):
    if "contract_id" not in frame or not frame["contract_id"].eq(spec.contract_id).all():
        raise ValueError("missing or mismatched derivative contract_id")
    for column, expected in (("venue", spec.venue), ("symbol", spec.symbol),
                             ("quote_currency", spec.quote_currency),
                             ("settlement_currency", spec.settlement_currency)):
        if column in frame and not frame[column].eq(expected).all():
            raise ValueError(f"mismatched derivative {column}")


def _numeric(frame: pd.DataFrame, column: str, *, positive=False, nonnegative=False):
    if column not in frame:
        raise ValueError(f"missing derivative column: {column}")
    frame[column] = pd.to_numeric(frame[column], errors="raise")
    if not frame[column].map(lambda value: math.isfinite(float(value))).all():
        raise ValueError(f"nonfinite or missing derivative value: {column}")
    if positive and not frame[column].gt(0).all():
        raise ValueError(f"{column} must be positive")
    if nonnegative and not frame[column].ge(0).all():
        raise ValueError(f"{column} must be nonnegative")


def validate_settlements(spec: LinearContractSpec, settlements: pd.DataFrame) -> pd.DataFrame:
    """Validate actual rates, their publication clocks, and the settlement grid."""
    frame = settlements.copy()
    _identity(frame, spec)
    for name in ("settlement_time", "available_at"):
        _timestamp_column(frame, name)
    _numeric(frame, "funding_rate")
    _numeric(frame, "mark_price", positive=True)
    if frame["settlement_time"].duplicated().any():
        raise ValueError("duplicate funding settlement; deduplicate at the source with evidence")
    if (frame["available_at"] < frame["settlement_time"]).any():
        raise ValueError("actual funding result cannot be available before settlement; use predicted_funding_rate")
    interval_ns = pd.Timedelta(hours=spec.funding_interval_hours).value
    anchor_ns = as_utc_timestamp(spec.settlement_anchor).value
    if ((frame["settlement_time"].astype("int64") - anchor_ns) % interval_ns).ne(0).any():
        raise ValueError("funding settlement does not match declared interval/anchor")
    return frame.sort_values("settlement_time").reset_index(drop=True)


def validate_observations(spec: LinearContractSpec, observations: pd.DataFrame) -> pd.DataFrame:
    frame = observations.copy()
    _identity(frame, spec)
    for name in ("observed_at", "available_at"):
        _timestamp_column(frame, name)
    if (frame["available_at"] < frame["observed_at"]).any():
        raise ValueError("observation cannot be available before observed_at")
    if frame["available_at"].duplicated().any():
        raise ValueError("duplicate observation availability timestamp")
    columns = [name for name in ("open_interest", "mark_price", "index_price", "predicted_funding_rate") if name in frame]
    if not columns:
        raise ValueError("observation file requires OI, mark/index or predicted_funding_rate")
    for name in columns:
        _numeric(frame, name, positive=name in {"mark_price", "index_price"}, nonnegative=name == "open_interest")
    return frame.sort_values("available_at").reset_index(drop=True)


def _file_identity(path: str | Path) -> dict:
    path = Path(path).resolve()
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def load_derivatives_bundle(contract_file: str | Path, funding_file: str | Path,
                            observations_file: str | Path | None = None) -> DerivativesBundle:
    """Read only local inputs; completeness of a requested replay is checked later."""
    spec = LinearContractSpec.from_json(contract_file)
    settlements = validate_settlements(spec, pd.read_csv(funding_file, dtype={"contract_id": str}))
    observations = (validate_observations(spec, pd.read_csv(observations_file, dtype={"contract_id": str}))
                    if observations_file else pd.DataFrame())
    interval = pd.Timedelta(hours=spec.funding_interval_hours)
    gaps = int(settlements["settlement_time"].diff().dropna().gt(interval).sum())
    provenance = {"contract": _file_identity(contract_file), "funding": _file_identity(funding_file),
                  "observations": _file_identity(observations_file) if observations_file else None,
                  "contract_spec": asdict(spec), "spec_digest": spec.digest,
                  "timestamp_policy": "UTC; naive timestamps interpreted as UTC"}
    quality = {"settlement_rows": len(settlements), "settlement_gap_count": gaps,
               "observation_rows": len(observations), "observation_status": "present" if len(observations) else "missing",
               "funding_status": "present_unverified_range" if len(settlements) else "missing",
               "coverage_status": "verify_explicit_replay_range",
               "cost_mode": "explicit_position_event_replay_required"}
    return DerivativesBundle(spec, settlements, observations, provenance, quality)


def merge_derivative_features(bars: pd.DataFrame, bundle: DerivativesBundle,
                              *, max_age: str | pd.Timedelta = "24h") -> pd.DataFrame:
    """Merge only available information; costs never enter the bar funding_rate.

    The input index is the decision's information cutoff, not an implicit bar
    open-to-close shift. Features retain publication and observation clocks.
    Stale/missing features are NaN and have a true stale flag.
    """
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        raise ValueError("derivative features require a sorted unique DatetimeIndex")
    age_limit = pd.Timedelta(max_age)
    if pd.isna(age_limit) or age_limit <= pd.Timedelta(0):
        raise ValueError("max_age must be positive")
    result = bars.copy()
    left = pd.DataFrame({"_cutoff": pd.to_datetime(bars.index, utc=True)})
    inputs = [(bundle.settlements, "funding_rate", "last_settled_funding_rate", "settlement_time")]
    inputs += [(bundle.observations, name,
                "derivative_" + name if name in {"mark_price", "index_price"} else name, "observed_at")
               for name in ("open_interest", "mark_price", "index_price", "predicted_funding_rate")
               if name in bundle.observations]
    for frame, source_column, feature_name, observed_column in inputs:
        collision = {feature_name, feature_name + "_available_at", feature_name + "_observed_at", feature_name + "_stale"} & set(result)
        if collision:
            raise ValueError(f"derivative feature would overwrite existing columns: {sorted(collision)}")
        right = frame[["available_at", observed_column, source_column]].sort_values("available_at")
        if right["available_at"].duplicated().any():
            raise ValueError("ambiguous derivative availability timestamp")
        merged = pd.merge_asof(left, right, left_on="_cutoff", right_on="available_at", direction="backward")
        stale = merged[observed_column].isna() | ((merged["_cutoff"] - merged[observed_column]) > age_limit)
        result[feature_name] = merged[source_column].mask(stale).to_numpy()
        for source, suffix in (("available_at", "_available_at"), (observed_column, "_observed_at")):
            result[feature_name + suffix] = merged[source].map(lambda value: None if pd.isna(value) else value.isoformat()).to_numpy()
        result[feature_name + "_stale"] = stale.to_numpy()
    result["derivative_contract_id"] = bundle.spec.contract_id
    result["derivative_contract_spec_digest"] = bundle.spec.digest
    result["funding_settlement_mode"] = "event_replay_required"
    result.attrs.update(bars.attrs)
    result.attrs["derivatives"] = {"provenance": dict(bundle.provenance), "quality": dict(bundle.quality),
                                   "feature_max_age": str(age_limit),
                                   "stale_counts": {name: int(result[name].sum()) for name in result if name.endswith("_stale")}}
    return result


def replay_funding_settlements(spec: LinearContractSpec, position_events: pd.DataFrame,
                               settlements: pd.DataFrame, *, start, end,
                               initial_contracts: float = 0.0) -> dict:
    """Pure funding ledger for ``(start, end]`` using complete position events.

    ``initial_contracts`` is the position just after start's settlement.
    ``position_events`` contains timestamp, position_contracts and contract_id;
    positions after each fill are effective strictly after that timestamp.
    Inputs must cover all fills in the requested range. Bar-end snapshots do
    not establish this contract. No fills or account credentials are fetched.
    """
    start, end = as_utc_timestamp(start), as_utc_timestamp(end)
    if pd.isna(start) or pd.isna(end) or end <= start:
        raise ValueError("funding replay requires a finite increasing range")
    if isinstance(initial_contracts, bool) or not math.isfinite(float(initial_contracts)):
        raise ValueError("initial_contracts must be finite")
    frame = validate_settlements(spec, settlements)
    frame = frame[(frame["settlement_time"] > start) & (frame["settlement_time"] <= end)].copy()
    interval = pd.Timedelta(hours=spec.funding_interval_hours)
    anchor = as_utc_timestamp(spec.settlement_anchor)
    first = anchor + ((start - anchor) // interval + 1) * interval
    expected = pd.date_range(first, end, freq=interval)
    if not frame["settlement_time"].tolist() == expected.tolist():
        raise ValueError("missing funding settlements in replay range; refusing unknown costs")
    positions = position_events.copy()
    _identity(positions, spec)
    _timestamp_column(positions, "timestamp")
    _numeric(positions, "position_contracts")
    if not positions["timestamp"].is_monotonic_increasing or positions["timestamp"].duplicated().any():
        raise ValueError("position events must be sorted and unique; combine simultaneous fills explicitly")
    if ((positions["timestamp"] < start) | (positions["timestamp"] > end)).any():
        raise ValueError("position events lie outside replay range")
    records = positions.to_dict("records")
    cursor, contracts = 0, float(initial_contracts)
    ledger = []
    for row in frame.to_dict("records"):
        while cursor < len(records) and records[cursor]["timestamp"] < row["settlement_time"]:
            contracts = float(records[cursor]["position_contracts"])
            cursor += 1
        base_qty = contracts * spec.contract_multiplier
        amount = base_qty * float(row["mark_price"]) * float(row["funding_rate"])
        ledger.append({"event_id": f"{spec.identity}:{row['settlement_time'].isoformat()}",
                       "timestamp": row["settlement_time"].isoformat(), "available_at": row["available_at"].isoformat(),
                       "contract_spec_digest": spec.digest, "symbol": spec.symbol, "kind": "funding",
                       "rate": float(row["funding_rate"]), "mark_price": float(row["mark_price"]),
                       "position_contracts": contracts, "base_quantity": base_qty,
                       "notional": abs(base_qty) * float(row["mark_price"]), "amount": amount,
                       "currency": spec.settlement_currency, "source": "offline_settlement_position_events"})
    total = math.fsum(row["amount"] for row in ledger)
    return {"ledger": ledger, "net_funding_expense": total,
            "gross_funding_paid": math.fsum(max(row["amount"], 0.0) for row in ledger),
            "gross_funding_received": math.fsum(max(-row["amount"], 0.0) for row in ledger),
            "cash_change": -total, "equity_change_from_funding": -total,
            "coverage": {"start_exclusive": start.isoformat(), "end_inclusive": end.isoformat(),
                         "expected_settlements": len(expected), "replayed_settlements": len(ledger), "status": "complete"},
            "contract_spec": asdict(spec), "contract_spec_digest": spec.digest,
            "position_contract": "caller supplies complete post-fill snapshots; same-time fills occur after settlement",
            "scope": "funding only; price PnL, trading fees, borrow and basis must be reconciled separately"}
