"""Shared strict selection/signal time context over original source revisions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from signal_meta.signal_observation_types import fingerprint
from core.market.temporal_data import (
    TemporalDataError,
    TemporalDataPolicy,
    TemporalOHLCV,
    strategy_decision_cutoff,
    temporal_policy,
)


def utc(value):
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise TemporalDataError("finite decision cutoff required")
    return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")


@dataclass(frozen=True)
class SelectionTemporalContext:
    decision_cutoff: Any
    policy: Any = field(default_factory=lambda: TemporalDataPolicy(mode="strict"))

    def __post_init__(self):
        object.__setattr__(self, "decision_cutoff", utc(self.decision_cutoff))
        object.__setattr__(self, "policy", temporal_policy(self.policy))

    def to_dict(self):
        return {
            "schema": "selection-temporal-context/v1",
            "decision_cutoff": self.decision_cutoff.isoformat(),
            "mode": self.policy.mode,
            "knowledge": self.policy.knowledge,
            "unknown": self.policy.unknown,
        }

    @property
    def identity(self):
        return fingerprint(self.to_dict())

    @classmethod
    def from_mapping(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TemporalDataError("selection temporal context object required")
        if value.get("schema", "selection-temporal-context/v1") != "selection-temporal-context/v1":
            raise TemporalDataError("unsupported selection temporal context")
        policy = value.get("policy") or {
            key: value[key] for key in ("mode", "knowledge", "unknown") if key in value
        }
        return cls(value["decision_cutoff"], policy or {"mode": "strict"})

    @classmethod
    def from_event(cls, event):
        close = utc(event.timestamp) + pd.Timedelta(days=1)
        explicit = getattr(event, "selection_temporal_context", None)
        if explicit is not None:
            context = cls.from_mapping(explicit)
        elif getattr(event, "source", None) == "historical_strict" or any(
            (frame.attrs.get("temporal_audit") or {}).get("mode") == "strict"
            for frame in getattr(event, "histories", {}).values()
        ):
            audits = [frame.attrs.get("temporal_audit") or {} for frame in event.histories.values()]
            if not audits or any(a.get("mode") != "strict" or not a.get("as_of") for a in audits):
                raise TemporalDataError("strict ML event requires every source's actual cutoff")
            knowledge = {a.get("knowledge") for a in audits}
            unknown = {a.get("unknown_policy", "exclude") for a in audits}
            if len(knowledge) != 1 or len(unknown) != 1:
                raise TemporalDataError("ML histories have inconsistent knowledge policies")
            context = cls(
                strategy_decision_cutoff(event, close),
                {
                    "mode": "strict",
                    "knowledge": next(iter(knowledge)),
                    "unknown": next(iter(unknown)),
                },
            )
        else:
            context = cls(close, {"mode": "retrospective"})
        if context.decision_cutoff < close:
            raise TemporalDataError("ML decision cutoff precedes signal candle close")
        return context

    def visible(self, source, *, symbol, timeframe="1d"):
        """Return the existing store's latest visible revisions, including withdrawals."""
        # The context selects knowledge; constructing a view never changes a
        # live collector's policy, records, durable state or counters.
        policy = TemporalDataPolicy(
            mode=self.policy.mode, knowledge=self.policy.knowledge, unknown=self.policy.unknown
        )
        view = TemporalOHLCV(
            f"ml-selection|{symbol}|{timeframe}", timeframe=timeframe, policy=policy
        )
        if isinstance(source, TemporalOHLCV):
            if source.timeframe != timeframe:
                raise TemporalDataError("source timeframe differs from ML selection context")
            view.dataset_id = source.dataset_id
            for record in source.records:
                view._accept(deepcopy(record))
        elif isinstance(source, pd.DataFrame):
            if not view.import_identity(source):
                envelopes = source.attrs.get("temporal_source_versions")
                if envelopes:
                    for envelope in envelopes.values():
                        if fingerprint(envelope["record"]) != envelope.get("record_sha256"):
                            raise TemporalDataError("source envelope byte identity mismatch")
                        view._accept(deepcopy(envelope["record"]))
                    view._verify_frame_versions(source)
                else:
                    view.ingest(source)
        else:
            raise TemporalDataError("ML sources must be DataFrames or TemporalOHLCV stores")
        result = view.as_of(self.decision_cutoff)
        evidence = result.attrs.get("temporal_source_versions", {})
        availability = []
        for at in result.index:
            record = evidence[utc(at).isoformat()]["record"]
            known = (
                utc(record["available_at"])
                if record["available_at"]
                else utc(at) + pd.Timedelta(days=1)
            )
            if self.policy.mode == "strict" and self.policy.knowledge == "local":
                known = max(known, utc(record["observed_at"]))
            availability.append(known)
        result["available_at"] = pd.to_datetime(availability, utc=True)
        result.attrs["selection_temporal_context"] = self.to_dict()
        result.attrs["selection_temporal_context_id"] = self.identity
        return result
