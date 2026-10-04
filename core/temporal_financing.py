"""为固定名义金额标签提供可追溯的资金费与借款成本证据。

资金费要求版本化合约条款和应发生的全部结算；借款要求决策时已知的借款许可、
连续且精确覆盖窗口的 ACT/365F 计息区间及显式费用。两者都需窗口结束后的完整性
声明，列明事件 ID；默认费率不能证明证据完整。这里计算固定仓位的研究成本，
不维护实际账户债务账本。
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import pandas as pd

from core.data_versions import DataVersionStore, _record
from core.derivatives_data import LinearContractSpec, replay_funding_settlements
from core.signal_observation_types import fingerprint, iso
from core.timeframes import as_utc_timestamp as utc
from core.universe import normalize_symbol


FINANCING_PROTOCOL = "temporal-financing-evidence/v1"
FINANCING_CALCULATOR = "fixed-position-funding-and-act365f-borrow/v1"


def _number(value, name, *, nonnegative=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"finite financing {name} required")
    if (nonnegative and value < 0) or (positive and value <= 0):
        raise ValueError(f"invalid financing {name}")
    return float(value)


def _validate_record(record):
    if record.get("record_type") == "tombstone":
        return
    data = record["data"]
    kind = data.get("kind")
    if kind not in {"linear_contract", "borrow_permission", "funding", "borrow", "coverage"}:
        raise ValueError("unsupported financing record kind")
    if kind == "funding":
        at = utc(data["settlement_time"])
        _number(data["funding_rate"], "funding rate")
        _number(data["mark_price"], "funding mark", positive=True)
    elif kind in {"borrow", "coverage"}:
        start, at = utc(data["start"]), utc(data["end"])
        if pd.isna(start) or pd.isna(at) or at <= start:
            raise ValueError("financing intervals must be increasing")
        if kind == "borrow":
            _number(data["annual_rate"], "borrow annual rate", nonnegative=True)
            _number(data["quote_conversion_price"], "borrow conversion", positive=True)
            _number(data["fee_quote_per_base_unit"], "borrow fees", nonnegative=True)
        elif (data.get("complete") is not True or not isinstance(data.get("event_ids"), list)
                or len(set(data["event_ids"])) != len(data["event_ids"])):
            raise ValueError("coverage requires complete=True and unique explicit event_ids")
    else:
        if utc(data["valid_until"]) <= utc(data["valid_from"]):
            raise ValueError("financing terms require an explicit validity interval")
        if kind == "linear_contract":
            LinearContractSpec(**data["spec"])
        else:
            _number(data["limit"], "borrow limit", nonnegative=True)
            fraction = _number(data["borrow_fraction"], "borrow fraction", nonnegative=True)
            if fraction > 1 or data["direction"] not in {"long", "short"}:
                raise ValueError("invalid borrow permission")
        return
    if at != utc(record["event_time"]) or (record["available_at"] is not None and utc(record["available_at"]) < at):
        raise ValueError("actual financing evidence cannot precede its event/coverage end")


def financing_record_identity(record):
    """同一事件的修订必须保留身份坐标；撤回也不能移到另一个事件时刻。"""
    data = record["data"]
    kind = data.get("kind")
    if record.get("record_type") == "tombstone":
        return utc(record["event_time"]).isoformat(), None
    names = {"funding": ("contract_id", "settlement_time"), "borrow": ("permission_id",),
        "coverage": ("account_mode", "symbol", "direction", "start", "end", "currency"),
        "borrow_permission": ("symbol", "direction", "base_currency", "quote_currency", "borrow_currency")}
    if kind == "linear_contract":
        terms = {name: data["spec"][name] for name in ("venue", "contract_id", "symbol",
            "base_currency", "quote_currency", "settlement_currency")}
    else:
        terms = {name: data[name] for name in names[kind]}
    for name in ("start", "end", "settlement_time"):
        if name in terms:
            terms[name] = utc(terms[name]).isoformat()
    return utc(record["event_time"]).isoformat(), fingerprint({"kind": kind, **terms})


class TemporalFinancing:
    """显式追加融资证据，按截止时间读取；同一修订身份不能对应不同内容。"""

    def __init__(self, dataset_id, records=(), *, store_path=None):
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("financing dataset identity required")
        self.dataset_id = dataset_id
        self.store = DataVersionStore(store_path) if store_path else None
        self._records, self._claims, self._snapshots = [], {}, []
        self._event_identities = {}
        if self.store:
            for path in sorted((self.store.root / "snapshots").glob("*.json")):
                manifest = json.loads(path.read_bytes())
                if (manifest.get("dataset_id") == dataset_id
                        and manifest.get("metadata", {}).get("financing_protocol") == FINANCING_PROTOCOL):
                    bundle = self.store.read_snapshot(path.stem)
                    self._accept(bundle["records"])
                    self._snapshots.append(path.stem)
        self.ingest(records)

    def _accept(self, records):
        staged = {}
        identities = dict(self._event_identities)
        for raw in records:
            record = _record(raw)
            _validate_record(record)
            event, coordinates = financing_record_identity(record)
            previous = identities.get(record["record_id"])
            if previous is not None and (event != previous[0] or
                    (coordinates is not None and previous[1] is not None and coordinates != previous[1])):
                raise ValueError("financing logical event identity changed across revisions")
            identities[record["record_id"]] = event, coordinates or (previous[1] if previous else None)
            key = record["record_id"], record["revision_id"]
            prior = staged.get(key, self._claims.get(key))
            if prior is not None and prior != record:
                raise ValueError("immutable financing revision changed")
            if key not in self._claims:
                staged[key] = record
        self._claims.update(staged)
        self._event_identities = identities
        self._records.extend(staged.values())
        return list(staged.values())

    def ingest(self, records):
        added = self._accept(records)
        if added and self.store:
            manifest = self.store.create_snapshot(self.dataset_id, added,
                metadata={"financing_protocol": FINANCING_PROTOCOL})
            self._snapshots.append(manifest["snapshot_id"])
        return self.identity

    @classmethod
    def from_json(cls, path, *, store_path=None):
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if payload.get("schema") != FINANCING_PROTOCOL:
            raise ValueError("unsupported financing evidence file")
        return cls(payload["dataset_id"], payload["records"], store_path=store_path)

    def export(self):
        return {"schema": FINANCING_PROTOCOL, "dataset_id": self.dataset_id,
                "records": deepcopy(sorted(self._records, key=lambda r: (r["record_id"], r["revision_id"])))}

    @property
    def identity(self):
        return {"schema": FINANCING_PROTOCOL, "dataset_id": self.dataset_id,
            "records_sha256": fingerprint(self.export()["records"]), "record_count": len(self._records),
            "snapshot_ids": list(self._snapshots), "store_path": str(self.store.root) if self.store else None,
            "calculator": FINANCING_CALCULATOR}

    def as_of(self, at, *, knowledge="local"):
        """选择截止时刻可见的最新版本，保留 tombstone 供成本/标签失效判断。"""
        if knowledge not in {"local", "published"}:
            raise ValueError("invalid financing knowledge policy")
        cutoff, selected, ranks = utc(at), {}, {}
        for record in self._records:
            if record["available_at"] is None or utc(record["available_at"]) > cutoff or utc(record["event_time"]) > cutoff:
                continue
            if ((knowledge == "local" and utc(record["observed_at"]) > cutoff)
                    or (knowledge == "published" and record["availability_evidence"]["kind"] != "source_publication")):
                continue
            key = record["record_id"]
            rank = utc(record["revision_at"] or record["available_at"]), utc(record["observed_at"])
            if key in ranks and rank == ranks[key] and selected[key] != record:
                raise ValueError("ambiguous financing revision ordering")
            if key not in ranks or rank > ranks[key]:
                selected[key], ranks[key] = record, rank
        return {key: {"dataset_id": self.dataset_id, "record_sha256": fingerprint(record),
                      "record": deepcopy(record)} for key, record in selected.items()}


def financing_cost(versions, *, candidate, account_mode, quantity, entry_price,
                   start, end, cutoff, knowledge):
    """验证完整持有窗口，返回 carry、逐笔成本及实际依赖的版本证据。

    缺失或已撤回的输入返回 complete=False，不能默认为零成本；身份、哈希或
    格式冲突直接报错。合约/借款许可须在决策时已知，结算事实可在窗口结束后
    到达，但不能晚于本次标签截止时间。
    """
    start, end, cutoff = utc(start), utc(end), utc(cutoff)
    symbol = candidate["symbol"]
    direction = candidate["direction"]
    decision = utc(candidate["context"]["available_at"])
    _number(quantity, "quantity", positive=True)
    _number(entry_price, "entry price", positive=True)
    records, envelopes, source_datasets = {}, {}, set()
    for envelope in versions.values() if isinstance(versions, dict) else versions:
        record = _record(envelope["record"])
        if fingerprint(record) != envelope.get("record_sha256"):
            raise ValueError("financing source hash mismatch")
        _validate_record(record)
        effective = utc(record["available_at"]) if record["available_at"] is not None else None
        if effective is None:
            raise ValueError("unknown financing source availability")
        if knowledge == "local":
            effective = max(effective, utc(record["observed_at"]))
        elif knowledge != "published" or record["availability_evidence"]["kind"] != "source_publication":
            raise ValueError("invalid financing knowledge evidence")
        if effective > cutoff or utc(record["event_time"]) > cutoff:
            raise ValueError("financing source unavailable at label cutoff")
        key = record["record_id"]
        if key in records and records[key] != record:
            raise ValueError("multiple financing versions supplied for one logical event")
        records[key], envelopes[key] = record, deepcopy(envelope)
        source_datasets.add(envelope["dataset_id"])
    if len(source_datasets) > 1:
        raise ValueError("financing evidence mixes source datasets")
    used = {}
    def take(key):
        if key in envelopes:
            used[key] = envelopes[key]
        record = records.get(key)
        return None if record is None or record.get("record_type") == "tombstone" else record
    def incomplete(reason):
        return {"complete": False, "reason": reason, "source_versions": list(envelopes.values()),
                "calculator": FINANCING_CALCULATOR}
    coverage = [r for r in records.values() if r.get("record_type") != "tombstone"
        and r["data"].get("kind") == "coverage" and r["data"].get("account_mode") == account_mode
        and normalize_symbol(r["data"].get("symbol", "")) == normalize_symbol(symbol)
        and r["data"].get("direction") == direction and utc(r["data"]["start"]) == start
        and utc(r["data"]["end"]) == end]
    if len(coverage) != 1:
        # Include withdrawals so loss of a previously complete proof produces a
        # new invalidating label version rather than retaining old mature value.
        used.update({key: envelope for key, envelope in envelopes.items()
                     if envelope["record"].get("record_type") == "tombstone"})
        return incomplete("missing_or_ambiguous_financing_coverage")
    coverage = take(coverage[0]["record_id"])["data"]
    event_records = []
    for key in coverage["event_ids"]:
        record = take(key)
        if record is None:
            return incomplete("missing_or_retracted_financing_event")
        event_records.append(record)
    if account_mode == "perpetual":
        contract = take(coverage.get("contract_record_id"))
        if contract is None or contract["data"].get("kind") != "linear_contract":
            return incomplete("missing_or_retracted_contract")
        terms = contract["data"]
        spec = LinearContractSpec(**terms["spec"])
        if normalize_symbol(spec.symbol) != normalize_symbol(symbol) or coverage["currency"] != spec.settlement_currency:
            raise ValueError("funding contract/symbol/currency mismatch")
        known = max(utc(contract["event_time"]), utc(contract["available_at"]))
        if knowledge == "local":
            known = max(known, utc(contract["observed_at"]))
        if known > decision or utc(terms["valid_from"]) > start or utc(terms["valid_until"]) < end:
            return incomplete("contract_terms_unavailable_or_outside_validity")
        settlements = []
        for record in event_records:
            data = record["data"]
            if data.get("kind") != "funding" or data["contract_id"] != spec.contract_id:
                raise ValueError("coverage lists an incompatible funding event")
            settlements.append({**data, "available_at": record["available_at"]})
        observed_ids = {r["record_id"] for r in records.values() if r.get("record_type") != "tombstone"
            and r["data"].get("kind") == "funding" and r["data"].get("contract_id") == spec.contract_id
            and start < utc(r["data"]["settlement_time"]) <= end}
        if observed_ids != set(coverage["event_ids"]):
            return incomplete("funding_coverage_omits_observed_events")
        positions = pd.DataFrame(columns=["timestamp", "position_contracts", "contract_id"])
        frame = pd.DataFrame(settlements, columns=["contract_id", "settlement_time", "available_at", "funding_rate", "mark_price"])
        try:
            replay = replay_funding_settlements(spec, positions, frame, start=start, end=end,
                initial_contracts=(1 if direction == "long" else -1)*quantity/spec.contract_multiplier)
        except ValueError as exc:
            if "missing funding settlements" in str(exc):
                return incomplete("missing_scheduled_funding_settlement")
            raise
        carry = sum(row["amount"] for row in replay["ledger"])
        ledger = replay["ledger"]
    elif account_mode == "spot_margin":
        permission = take(coverage.get("permission_record_id"))
        if permission is None or permission["data"].get("kind") != "borrow_permission":
            return incomplete("missing_or_retracted_borrow_permission")
        terms = permission["data"]
        if (normalize_symbol(terms["symbol"]) != normalize_symbol(symbol) or terms["direction"] != direction
                or coverage["currency"] != terms["quote_currency"]
                or normalize_symbol(symbol) != normalize_symbol(f'{terms["base_currency"]}/{terms["quote_currency"]}')):
            raise ValueError("borrow permission identity/currency mismatch")
        known = max(utc(permission["event_time"]), utc(permission["available_at"]))
        if knowledge == "local":
            known = max(known, utc(permission["observed_at"]))
        if known > decision or utc(terms["valid_from"]) > start or utc(terms["valid_until"]) < end:
            return incomplete("borrow_permission_unavailable_or_outside_validity")
        fraction = float(terms["borrow_fraction"])
        principal = quantity if direction == "short" else quantity*entry_price*fraction
        expected_currency = terms["base_currency"] if direction == "short" else terms["quote_currency"]
        if terms["borrow_currency"] != expected_currency or (direction == "short" and fraction != 1.):
            raise ValueError("unsupported borrow principal denomination")
        if principal > terms["limit"]:
            return incomplete("borrow_permission_limit_exceeded")
        cursor, carry, ledger = start, 0., []
        for record in sorted(event_records, key=lambda r: utc(r["data"]["start"])):
            data = record["data"]
            if data.get("kind") != "borrow" or data.get("permission_id") != permission["record_id"]:
                raise ValueError("coverage lists an incompatible borrow event")
            left, right = utc(data["start"]), utc(data["end"])
            if left != cursor or right > end:
                return incomplete("borrow_accrual_gap_overlap_or_partial_interval")
            if direction == "long" and data["quote_conversion_price"] != 1.:
                raise ValueError("quote borrow conversion must be one")
            amount = principal*data["annual_rate"]*(right-left).total_seconds()/(365*86400)*data["quote_conversion_price"]
            amount += quantity*data["fee_quote_per_base_unit"]
            ledger.append({"event_id": record["record_id"], "start": iso(left), "end": iso(right),
                "principal": principal, "borrow_currency": expected_currency, "amount": amount,
                "currency": terms["quote_currency"], "day_count": "ACT/365F", "compounding": "none"})
            carry += amount
            cursor = right
        if cursor != end:
            return incomplete("borrow_accrual_window_incomplete")
        observed_ids = {r["record_id"] for r in records.values() if r.get("record_type") != "tombstone"
            and r["data"].get("kind") == "borrow" and r["data"].get("permission_id") == permission["record_id"]
            and utc(r["data"]["start"]) < end and utc(r["data"]["end"]) > start}
        if observed_ids != set(coverage["event_ids"]):
            return incomplete("borrow_coverage_omits_observed_events")
    else:
        return incomplete("unsupported_financing_account")
    return {"complete": True, "carry": float(carry), "ledger": ledger,
        "currency": coverage["currency"], "source_versions": list(used.values()),
        "calculator": FINANCING_CALCULATOR,
        "source_versions_sha256": fingerprint(list(used.values()))}


def select_financing_versions(versions, *, candidate, account_mode, start, end):
    """筛出单个标签的相关证据；完整性声明外的已观察事件也保留，以检查漏报。

    调用方的 provider 负责版本校验；缺少完整性声明时仍传递撤回版本，
    使已有成熟标签能够失效，而不是继续沿用旧成本。
    """
    start, end = utc(start), utc(end)
    selected = {}
    for key, envelope in versions.items():
        record = envelope["record"]
        data = record["data"]
        if (record.get("record_type") != "tombstone" and data.get("kind") == "coverage"
                and data.get("account_mode") == account_mode and data.get("direction") == candidate["direction"]
                and normalize_symbol(data.get("symbol", "")) == normalize_symbol(candidate["symbol"])
                and utc(data["start"]) == start and utc(data["end"]) == end):
            selected[key] = envelope
    if not selected:
        return {key: value for key, value in versions.items() if value["record"].get("record_type") == "tombstone"}
    for envelope in list(selected.values()):
        data = envelope["record"]["data"]
        term_key = data.get("contract_record_id") if account_mode == "perpetual" else data.get("permission_record_id")
        for key in [term_key, *data["event_ids"]]:
            if key in versions:
                selected[key] = versions[key]
        term = versions.get(term_key, {}).get("record", {}).get("data", {})
        for key, source in versions.items():
            row = source["record"]["data"]
            if ((account_mode == "perpetual" and row.get("kind") == "funding"
                 and row.get("contract_id") == term.get("spec", {}).get("contract_id")
                 and start < utc(row["settlement_time"]) <= end)
                    or (account_mode == "spot_margin" and row.get("kind") == "borrow"
                        and row.get("permission_id") == term_key and utc(row["start"]) < end and utc(row["end"]) > start)):
                selected[key] = source
    return selected
