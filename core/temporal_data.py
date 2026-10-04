"""为导入、采集和行情适配提供可选的 OHLCV 时间版本边界。

retrospective 保留最终数据回放兼容行为；strict 按决策时刻和可用性证据筛选
原始 OHLCV，再由下游重算指标。bar 开盘时间、来源可用时间和本地观察时间
分别记录，不互相代替。快照只追加发生变化的版本批次，避免逐 bar 保存全量数据。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math

import pandas as pd

from core.data_versions import DataVersionStore, _record
from core.timeframes import timeframe_delta


OHLCV = ["open", "high", "low", "close", "volume"]


class TemporalDataError(ValueError):
    """A strict data boundary lacks admissible historical evidence."""


@dataclass(frozen=True)
class TemporalDataPolicy:
    """选择回放口径；strict 下 local 要求本地已观察，published 要求发布证据。

    unknown 控制缺少可用性证据时跳过还是拒绝；持久化快照本身不会补足证据。
    """

    mode: str = "retrospective"
    knowledge: str = "local"
    unknown: str = "exclude"
    store_path: str | None = None
    decision_delay_seconds: float = 0.

    def __post_init__(self):
        if self.mode not in {"retrospective", "strict"}:
            raise ValueError("temporal mode must be retrospective or strict")
        if self.knowledge not in {"local", "published"} or self.unknown not in {"exclude", "raise"}:
            raise ValueError("invalid temporal knowledge/unknown policy")
        if not math.isfinite(self.decision_delay_seconds) or self.decision_delay_seconds < 0:
            raise ValueError("decision_delay_seconds must be finite and nonnegative")


def temporal_policy(value=None) -> TemporalDataPolicy:
    if isinstance(value, TemporalDataPolicy):
        return value
    if isinstance(value, str):
        return TemporalDataPolicy(mode=value)
    return TemporalDataPolicy(**(value or {}))


def _utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise TemporalDataError("finite timestamp required")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _optional(value):
    return None if value is None or (not isinstance(value, (dict, list)) and pd.isna(value)) else value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def compatibility_audit():
    return {"mode": "retrospective", "historical_availability": "unknown",
            "immutable_snapshot": "not_configured", "strategy_history": "legacy_final_frame",
            "point_in_time_certified": False}


def strict_market_event(event):
    return event.source == "historical_strict" or any(
        (frame.attrs.get("temporal_audit") or {}).get("mode") == "strict"
        for frame in event.histories.values())


def strategy_market_view(event):
    """从可见历史构建策略视图，隔离事件中供撮合使用的已实现价格。

    当前 bar 尚不可见时，不向策略提供该 bar。entry_blocked 可从撮合视图继承
    以保守禁止入场，但已实现视图中的修订价格和成交量不能进入决策输入。
    """
    if not strict_market_event(event):
        return event
    bars, positions, histories = {}, {}, {}
    for symbol, history in event.histories.items():
        # The observer consumes proof on the original event before strategies
        # run. Feature/risk consumers need audit metadata, not N source records
        # copied onto each Series they slice while computing indicators.
        calculation = history.copy(deep=False)
        calculation.attrs = {key: value for key, value in history.attrs.items()
                             if key != "temporal_source_versions"}
        histories[symbol] = calculation
        if symbol not in event.bars or history.empty or event.timestamp not in history.index:
            continue
        location = history.index.get_loc(event.timestamp)
        if not isinstance(location, int):
            raise TemporalDataError("strategy history must have unique timestamps")
        bar = pd.Series(history.to_numpy(copy=False)[location].copy(),
                        index=history.columns, name=history.index[location])
        realised = event.bars.get(symbol)
        if realised is not None and bool(realised.get("entry_blocked", False)):
            bar["entry_blocked"] = True
        bars[symbol], positions[symbol] = bar, location
    return replace(event, bars=bars, positions=positions, histories=histories)


def strategy_market_prices(event, fallback=None):
    """取策略已知的最近收盘价；strict 缺价时不借用已实现价格补全。"""
    if not strict_market_event(event):
        return dict(fallback or {})
    prices = {}
    for symbol, history in event.histories.items():
        position = history.index.searchsorted(event.timestamp, side="right")-1
        if position >= 0:
            price = float(history.iat[position, history.columns.get_loc("close")])
            if math.isfinite(price) and price > 0:
                prices[symbol] = price
    return prices


def strategy_decision_cutoff(event, default):
    """读取统一的 as-of 截止时间，保留实盘接收延迟，拒绝不同步的历史视图。"""
    if not strict_market_event(event):
        return _utc(default)
    cutoffs = {_utc(audit["as_of"]) for history in event.histories.values()
               if (audit := history.attrs.get("temporal_audit") or {}).get("as_of")}
    if len(cutoffs) > 1:
        raise TemporalDataError("strategy histories use inconsistent as-of cutoffs")
    cutoff = next(iter(cutoffs), _utc(default))
    if cutoff < _utc(default):
        raise TemporalDataError("strategy cutoff precedes the closed bar")
    return cutoff


class TemporalOHLCV:
    """维护单一数据集的追加式版本，并按指定截止时间读取历史。

    available_at 必须附带证据类型和引用，CSV 时间列本身不足以证明可用性。
    local_receipt 只证明本采集器收到完整 bar 的时间，不能充当来源发布时间。
    """

    def __init__(self, dataset_id, *, timeframe, policy=None):
        self.policy = temporal_policy(policy)
        self.dataset_id = str(dataset_id)
        self.timeframe = timeframe
        self.delta = pd.Timedelta(timeframe_delta(timeframe))
        self.store = DataVersionStore(self.policy.store_path) if self.policy.store_path else None
        self.records = []
        self._claims = {}
        self._latest = {}
        self.snapshot_ids = []
        self.audit = {"mode": self.policy.mode, "knowledge": self.policy.knowledge,
            "unknown_policy": self.policy.unknown, "historical_availability": "unknown",
            "point_in_time_certified": False, "revision_rows": 0, "unknown_rows": 0,
            "excluded_unknown": 0, "excluded_unavailable": 0,
            "timestamp_semantics": "bar open; availability is a separate documented instant",
            "decision_cutoff": "bar open + timeframe + decision_delay_seconds",
            "decision_delay_seconds": self.policy.decision_delay_seconds,
            "persisted": self.store is not None}
        if self.store:
            # Once per collector/dataset startup, recover append-only batches.
            # No directory scan or snapshot write occurs per decision/bar.
            directory = self.store.root / "snapshots"
            for path in sorted(directory.glob("*.json")):
                manifest = json.loads(path.read_bytes())
                if (manifest.get("dataset_id") == self.dataset_id and
                        manifest.get("metadata", {}).get("append_only_revision_batch")):
                    bundle = self.store.read_snapshot(path.stem)
                    if bundle["manifest"]["metadata"].get("timeframe") != self.timeframe:
                        raise TemporalDataError("stored temporal timeframe changed")
                    for record in bundle["records"]:
                        self._accept(record)
                    self.snapshot_ids.append(path.stem)
            self._refresh_audit()

    def _accept(self, record):
        record = _record(record)
        key = (record["record_id"], record["revision_id"])
        if key in self._claims:
            if self._claims[key] != record:
                raise TemporalDataError("conflicting temporal revision identity")
            return False
        self._claims[key] = record
        self.records.append(record)
        # Import order is not a version ordering guarantee.
        rank = (record["revision_at"] or record["available_at"] or record["observed_at"], record["observed_at"])
        previous = self._latest.get(record["record_id"])
        if previous is None or rank >= (previous["revision_at"] or previous["available_at"] or previous["observed_at"], previous["observed_at"]):
            self._latest[record["record_id"]] = record
        return True

    def import_identity(self, frame):
        """恢复冻结身份指定的原始版本，并核对 OHLCV；不采信派生指标列。"""
        identity = frame.attrs.get("temporal_identity") or {}
        snapshots = identity.get("snapshot_ids", [])
        path = identity.get("store_path")
        if snapshots and path:
            source = DataVersionStore(path)
            if self.records and self.dataset_id != identity.get("dataset_id"):
                raise TemporalDataError("temporal dataset identity changed")
            if (not set(self.snapshot_ids).issubset(snapshots) or
                    (self.store is not None and self.store.root != source.root)):
                # 冻结身份只授权这些批次；磁盘新增版本不能悄悄扩展已冻结的回放。
                self.records, self._claims, self._latest, self.snapshot_ids = [], {}, {}, []
            for snapshot_id in snapshots:
                if snapshot_id in self.snapshot_ids:
                    continue
                bundle = source.read_snapshot(snapshot_id)
                if bundle["manifest"]["dataset_id"] != identity.get("dataset_id"):
                    raise TemporalDataError("snapshot dataset identity mismatch")
                if bundle["manifest"].get("metadata", {}).get("timeframe") != self.timeframe:
                    raise TemporalDataError("snapshot timeframe identity mismatch")
                for record in bundle["records"]:
                    self._accept(record)
            self.dataset_id = identity["dataset_id"]
            self.store = source
            self.snapshot_ids = list(snapshots)
            self._verify_frame_versions(frame)
            if self.identity["records_sha256"] != identity.get("records_sha256"):
                raise TemporalDataError("frozen temporal record digest mismatch")
            self._refresh_audit()
            return True
        memory_records = frame.attrs.get("temporal_records", [])
        if memory_records and identity:
            if identity.get("timeframe") != self.timeframe:
                raise TemporalDataError("snapshot timeframe identity mismatch")
            self.records, self._claims, self._latest, self.snapshot_ids = [], {}, {}, []
            self.dataset_id = identity["dataset_id"]
            self.store = None
        for record in memory_records:
            self._accept(record)
        if memory_records:
            self._verify_frame_versions(frame)
            if identity and self.identity["records_sha256"] != identity.get("records_sha256"):
                raise TemporalDataError("frozen temporal record digest mismatch")
        self._refresh_audit()
        return bool(frame.attrs.get("temporal_records"))

    def _verify_frame_versions(self, frame):
        known = {}
        for record in self.records:
            if record.get("record_type") == "tombstone":
                continue
            known.setdefault(record["record_id"], set()).add(tuple(record["data"][column] for column in OHLCV))
        for at, row in frame.iterrows():
            if tuple(float(row[column]) for column in OHLCV) not in known.get(_utc(at).isoformat(), set()):
                raise TemporalDataError("frame values differ from frozen temporal versions")

    def ingest(self, frame, *, observed_at=None, local_receipt=False, source_reference=None):
        """追加原始版本；本地采集只为已收盘 bar 建立接收时刻的可用性证据。"""
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise TemporalDataError("temporal OHLCV requires a DatetimeIndex")
        receipt = _utc(observed_at or datetime.now(timezone.utc)).isoformat()
        changed = []
        for at, row in frame.iterrows():
            event = _utc(at)
            values = {column: float(row[column]) for column in OHLCV}
            if not all(math.isfinite(value) for value in values.values()):
                raise TemporalDataError("finite raw OHLCV required for immutable versions")
            observed = _optional(row.get("observed_at")) or receipt
            available = _optional(row.get("available_at"))
            evidence = _optional(row.get("availability_evidence")) or frame.attrs.get("availability_evidence")
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            if evidence is None and _optional(row.get("availability_source")) and _optional(row.get("availability_reference")):
                evidence = {"kind": row["availability_source"], "reference": row["availability_reference"]}
            if local_receipt:
                observed = receipt
                available = receipt if event+self.delta <= _utc(receipt) else None
                evidence = {"kind": "local_receipt", "reference": source_reference or self.dataset_id} if available else None
            elif not evidence:
                # 未附证据的 CSV 时间或名义收盘时间仍属未知，不能升级为历史发布事实。
                available = None
            if available is not None and _utc(available) < event+self.delta:
                raise TemporalDataError("complete OHLCV available before candle close")
            event_id = event.isoformat()
            previous = self._latest.get(event_id)
            declared_revision = _optional(row.get("revision_id"))
            prior_claim = self._claims.get((event_id, str(declared_revision))) if declared_revision is not None else previous
            if (not local_receipt and prior_claim and _optional(row.get("observed_at")) is None
                    and prior_claim["data"] == values
                    and (prior_claim["available_at"] is None if available is None else
                         prior_claim["available_at"] is not None and _utc(prior_claim["available_at"]) == _utc(available))):
                observed = prior_claim["observed_at"]
            if (not local_receipt and previous and available is None and
                    previous["available_at"] is None and previous["data"] == values and
                    _optional(row.get("revision_id")) is None):
                continue
            if local_receipt and previous and previous["data"] == values:
                previous_local = (previous.get("availability_evidence") or {}).get("kind") == "local_receipt"
                if previous_local and bool(previous["available_at"]) == bool(available):
                    continue
                if available is None and previous["available_at"] is None:
                    continue
            record = {"record_id": event_id, "event_time": event_id,
                "observed_at": str(observed), "available_at": str(available) if available is not None else None,
                "availability_evidence": evidence if available is not None else None,
                "published_at": _optional(row.get("published_at")),
                "revision_at": _optional(row.get("revision_at")), "data": values}
            for key in ("published_at", "revision_at"):
                if record[key] is not None:
                    record[key] = str(record[key])
            record["revision_id"] = str(_optional(row.get("revision_id")) or _digest(record))
            if self._accept(record):
                changed.append(self._claims[(event_id, record["revision_id"])])
        if changed and self.store:
            snapshot = self.store.create_snapshot(self.dataset_id, changed,
                metadata={"timeframe": self.timeframe, "append_only_revision_batch": True},
                source_refs={"reference": source_reference or self.dataset_id})
            self.snapshot_ids.append(snapshot["snapshot_id"])
        self._refresh_audit()
        return self.annotate(frame.copy(deep=False))

    def _refresh_audit(self):
        unknown = sum(record["available_at"] is None for record in self.records)
        self.audit.update(revision_rows=len(self.records), unknown_rows=unknown,
            tombstone_rows=sum(record.get("record_type") == "tombstone" for record in self.records),
            historical_availability="documented" if self.records and not unknown else "unknown_or_mixed",
            persisted=self.store is not None)

    def retract(self, event_time, *, revision_id, available_at, observed_at,
                availability_evidence, reason, revision_at=None, source_reference=None):
        """追加带可用性证据的撤回版本；数据源本轮漏掉一行不构成撤回。"""
        event_id = _utc(event_time).isoformat()
        record = _record({"record_id": event_id, "event_time": event_id,
            "revision_id": revision_id, "available_at": str(available_at),
            "observed_at": str(observed_at), "revision_at": str(revision_at) if revision_at is not None else None,
            "availability_evidence": availability_evidence, "record_type": "tombstone",
            "retraction_reason": reason, "data": {}})
        if self._accept(record) and self.store:
            snapshot = self.store.create_snapshot(self.dataset_id, [record],
                metadata={"timeframe": self.timeframe, "append_only_revision_batch": True},
                source_refs={"reference": source_reference or self.dataset_id})
            self.snapshot_ids.append(snapshot["snapshot_id"])
        self._refresh_audit()
        return deepcopy(record)

    @property
    def identity(self):
        return {"schema": "temporal_ohlcv/v1", "dataset_id": self.dataset_id,
            "timeframe": self.timeframe, "snapshot_ids": list(self.snapshot_ids),
            "store_path": str(self.store.root) if self.store else None,
            "records_sha256": _digest(sorted(self.records, key=lambda r: (r["record_id"], r["revision_id"]))),
            "policy": asdict(self.policy)}

    def annotate(self, frame):
        frame.attrs["temporal_identity"] = self.identity
        frame.attrs["temporal_audit"] = dict(self.audit)
        if not self.store:
            frame.attrs["temporal_records"] = list(self.records)
        return frame

    def as_of(self, at, *, allowed_times=None):
        """先筛选已收盘且符合知识口径的版本，再为每个 bar 选择最新版本。

        tombstone 参与版本选择后才从行数据移除，因此撤回后不会退回旧值；
        证据映射保留撤回事实，供标签生产者追加失效版本。
        """
        cutoff = _utc(at)
        allowed = None if allowed_times is None else {_utc(value).isoformat() for value in allowed_times}
        chosen, rank_by_id = {}, {}
        unknown_ids = set()
        unavailable = 0
        for record in self.records:
            if allowed is not None and record["record_id"] not in allowed:
                continue
            if _utc(record["event_time"])+self.delta > cutoff:
                continue
            available = record["available_at"]
            if self.policy.mode == "strict":
                if available is None:
                    unknown_ids.add(record["record_id"])
                    continue
                evidence_kind = record["availability_evidence"]["kind"]
                if (_utc(available) > cutoff or
                    (self.policy.knowledge == "local" and _utc(record["observed_at"]) > cutoff) or
                    (self.policy.knowledge == "published" and evidence_kind != "source_publication")):
                    unavailable += 1
                    continue
            rank = (_utc(record["revision_at"] or available or record["observed_at"]), _utc(record["observed_at"]))
            key = record["record_id"]
            if key in rank_by_id and rank == rank_by_id[key] and record != chosen[key]:
                raise TemporalDataError("ambiguous simultaneously available revisions")
            if key not in rank_by_id or rank > rank_by_id[key]:
                chosen[key], rank_by_id[key] = record, rank
        unknown = len(unknown_ids-set(chosen))
        self.audit["excluded_unknown"] += unknown
        self.audit["excluded_unavailable"] += unavailable
        if unknown and self.policy.mode == "strict" and self.policy.unknown == "raise":
            raise TemporalDataError("historical availability unknown; strict history rejected")
        active = {key: record for key, record in chosen.items() if record.get("record_type") != "tombstone"}
        rows = [record["data"] for _, record in sorted(active.items())]
        index = pd.DatetimeIndex([_utc(key).tz_localize(None) for key in sorted(active)], name="timestamp")
        result = pd.DataFrame(rows, index=index, columns=OHLCV)
        result.attrs["temporal_audit"] = {**self.audit, "as_of": cutoff.isoformat()}
        # 证据复制并附内容哈希，下游可同时核对版本身份和可见 OHLCV，
        # 不能仅凭一个 as_of 字符串宣称数据当时已知。
        result.attrs["temporal_source_versions"] = {
            key: {"dataset_id": self.dataset_id, "record_sha256": _digest(record),
                  "record": deepcopy(record)} for key, record in chosen.items()}
        return result


def freeze_ohlcv(frame, *, symbol, timeframe, policy=None, observed_at=None,
                 local_receipt=False, source_reference="unknown", dataset_id=None):
    history = TemporalOHLCV(dataset_id or f"{source_reference}|{symbol}|{timeframe}",
                          timeframe=timeframe, policy=policy)
    if not history.import_identity(frame):
        history.ingest(frame, observed_at=observed_at, local_receipt=local_receipt,
                       source_reference=source_reference)
    return history.annotate(frame.copy(deep=False))
