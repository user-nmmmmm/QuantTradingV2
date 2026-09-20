"""Versioned strict JSON shared by regular and research report entrypoints."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd


def metrics_document(metrics: Mapping[str, Any], metadata=None) -> dict:
    nonfinite = {}

    def clean(value, path):
        if value is pd.NA or value is pd.NaT:
            nonfinite[path] = {"status": "insufficient_data", "reason": "missing pandas value"}
            return None
        if is_dataclass(value) and not isinstance(value, type):
            value = asdict(value)
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, Decimal):
            value = float(value)
        if isinstance(value, Enum):
            return clean(value.value, path)
        if isinstance(value, float) and not math.isfinite(value):
            nonfinite[path] = {"status": "undefined", "reason": "non-finite legacy metric"}
            return None
        if isinstance(value, Mapping):
            return {str(k): clean(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, (list, tuple, np.ndarray)):
            return [clean(v, f"{path}[{i}]") for i, v in enumerate(value)]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Path):
            return str(value)
        return value

    cleaned = clean(dict(metrics), "metrics")
    results = []
    for raw in cleaned.get("MetricResults", []):
        row = dict(raw)
        if row.get("status") == "insufficient":
            row["status"] = "insufficient_data"
        results.append(row)
    return {"schema_version": "quanttrading.metrics/v1",
            "metric_status_version": "metric-result/v2",
            "metrics": cleaned, "metric_results": results,
            "run_identity": clean(metadata or {}, "run_identity"),
            "nonfinite_values": nonfinite}


def write_metrics_json(path: str | Path, metrics: Mapping[str, Any], metadata=None) -> dict:
    document = metrics_document(metrics, metadata)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                      encoding="utf-8")
    return document
