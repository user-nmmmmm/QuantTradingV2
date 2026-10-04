"""基于可见 OHLCV 证据追加固定持有期标签，保留修订与撤回历史。

协议按声明的时间证据建立可用性边界，不认证外部发布者，也不证明模拟成交真实
发生。带融资成本的标签还必须具备完整的版本化结算/借款证据，默认费率不能
让标签取得训练资格。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import math
from types import SimpleNamespace

import pandas as pd

from core.data_versions import _record
from core.signal_observation_types import close_time, fingerprint, iso
from core.signal_outcomes import ForwardOutcomeTracker
from core.temporal_data import OHLCV, strict_market_event
from core.timeframes import as_utc_timestamp, timeframe_delta
from core.temporal_financing import financing_cost, select_financing_versions, FINANCING_CALCULATOR


LABEL_PROTOCOL = "strict-fixed-horizon/v1"


class TemporalLabelError(ValueError):
    """Label evidence is internally inconsistent or an immutable claim changed."""


class VersionedOutcomeTracker:
    """在证据完整后重放持有窗口，复用既有收益计算器并追加标签版本。

    results 为每个候选/持有期提供最新结果，revisions 保留全部历史。训练需先
    选择自身截止时间之前可用的最新版本，再检查资格；最新版本失效时，不能
    回退到旧的成熟标签。缺失证据先等待补齐，finish 才输出相应 censored 状态。
    返回值均为独立副本，调用方无法据此改写内部历史。
    """

    def __init__(self, policy, costs, *, knowledge=None, financing=None):
        self.policy, self.costs = policy, costs
        self._candidates, self._candidate_hashes = {}, {}
        self._sources, self._claims, self._datasets = {}, {}, {}
        self._latest, self._revisions, self._revision_claims = {}, [], {}
        self._last_inputs, self._unresolved = {}, {}
        self._windows, self._last_source_ids = {}, {}
        self._candidate_sources = {}
        self._cutoff = None
        self.financing, self._financing_versions = financing, {}
        self.knowledge = knowledge
        self.cost_policy_sha256 = fingerprint(asdict(costs))

    @property
    def results(self):
        return deepcopy(list(self._latest.values()))

    @property
    def revisions(self):
        return deepcopy(self._revisions)

    @property
    def protocol(self):
        return {"schema": LABEL_PROTOCOL, "versioned": True,
            "knowledge": self.knowledge, "cost_policy_sha256": self.cost_policy_sha256,
            "strict_training_scope": "raw_ohlcv; financed accounts require complete versioned financing evidence",
            "financing_calculator": FINANCING_CALCULATOR,
            "financing_identity": self.financing.identity if self.financing is not None else None,
            "training_selection": "latest revision whose available_at < training cutoff, then eligibility",
            "availability_rule": "max(candidate, source availability/knowledge, window close, first computation observation)",
            "execution_scope": "independent fixed-notional simulated diagnostic; not actual fills",
            "source_revision_policy": "append_only revisions including explicit tombstones; latest withdrawal never falls back",
            "missing_source_policy": "later omitted rows do not revoke previously verified source facts",
            "delayed_candidate_policy": "next open at/after actual availability; training ineligible in v1",
            "point_in_time_certified": False}

    def add(self, candidate):
        digest = fingerprint(candidate.to_dict())
        prior = self._candidate_hashes.get(candidate.candidate_id)
        if prior is not None:
            if prior != digest:
                raise TemporalLabelError("candidate identity changed")
            return
        if not math.isfinite(candidate.reference_price) or candidate.reference_price <= 0:
            raise TemporalLabelError("candidate price must be positive and finite")
        if as_utc_timestamp(candidate.context.available_at) < as_utc_timestamp(
                close_time(candidate.timestamp, candidate.context.timeframe)):
            raise TemporalLabelError("candidate available before its closed signal bar")
        self._candidate_hashes[candidate.candidate_id] = digest
        self._candidates[candidate.candidate_id] = deepcopy(candidate)
        if self._cutoff is not None:
            self._evaluate(candidate)

    def _ingest(self, event):
        if not strict_market_event(event):
            raise TemporalLabelError("versioned outcomes require strict source histories")
        cutoffs, knowledge = set(), set()
        for frame in event.histories.values():
            audit = frame.attrs.get("temporal_audit") or {}
            if audit.get("mode") != "strict" or not audit.get("as_of"):
                raise TemporalLabelError("strict label history lacks its as-of evidence boundary")
            cutoffs.add(as_utc_timestamp(audit["as_of"]))
            knowledge.add(audit.get("knowledge"))
        if len(cutoffs) != 1 or len(knowledge) != 1 or next(iter(knowledge)) not in {"local", "published"}:
            raise TemporalLabelError("label histories have inconsistent cutoff/knowledge")
        cutoff, known = next(iter(cutoffs)), next(iter(knowledge))
        if self._cutoff is not None and cutoff < self._cutoff:
            raise TemporalLabelError("label observation time regressed")
        if self.knowledge is not None and known != self.knowledge:
            raise TemporalLabelError("label knowledge policy changed")
        self.knowledge = known
        for symbol, frame in event.histories.items():
            evidence = frame.attrs.get("temporal_source_versions") or {}
            if not frame.index.is_unique:
                raise TemporalLabelError("duplicate source timestamps")
            visible_index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index.tz_convert("UTC")
            visible_positions = {at: i for i, at in enumerate(visible_index)}
            # A Series extracted by iloc inherits/deepcopies all DataFrame attrs.
            # Read raw values without making N copies of N source envelopes.
            values = frame.to_numpy(copy=False)
            columns = {column: frame.columns.get_loc(column) for column in OHLCV}
            # 指标历史可能已截短，但证据映射保留截止时刻的原始版本。
            # 仍需处理策略回看窗口以外的晚到修订，否则旧标签无法正确更新。
            for at, envelope in evidence.items():
                moment = as_utc_timestamp(at)
                try:
                    record = _record(envelope["record"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise TemporalLabelError("invalid label source record") from exc
                digest = fingerprint(record)
                if digest != envelope.get("record_sha256"):
                    raise TemporalLabelError("source record hash mismatch")
                dataset = envelope.get("dataset_id")
                if not isinstance(dataset, str) or not dataset:
                    raise TemporalLabelError("missing source dataset identity")
                if symbol in self._datasets and self._datasets[symbol] != dataset:
                    raise TemporalLabelError("source dataset changed for symbol")
                self._datasets[symbol] = dataset
                if as_utc_timestamp(record["event_time"]) != moment or as_utc_timestamp(record["record_id"]) != moment:
                    raise TemporalLabelError("source event identity differs from visible row")
                position = visible_positions.get(moment)
                withdrawn = record.get("record_type") == "tombstone"
                if withdrawn and position is not None:
                    raise TemporalLabelError("withdrawn source still has a visible OHLCV row")
                if position is not None:
                    if any(float(values[position, columns[column]]) != record["data"].get(column) for column in OHLCV):
                        raise TemporalLabelError("visible OHLCV differs from source version")
                if not withdrawn and any(column not in record["data"] or not math.isfinite(float(record["data"][column]))
                       for column in OHLCV):
                    raise TemporalLabelError("source must contain finite raw OHLCV")
                if (record["available_at"] is None or as_utc_timestamp(record["available_at"]) > cutoff
                        or moment+timeframe_delta(event.timeframe) > cutoff
                        or (known == "local" and as_utc_timestamp(record["observed_at"]) > cutoff)
                        or (known == "published" and record["availability_evidence"]["kind"] != "source_publication")):
                    raise TemporalLabelError("source version is not available under declared knowledge")
                key = (dataset, record["record_id"], record["revision_id"])
                if key in self._claims and self._claims[key] != digest:
                    raise TemporalLabelError("immutable source revision identity changed")
                self._claims[key] = digest
                envelope = {"dataset_id": dataset, "record_sha256": digest, "record": record}
                records = self._sources.setdefault(symbol, {})
                prior = records.get(moment)
                if prior is not None:
                    def rank(source):
                        value = source["record"]
                        return (as_utc_timestamp(value["revision_at"] or value["available_at"]),
                                as_utc_timestamp(value["observed_at"]))
                    if rank(envelope) < rank(prior):
                        raise TemporalLabelError("visible source revision regressed")
                    if rank(envelope) == rank(prior) and digest != prior["record_sha256"]:
                        raise TemporalLabelError("ambiguous simultaneously available source revisions")
                records[moment] = deepcopy(envelope)
        self._cutoff = cutoff

    def advance(self, event):
        self._ingest(event)
        if self.financing is not None:
            self._financing_versions = self.financing.as_of(self._cutoff, knowledge=self.knowledge)
        for candidate in self._candidates.values():
            self._evaluate(candidate)

    @staticmethod
    def _window(candidate, horizon):
        delta = pd.Timedelta(timeframe_delta(candidate.context.timeframe))
        available = as_utc_timestamp(candidate.context.available_at)
        entry = as_utc_timestamp(candidate.timestamp)+delta
        # 决策晚到时顺延到可用时间之后（含当刻）的首个开盘，不能补造过去的入场。
        if available > entry:
            entry += math.ceil((available-entry)/delta)*delta
        return pd.date_range(entry, periods=horizon, freq=delta), entry+horizon*delta

    def _identity(self, candidate, horizon):
        return {"candidate_id": candidate.candidate_id, "strategy": candidate.strategy,
            "symbol": candidate.symbol, "direction": candidate.direction,
            "horizon_bars": horizon, "scope": "independent_fixed_notional_signal_diagnostic"}

    def _evaluate(self, candidate):
        if as_utc_timestamp(candidate.context.available_at) > self._cutoff:
            return
        for horizon in self.policy.horizons:
            key = (candidate.candidate_id, horizon)
            if key not in self._windows:
                self._windows[key] = self._window(candidate, horizon)
            window, window_close = self._windows[key]
            known = self._sources.get(candidate.symbol, {})
            signal_head = known.get(as_utc_timestamp(candidate.timestamp))
            retracted = [source for source in [signal_head, *(known.get(at) for at in window)]
                         if source is not None and source["record"].get("record_type") == "tombstone"]
            if retracted:
                self._invalidate(candidate, horizon, "censored_source_retracted",
                    [known[at] for at in window if at in known], retractions=retracted)
                continue
            if self.costs.account_mode != "spot" and self.financing is None:
                self._unresolved[key] = "censored_unversioned_financing_evidence"
                continue
            candidate_source = self._candidate_sources.get(candidate.candidate_id)
            if candidate_source is None:
                candidate_source = self._sources.get(candidate.symbol, {}).get(as_utc_timestamp(candidate.timestamp))
                if candidate_source is not None:
                    record = candidate_source["record"]
                    candidate_at = as_utc_timestamp(candidate.context.available_at)
                    if (as_utc_timestamp(record["available_at"]) > candidate_at or
                            (self.knowledge == "local" and as_utc_timestamp(record["observed_at"]) > candidate_at)):
                        candidate_source = None
                if candidate_source is None:
                    self._unresolved[key] = "censored_candidate_source_evidence"
                    continue
                self._candidate_sources[candidate.candidate_id] = deepcopy(candidate_source)
            if window_close > self._cutoff:
                self._unresolved[key] = "censored_end_of_data"
                continue
            if any(at not in known for at in window):
                self._unresolved[key] = "censored_missing_source_evidence"
                continue
            sources = [known[at] for at in window]
            finance_versions = (select_financing_versions(self._financing_versions,
                candidate=candidate.to_dict(), account_mode=self.costs.account_mode,
                start=window[0], end=window_close) if self.costs.account_mode != "spot" else {})
            source_ids = (tuple(source["record_sha256"] for source in sources),
                          tuple((key, value["record_sha256"]) for key, value in sorted(finance_versions.items())))
            if self._last_source_ids.get(key) == source_ids:
                continue
            inputs = {"candidate_sha256": self._candidate_hashes[candidate.candidate_id],
                "horizon_bars": horizon, "cost_policy_sha256": self.cost_policy_sha256,
                "reference_notional": self.policy.reference_notional,
                "knowledge": self.knowledge, "source_versions": sources,
                "candidate_source_version": candidate_source, "financing_versions": finance_versions}
            input_fingerprint = fingerprint(inputs)
            if self._last_inputs.get(key) == input_fingerprint:
                continue
            available = [self._cutoff, window_close, as_utc_timestamp(candidate.context.available_at)]
            for source in sources:
                available.append(as_utc_timestamp(source["record"]["available_at"]))
                if self.knowledge == "local":
                    available.append(as_utc_timestamp(source["record"]["observed_at"]))
            # 复用成交报价计算；资金费/借款成本在下方按事件证据计入，
            # 这里禁用默认 bar 费率，避免重复收费或把缺证据当作已知成本。
            calc_costs = (replace(self.costs, account_mode="perpetual", funding_rate_required=False)
                          if self.costs.account_mode != "spot" else self.costs)
            calc = ForwardOutcomeTracker(replace(self.policy, horizons=(horizon,)), calc_costs)
            aligned = replace(candidate, context=replace(candidate.context, available_at=iso(window[0])))
            calc.add(aligned)
            for at, source in zip(window, sources):
                bar = pd.Series(deepcopy(source["record"]["data"]), name=at)
                calc.advance(SimpleNamespace(timestamp=at, bars={candidate.symbol: bar}))
            if len(calc.results) != 1:
                raise TemporalLabelError("complete-window outcome calculator did not produce one result")
            finance = None
            if self.costs.account_mode != "spot" and calc.results[0]["status"] == "matured":
                finance = financing_cost(finance_versions, candidate=candidate.to_dict(),
                    account_mode=self.costs.account_mode, quantity=calc.results[0]["quantity"],
                    entry_price=calc.results[0]["entry_price"], start=window[0], end=window_close,
                    cutoff=self._cutoff, knowledge=self.knowledge)
                if not finance["complete"]:
                    if key in self._latest:
                        self._invalidate(candidate, horizon, "censored_financing_evidence_invalidated", sources,
                            extra={"financing_versions": finance["source_versions"],
                                   "financing_reason": finance["reason"]})
                    else:
                        self._unresolved[key] = "censored_unversioned_financing_evidence"
                    continue
                for source in finance["source_versions"]:
                    available.append(as_utc_timestamp(source["record"]["available_at"]))
                    if self.knowledge == "local":
                        available.append(as_utc_timestamp(source["record"]["observed_at"]))
            row = {**calc.results[0], "available_at": iso(max(available)),
                "observed_at": iso(self._cutoff), "candidate_available_at": candidate.context.available_at,
                "window_close": iso(window_close), "knowledge": self.knowledge,
                "source_versions": deepcopy(sources), "input_fingerprint": input_fingerprint,
                "cost_policy_sha256": self.cost_policy_sha256, "label_protocol": LABEL_PROTOCOL,
                "account_mode": self.costs.account_mode,
                "candidate_source_version": deepcopy(candidate_source)}
            row.setdefault("execution_flags", [])
            if finance is not None:
                row.update(financing_versions=finance["source_versions"],
                    financing_input_sha256=finance["source_versions_sha256"],
                    financing_calculator=finance["calculator"], financing_ledger=finance["ledger"],
                    financing_currency=finance["currency"], net_before_financing=row["net_pnl"],
                    carry=finance["carry"])
                row["net_pnl"] -= finance["carry"]
                row["net_return_bps"] = row["net_pnl"]/(row["quantity"]*row["entry_reference"])*10000
            delayed = as_utc_timestamp(candidate.context.available_at) != as_utc_timestamp(
                close_time(candidate.timestamp, candidate.context.timeframe))
            row["training_eligible"] = (row["status"] == "matured" and
                                         (candidate.direction == "long" or self.costs.account_mode != "spot")
                                         and not row.get("execution_flags") and not delayed)
            if delayed:
                row["training_ineligible_reason"] = "delayed_candidate_not_supported_by_v1_training"
            self._append(key, row)
            self._last_inputs[key] = input_fingerprint
            self._last_source_ids[key] = source_ids
            self._unresolved.pop(key, None)

    def _invalidate(self, candidate, horizon, status, sources, *, retractions=(), extra=None):
        key = candidate.candidate_id, horizon
        evidence = {"status": status, "sources": sources, "retractions": list(retractions), "extra": extra}
        signature = ("invalid", fingerprint(evidence))
        if self._last_source_ids.get(key) == signature:
            return
        row = {**self._identity(candidate, horizon), "status": status,
            "available_at": iso(self._cutoff), "observed_at": iso(self._cutoff),
            "candidate_available_at": candidate.context.available_at,
            "entry_time": None, "net_pnl": None, "net_return_bps": None,
            "execution_flags": [], "source_versions": deepcopy(sources),
            "retraction_versions": deepcopy(list(retractions)), "knowledge": self.knowledge,
            "label_protocol": LABEL_PROTOCOL, "training_eligible": False,
            "cost_policy_sha256": self.cost_policy_sha256, "account_mode": self.costs.account_mode}
        if extra:
            row.update(deepcopy(extra))
        if candidate.candidate_id in self._candidate_sources:
            row["candidate_source_version"] = deepcopy(self._candidate_sources[candidate.candidate_id])
        self._append(key, row)
        self._last_source_ids[key] = signature
        self._last_inputs.pop(key, None)
        self._unresolved.pop(key, None)

    def _append(self, key, row):
        digest = fingerprint(row)
        revision = "label_"+digest
        if revision in self._revision_claims and self._revision_claims[revision] != row:
            raise TemporalLabelError("label revision hash conflict")
        if revision in self._revision_claims:
            return
        prior = self._latest.get(key)
        if prior is not None and as_utc_timestamp(row["available_at"]) <= as_utc_timestamp(prior["available_at"]):
            raise TemporalLabelError("changed label revision requires a later observation cutoff")
        self._revision_claims[revision] = deepcopy(row)
        result = {**deepcopy(row), "revision_id": revision, "content_sha256": digest}
        self._revisions.append(result)
        self._latest[key] = result

    def finish(self, at):
        # at 或文件结束不构成新证据；只沿用最后已验证的观察截止时间，
        # 不能因此让未到达的行情/融资事实提前可用。
        for candidate in self._candidates.values():
            for horizon in self.policy.horizons:
                key = (candidate.candidate_id, horizon)
                if key in self._latest:
                    continue
                row = {**self._identity(candidate, horizon),
                    "status": self._unresolved.get(key, "censored_missing_source_evidence"),
                    "available_at": iso(self._cutoff) if self._cutoff is not None else None,
                    "observed_at": iso(self._cutoff) if self._cutoff is not None else None,
                    "entry_time": None, "net_pnl": None, "net_return_bps": None,
                    "execution_flags": [],
                    "source_versions": [], "knowledge": self.knowledge,
                    "label_protocol": LABEL_PROTOCOL, "training_eligible": False,
                    "cost_policy_sha256": self.cost_policy_sha256, "account_mode": self.costs.account_mode}
                self._append(key, row)
