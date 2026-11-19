"""Authenticated local publication of cost artifacts, separate from venue evidence.

The signing key and authority identity are explicit trust configuration. A
receipt proves what this local authority froze and when its API released it;
it does not authenticate a venue, synchronize the host clock or prove accuracy.
No caller-supplied timestamp can backdate a freeze or release.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import importlib
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import numpy as np
import pandas as pd

from core.planning.execution_cost_provider import aware_time, cost_digest, strict_json


_RUNTIME_MODULES = (
    "core.planning.execution_cost_provider",
    "analysis.execution_cost_research",
    "analysis.execution_cost_publication",
    "analysis.execution_calibration",
    "core.market.quote_observations",
    "core.metrics.execution",
)
_FIT_DEFAULTS = {
    "method": "ridge",
    "feature_names": ["spread_bps", "volatility", "requested_notional"],
    "constant_bps": 20.0,
    "ridge_alpha": 1.0,
    "interval_coverage": 0.95,
    "minimum_train_days": 2,
    "minimum_stratum_samples": 3,
    "horizon_days": 7,
    "anchor": "decision_mid",
}


def _now():
    # Deliberately no public clock injection or published_at override.
    return pd.Timestamp(datetime.now(timezone.utc))


def _runtime():
    sources = {}
    for name in _RUNTIME_MODULES:
        raw = Path(importlib.import_module(name).__file__).read_bytes()
        sources[name] = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes_base64": base64.b64encode(raw).decode("ascii"),
        }
    return {
        "sources": sources,
        "python": list(sys.version_info[:3]),
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }


def _check_runtime(frozen):
    current = _runtime()
    if current != frozen:
        raise ValueError("publication_runtime_or_source_changed")
    for value in frozen["sources"].values():
        raw = base64.b64decode(value["bytes_base64"], validate=True)
        if hashlib.sha256(raw).hexdigest() != value["sha256"]:
            raise ValueError("publication_source_snapshot_hash_mismatch")


def _credentials(authority_id, key):
    if not isinstance(authority_id, str) or not authority_id.strip():
        raise ValueError("explicit publication authority identity required")
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("independently configured publication key of at least 32 bytes required")
    return authority_id, key


def _artifact(record, authority_id):
    payload = record["payload"]
    result = {
        "schema": "published-execution-cost-model/v2",
        "fitted_model": deepcopy(payload["fitted_model"]),
        "available_at": record["observed_at"],
        "expires_at": payload["fitted_model"]["expires_at"],
        "publication": {
            "authority_id": authority_id,
            "record_id": record["record_id"],
            "record_sha256": record["record_sha256"],
        },
        "publication_semantics": "actual_local_authority_release_not_external_publication",
        "training_evidence": payload["training_evidence"],
        "real_venue_calibration": False,
        "production_approved": False,
    }
    result["model_sha256"] = cost_digest(result)
    return result


class LocalExecutionCostPublicationVerifier:
    """Read-only resolver pinned to an independently configured authority/key.

    Never construct trust configuration from a model, prediction, or context.
    Local administrators holding the signing key and host clock are trusted;
    host compromise, clock attestation and external venue authentication remain
    outside this adapter's claim.
    """

    def __init__(self, path, *, authority_id, verification_key):
        self.path = Path(path).resolve()
        self.authority_id, self._key = _credentials(authority_id, verification_key)
        if not self.path.is_file():
            raise ValueError("publication journal missing")
        self._verified_models = {}
        self._records()

    def _records(self):
        uri = self.path.as_uri() + "?mode=ro"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as con:
                rows = con.execute(
                    "SELECT sequence, envelope FROM cost_publications ORDER BY sequence"
                ).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("publication_journal_unreadable") from exc
        result, previous, last_at = {}, None, None
        for expected_sequence, (sequence, raw) in enumerate(rows, 1):
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise ValueError("publication_record_must_be_object")
            signature = record.pop("signature", None)
            claimed = record.pop("record_sha256", None)
            digest = cost_digest(record)
            expected_signature = hmac.new(
                self._key, digest.encode("ascii"), hashlib.sha256
            ).hexdigest()
            if not isinstance(signature, str) or not hmac.compare_digest(
                signature, expected_signature
            ):
                raise ValueError("publication_signature_mismatch")
            if (
                claimed != digest
                or sequence != expected_sequence
                or record.get("sequence") != sequence
            ):
                raise ValueError("publication_record_identity_mismatch")
            if (
                record.get("authority_id") != self.authority_id
                or record.get("previous_sha256") != previous
            ):
                raise ValueError("publication_authority_or_chain_mismatch")
            observed = aware_time(record["observed_at"])
            if last_at is not None and observed < last_at:
                raise ValueError("publication_clock_regression")
            if record["record_id"] in result:
                raise ValueError("duplicate_publication_record")
            record.update(record_sha256=digest, signature=signature)
            result[record["record_id"]] = record
            previous, last_at = digest, observed
        return result

    def resolve_features(self, observation, *, as_of):
        reference = dict(observation)
        if (
            set(reference) != {"authority_id", "record_id", "record_sha256"}
            or reference["authority_id"] != self.authority_id
        ):
            raise ValueError("untrusted_feature_observation_authority")
        record = self._records().get(reference["record_id"])
        if (
            record is None
            or record["kind"] != "feature_observation"
            or record["record_sha256"] != reference["record_sha256"]
        ):
            raise ValueError("feature_observation_receipt_missing_or_changed")
        if aware_time(record["observed_at"]) > aware_time(as_of):
            raise ValueError("features_not_locally_observed_at_decision")
        return {
            "observed_at": record["observed_at"],
            "inputs": deepcopy(record["payload"]["inputs"]),
            "source_payloads_sha256": cost_digest(record["payload"]["source_payloads"]),
        }

    def resolve_model(self, publication, *, as_of, information_policy=None):
        """Return the exact authenticated artifact or fail; published != local."""
        if (
            information_policy is not None
            and information_policy.get("knowledge", "local") != "local"
        ):
            raise ValueError("external_published_model_evidence_unavailable")
        reference = dict(publication)
        if (
            set(reference) != {"authority_id", "record_id", "record_sha256"}
            or reference["authority_id"] != self.authority_id
        ):
            raise ValueError("untrusted_publication_authority")
        records = self._records()
        release = records.get(reference["record_id"])
        if (
            release is None
            or release["kind"] != "model_release"
            or release["record_sha256"] != reference["record_sha256"]
        ):
            raise ValueError("publication_receipt_missing_or_changed")
        at, released_at = aware_time(as_of), aware_time(release["observed_at"])
        if released_at > at:
            raise ValueError("model_unavailable_at_decision")
        payload = release["payload"]
        training = records.get(payload["training_record_id"])
        if (
            training is None
            or training["kind"] != "training_freeze"
            or training["sequence"] >= release["sequence"]
        ):
            raise ValueError("training_freeze_receipt_missing")
        if training["record_sha256"] != payload["training_record_sha256"]:
            raise ValueError("training_receipt_changed")
        frozen = training["payload"]
        _check_runtime(frozen["runtime"])
        model = payload["fitted_model"]
        cutoff = aware_time(model["training_cutoff"])
        if not aware_time(training["observed_at"]) <= cutoff <= released_at:
            raise ValueError("training_not_locally_mature_before_release")
        if aware_time(model["expires_at"]) <= at:
            raise ValueError("expired_model")
        # Recompute from the authenticated snapshot, rather than trusting a
        # caller's training hash or evidence_verification_level assertion.
        if release["record_sha256"] not in self._verified_models:
            from analysis.execution_cost_research import fit_frozen_cost_model

            for row in frozen["samples"]:
                if aware_time(row["target_available_at"]) > aware_time(training["observed_at"]):
                    raise ValueError("training_target_not_mature_at_local_freeze")
            rebuilt = fit_frozen_cost_model(
                frozen["samples"],
                **frozen["fit_parameters"],
                training_cutoff=model["training_cutoff"],
                expires_at=model["expires_at"],
                protocol_sha256=cost_digest(frozen["protocol"]),
            )
            if rebuilt != model:
                raise ValueError("published_model_does_not_match_frozen_training")
            expected_evidence = _training_evidence(frozen["samples"])
            if payload["training_evidence"] != expected_evidence:
                raise ValueError("publication_training_evidence_changed")
            self._verified_models[release["record_sha256"]] = True
        return _artifact(release, self.authority_id)


def _training_evidence(samples):
    levels = sorted({row["evidence_verification_level"] for row in samples})
    return {
        "asserted_sample_levels": levels,
        "authentication": "local_snapshot_only_not_external_venue_authentication",
        "real_venue_calibration": False,
        "production_approved": False,
    }


class LocalExecutionCostPublicationAuthority:
    """Single-writer local authority; keep its key outside artifacts/journal."""

    def __init__(self, path, *, authority_id, signing_key):
        self.path = Path(path).resolve()
        self.authority_id, self._key = _credentials(authority_id, signing_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS cost_publications "
                "(sequence INTEGER PRIMARY KEY, envelope TEXT NOT NULL)"
            )
        self.verifier()._records()

    def verifier(self):
        return LocalExecutionCostPublicationVerifier(
            self.path, authority_id=self.authority_id, verification_key=self._key
        )

    def _append(self, kind, payload, *, not_before=None):
        # BEGIN IMMEDIATE serializes concurrent writers; sign the sequence and
        # previous hash inside the same transaction that persists the receipt.
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            records = self.verifier()._records()
            last = next(reversed(records.values())) if records else None
            observed = _now()
            if last is not None and observed < aware_time(last["observed_at"]):
                raise ValueError("publication_clock_regression")
            if not_before is not None and observed < aware_time(not_before):
                raise ValueError("publication_clock_regression")
            if (
                kind == "model_release"
                and aware_time(payload["fitted_model"]["expires_at"]) <= observed
            ):
                raise ValueError("publication_model_already_expired")
            record = {
                "schema": "local-cost-publication-receipt/v2",
                "authority_id": self.authority_id,
                "sequence": len(records) + 1,
                "record_id": uuid.uuid4().hex,
                "kind": kind,
                "observed_at": observed.isoformat(),
                "previous_sha256": last["record_sha256"] if last else None,
                "payload": strict_json(payload),
            }
            digest = cost_digest(record)
            record.update(
                record_sha256=digest,
                signature=hmac.new(self._key, digest.encode("ascii"), hashlib.sha256).hexdigest(),
            )
            con.execute(
                "INSERT INTO cost_publications VALUES (?, ?)",
                (record["sequence"], json.dumps(record, sort_keys=True, allow_nan=False)),
            )
        return record

    def observe_features(self, inputs, *, source_payloads=None):
        """Observe an actual local input snapshot, without a caller time override.

        This authenticates local observation only. Source payloads are frozen
        bytes, rather than an assertion that a venue authenticated their truth.
        Direction/horizon are scenario choices, so a snapshot may serve both
        buy and sell estimates of the same known features.
        """
        from core.planning.execution_cost_provider import _number

        fields = {"symbol", "market_identity", "features"}
        if set(inputs) != fields or any(
            not isinstance(inputs.get(k), str) or not inputs[k].strip()
            for k in ("symbol", "market_identity")
        ):
            raise ValueError("explicit instrument, market and feature inputs required")
        features = inputs["features"]
        if (
            not isinstance(features, dict)
            or not features
            or any(not isinstance(k, str) or not k for k in features)
        ):
            raise ValueError("nonempty named numeric feature inputs required")
        normalized = {
            **dict(inputs),
            "features": {name: _number(value) for name, value in features.items()},
        }
        record = self._append(
            "feature_observation",
            {
                "inputs": normalized,
                "source_payloads": strict_json(
                    source_payloads if source_payloads is not None else {}
                ),
            },
        )
        return {
            "authority_id": self.authority_id,
            "record_id": record["record_id"],
            "record_sha256": record["record_sha256"],
        }

    def freeze_training(self, samples, *, protocol, fit_parameters=None, source_payloads=None):
        """Actually observe a local snapshot now; old event dates stay old."""
        from analysis.execution_cost_research import _validated_samples

        rows = _validated_samples(samples)
        if not rows or not isinstance(protocol, dict) or not protocol:
            raise ValueError("nonempty training samples and fixed protocol required")
        parameters = deepcopy(_FIT_DEFAULTS)
        supplied = strict_json(fit_parameters or {})
        if set(supplied) - set(parameters):
            raise ValueError(
                "publication fit_parameters contain unsupported or caller-controlled clocks"
            )
        parameters.update(supplied)
        observed = _now()
        if any(aware_time(row["target_available_at"]) > observed for row in rows):
            raise ValueError("training_target_not_mature_at_local_freeze")
        payload = {
            "samples": rows,
            "protocol": strict_json(protocol),
            "fit_parameters": parameters,
            "runtime": _runtime(),
            "source_payloads": strict_json(source_payloads if source_payloads is not None else {}),
            "training_evidence": _training_evidence(rows),
        }
        record = self._append("training_freeze", payload, not_before=observed)
        return {
            "authority_id": self.authority_id,
            "record_id": record["record_id"],
            "record_sha256": record["record_sha256"],
            "observed_at": record["observed_at"],
        }

    def publish(self, training_receipt, *, expires_at):
        """Fit the frozen inputs and release now; no pre-existing model input."""
        from analysis.execution_cost_research import fit_frozen_cost_model

        records = self.verifier()._records()
        training = records.get(training_receipt.get("record_id"))
        if (
            training is None
            or training["kind"] != "training_freeze"
            or training_receipt.get("authority_id") != self.authority_id
            or training_receipt.get("record_sha256") != training["record_sha256"]
            or training_receipt.get("observed_at") != training["observed_at"]
        ):
            raise ValueError("training_freeze_receipt_missing_or_changed")
        frozen = training["payload"]
        _check_runtime(frozen["runtime"])
        cutoff = _now()
        if cutoff < aware_time(training["observed_at"]):
            raise ValueError("publication_clock_regression")
        model = fit_frozen_cost_model(
            frozen["samples"],
            **frozen["fit_parameters"],
            training_cutoff=cutoff.isoformat(),
            expires_at=expires_at,
            protocol_sha256=cost_digest(frozen["protocol"]),
        )
        if aware_time(expires_at) <= _now():
            raise ValueError("publication_model_already_expired")
        payload = {
            "training_record_id": training["record_id"],
            "training_record_sha256": training["record_sha256"],
            "fitted_model": model,
            "training_evidence": _training_evidence(frozen["samples"]),
        }
        record = self._append("model_release", payload, not_before=cutoff)
        return _artifact(record, self.authority_id)
