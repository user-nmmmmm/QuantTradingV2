"""Read immutable label revisions at a training cutoff, without rewriting history.

Hashes detect changed evidence bytes; they do not authenticate a provider's
publication claim. Legacy retrospective observations remain a separate protocol.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
from types import SimpleNamespace

import pandas as pd

from core.data_versions import _record
from core.signal_observation_types import fingerprint
from core.temporal_financing import FINANCING_CALCULATOR, financing_cost
from core.timeframes import as_utc_timestamp, timeframe_delta


PROTOCOL = "strict-fixed-horizon/v1"


def _validate_financed_outcome(row, candidate, payload, protocol):
    """Recompute execution and carry from evidence, not self-reported amounts."""
    from core.signal_observation_types import (
        ContextSnapshot, ObservationPolicy, SignalCandidateEvent, canonical,
    )
    from core.signal_outcomes import ForwardOutcomeTracker, ObservationCosts

    sources = row["source_versions"]
    candidate_data = deepcopy(candidate)
    context = candidate_data.pop("context")
    context["features_json"] = canonical(context.pop("features"))
    candidate_data["signal_json"] = canonical(candidate_data.pop("signal"))
    event = SignalCandidateEvent(**candidate_data, context=ContextSnapshot(**context))
    delta = pd.Timedelta(timeframe_delta(event.context.timeframe))
    start = as_utc_timestamp(event.timestamp) + delta
    decision = as_utc_timestamp(event.context.available_at)
    if decision > start:
        start += math.ceil((decision-start)/delta)*delta
    event = replace(event, context=replace(event.context, available_at=start.isoformat()))
    policy = ObservationPolicy.from_mapping(payload["policy"])
    costs = replace(ObservationCosts(**payload["costs"]), account_mode="perpetual", funding_rate_required=False)
    calc = ForwardOutcomeTracker(replace(policy, horizons=(row["horizon_bars"],)), costs)
    calc.add(event)
    for source in sources:
        record = source["record"]
        at = as_utc_timestamp(record["event_time"])
        calc.advance(SimpleNamespace(timestamp=at, bars={event.symbol: pd.Series(record["data"], name=at)}))
    if len(calc.results) != 1 or calc.results[0]["status"] != "matured":
        raise ValueError("financed label requires a complete executable raw window")
    base = calc.results[0]
    end = start + row["horizon_bars"] * timeframe_delta(candidate["context"]["timeframe"])
    evidence = row.get("financing_versions")
    if not isinstance(evidence, list):
        raise ValueError("financed label requires explicit financing versions")
    finance = financing_cost(evidence, candidate=candidate, account_mode=row["account_mode"],
        quantity=base["quantity"], entry_price=base["entry_price"], start=start, end=end,
        cutoff=row["available_at"], knowledge=protocol["knowledge"])
    if row["status"] == "censored_financing_evidence_invalidated":
        if finance["complete"] or row.get("financing_reason") != finance["reason"]:
            raise ValueError("financing invalidation does not match its source evidence")
        return
    if (not finance["complete"] or row.get("financing_calculator") != FINANCING_CALCULATOR
            or protocol.get("financing_calculator") != FINANCING_CALCULATOR
            or row.get("financing_input_sha256") != finance["source_versions_sha256"]
            or row.get("financing_ledger") != finance["ledger"]
            or row.get("financing_currency") != finance["currency"]):
        raise ValueError("financing evidence/ledger/calculator is incomplete or inconsistent")
    expected = {field: base[field] for field in ("entry_reference", "exit_reference", "entry_price",
        "exit_price", "quantity", "gross_pnl", "commission", "slippage", "impact", "mae_bps", "mfe_bps")}
    expected.update(net_before_financing=base["net_pnl"], carry=finance["carry"],
        net_pnl=base["net_pnl"]-finance["carry"])
    expected["net_return_bps"] = expected["net_pnl"]/(base["quantity"]*base["entry_reference"])*10000
    if base.get("execution_flags", []) != row["execution_flags"]:
        raise ValueError("financed label execution flags differ from replay")
    for field, value in expected.items():
        actual = row.get(field)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isclose(actual, value, rel_tol=1e-12, abs_tol=1e-10):
            raise ValueError(f"financed label {field} differs from evidence replay")


class OutcomeRevisionBook:
    def __init__(self, payload, candidates, horizons, outcomes):
        self.versioned = payload.get("temporal_label_protocol") is not None
        self._latest = outcomes
        self._rows = {}
        strict = payload.get("temporal_scope", {}).get("decision_mode") == "strict"
        if not self.versioned:
            if strict or payload.get("outcome_revisions"):
                raise ValueError("strict training requires the versioned label protocol")
            return
        protocol = payload["temporal_label_protocol"]
        if protocol.get("schema") != PROTOCOL or protocol.get("knowledge") not in {"local", "published"}:
            raise ValueError("unsupported temporal label protocol")
        costs = payload.get("costs")
        if not isinstance(costs, dict) or protocol.get("cost_policy_sha256") != fingerprint(costs):
            raise ValueError("versioned label cost policy differs from the P0 snapshot")
        rows = payload.get("outcome_revisions")
        if not isinstance(rows, list):
            raise ValueError("versioned labels require an explicit revision list")
        identities = set()
        source_claims = {}
        financing_claims = {}
        source_heads = {}
        candidate_claims = {}
        for raw in rows:
            row = deepcopy(raw)
            cid, horizon = row["candidate_id"], row["horizon_bars"]
            if cid not in candidates or type(horizon) is not int or horizon not in horizons:
                raise ValueError("invalid label revision candidate or horizon")
            key = cid, horizon
            candidate = candidates[cid]
            digest = fingerprint({k: v for k, v in row.items() if k not in {"revision_id", "content_sha256"}})
            if row.get("content_sha256") != digest or row.get("revision_id") != "label_" + digest:
                raise ValueError("label revision content hash mismatch")
            if row["revision_id"] in identities:
                raise ValueError("duplicate label revision identity")
            identities.add(row["revision_id"])
            if row.get("label_protocol") != PROTOCOL or row.get("knowledge") != protocol["knowledge"]:
                raise ValueError("label revision protocol differs from its manifest")
            if row.get("cost_policy_sha256") != protocol["cost_policy_sha256"] or row.get("account_mode") != costs.get("account_mode"):
                raise ValueError("label revision cost or account contract differs from its snapshot")
            if row.get("scope") != "independent_fixed_notional_signal_diagnostic":
                raise ValueError("unsupported label revision scope")
            if any(row.get(name) != candidate[name] for name in ("symbol", "strategy", "direction")):
                raise ValueError("label revision identity differs from candidate")
            available = as_utc_timestamp(row["available_at"])
            observed = as_utc_timestamp(row["observed_at"])
            entry = as_utc_timestamp(candidate["context"]["available_at"])
            if any(value is None or value != value for value in (available, observed)) or available < max(observed, entry):
                raise ValueError("label availability precedes its observation or candidate")
            if type(row.get("training_eligible")) is not bool:
                raise ValueError("label revision must declare training eligibility")
            if not isinstance(row.get("execution_flags"), list):
                raise ValueError("label revision requires execution flags")
            sources = row.get("source_versions")
            if not isinstance(sources, list):
                raise ValueError("label revision requires source version evidence")
            retractions = row.get("retraction_versions", [])
            if not isinstance(retractions, list):
                raise ValueError("label retractions require version evidence")
            if row["status"] == "censored_source_retracted" and not retractions:
                raise ValueError("source-retracted label lacks withdrawal proof")
            source_times = []
            def check_head(source, namespace):
                record = source["record"]
                head_key = key, namespace, source["dataset_id"], record["record_id"]
                rank = (as_utc_timestamp(record.get("revision_at") or record["available_at"]),
                        as_utc_timestamp(record["observed_at"]))
                prior = source_heads.get(head_key)
                if prior is not None and (rank < prior[0] or (rank == prior[0] and source["record_sha256"] != prior[1])):
                    raise ValueError("label source revision regressed or fell back after withdrawal")
                source_heads[head_key] = rank, source["record_sha256"]
            for source in sources + retractions:
                record = source["record"]
                if fingerprint(record) != source.get("record_sha256"):
                    raise ValueError("label source content hash mismatch")
                normalized = _record(record)
                claim = source["dataset_id"], normalized["record_id"], normalized["revision_id"]
                if claim in source_claims and source_claims[claim] != source["record_sha256"]:
                    raise ValueError("conflicting immutable source revision")
                source_claims[claim] = source["record_sha256"]
                if normalized["available_at"] is None:
                    raise ValueError("unknown source availability cannot establish a label revision")
                source_available = as_utc_timestamp(normalized["available_at"])
                if protocol["knowledge"] == "local":
                    source_available = max(source_available, as_utc_timestamp(normalized["observed_at"]))
                elif normalized["availability_evidence"]["kind"] != "source_publication":
                    raise ValueError("published labels require source publication evidence")
                if source_available > available:
                    raise ValueError("label availability precedes its source version")
                check_head(source, "ohlcv")
                if source in retractions and normalized.get("record_type") != "tombstone":
                    raise ValueError("retraction proof must contain an explicit tombstone")
                if normalized.get("record_type") == "tombstone" and row["training_eligible"]:
                    raise ValueError("withdrawn source cannot train a label")
                if source in sources:
                    source_times.append(as_utc_timestamp(normalized["event_time"]))
            for source in row.get("financing_versions", []):
                record = _record(source["record"])
                digest = fingerprint(record)
                if source.get("record_sha256") != digest:
                    raise ValueError("financing source hash mismatch")
                claim = source["dataset_id"], record["record_id"], record["revision_id"]
                if claim in financing_claims and financing_claims[claim] != digest:
                    raise ValueError("conflicting immutable financing revision")
                financing_claims[claim] = digest
                check_head(source, "financing")
            if row["account_mode"] != "spot" and row["status"] in {"matured", "censored_financing_evidence_invalidated"}:
                _validate_financed_outcome(row, candidate, payload, protocol)
            if row["training_eligible"]:
                from core.signal_observation_types import finite
                delta = timeframe_delta(candidate["context"]["timeframe"])
                expected = [entry + i * delta for i in range(horizon)]
                if (row["status"] != "matured" or row["execution_flags"]
                        or row["account_mode"] not in {"spot", "spot_margin", "perpetual"}
                        or (candidate["direction"] != "long" and row["account_mode"] == "spot")
                        or entry != as_utc_timestamp(candidate["timestamp"])+delta):
                    raise ValueError("ineligible execution cannot train strict labels")
                candidate_source = row.get("candidate_source_version") or {}
                candidate_record = candidate_source.get("record")
                if not candidate_record or candidate_source.get("record_sha256") != fingerprint(candidate_record):
                    raise ValueError("strict label lacks its original candidate source proof")
                candidate_digest = fingerprint(candidate_source)
                if cid in candidate_claims and candidate_claims[cid] != candidate_digest:
                    raise ValueError("label revision changed the frozen candidate source")
                candidate_claims[cid] = candidate_digest
                if any(source["dataset_id"] != candidate_source.get("dataset_id") for source in sources):
                    raise ValueError("label window and candidate use different source datasets")
                candidate_record = _record(candidate_record)
                if (candidate_record.get("record_type") == "tombstone" or candidate_record["available_at"] is None
                        or as_utc_timestamp(candidate_record["event_time"]) != as_utc_timestamp(candidate["timestamp"])
                        or as_utc_timestamp(candidate_record["available_at"]) > entry
                        or (protocol["knowledge"] == "local" and as_utc_timestamp(candidate_record["observed_at"]) > entry)
                        or (protocol["knowledge"] == "published" and candidate_record["availability_evidence"]["kind"] != "source_publication")):
                    raise ValueError("candidate source was unavailable at the frozen decision")
                if sorted(source_times) != expected or available < entry + horizon * delta:
                    raise ValueError("strict label source window is incomplete or premature")
                if isinstance(row.get("net_return_bps"), bool) or finite(row.get("net_return_bps")) is None:
                    raise ValueError("nonfinite label revision")
            elif row["status"] != "matured" and not str(row["status"]).startswith("censored_"):
                raise ValueError("invalid label revision status")
            chain = self._rows.setdefault(key, [])
            if chain and available <= as_utc_timestamp(chain[-1]["available_at"]):
                raise ValueError("label revisions must have strictly increasing availability per identity")
            chain.append(row)
        for key, chain in self._rows.items():
            if outcomes[key] != chain[-1]:
                raise ValueError("current outcome differs from the last immutable revision")
        for key, row in outcomes.items():
            if row.get("training_eligible") or row.get("status") == "matured":
                if key not in self._rows:
                    raise ValueError("matured outcome has no immutable label revision")

    def as_of(self, cutoff):
        """Choose a version before cutoff, then apply eligibility (never reverse)."""
        before = as_utc_timestamp(cutoff)
        if not self.versioned:
            return {key: row for key, row in self._latest.items()
                    if row.get("available_at") and as_utc_timestamp(row["available_at"]) < before}
        result = {}
        for key, chain in self._rows.items():
            for row in reversed(chain):
                if as_utc_timestamp(row["available_at"]) < before:
                    result[key] = deepcopy(row)
                    break
        return result

    @staticmethod
    def eligible(row):
        return row["status"] == "matured" and row.get("training_eligible", True)

    def audit(self):
        return {"label_protocol": PROTOCOL if self.versioned else "retrospective_unversioned",
                "label_revision_count": sum(map(len, self._rows.values())),
                "version_selection": "latest strictly before each cutoff, then eligibility",
                "source_authentication": "documentary claims; hashes establish byte identity only"}
