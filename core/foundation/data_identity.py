"""Versioned identities for new research data without changing legacy hashes.

Exact v2 binds ordered axes, per-column dtype and typed values, including full
nanosecond timestamps in both indices and cells. It is not a serialization format.
"""

from __future__ import annotations

from typing import Any

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from typing import Mapping

import numpy as np
import pandas as pd

from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_frame


LEGACY_DATA_IDENTITY = "legacy/v1"
EXACT_DATA_IDENTITY = "exact/v2"


def _typed(value, _active=None):
    if isinstance(value, (Mapping, tuple, list)):
        active = set() if _active is None else _active
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic metadata cannot be fingerprinted")
        active.add(marker)
        try:
            if isinstance(value, Mapping):
                return [
                    "mapping",
                    [[_typed(key, active), _typed(item, active)] for key, item in value.items()],
                ]
            return [
                "python.tuple" if isinstance(value, tuple) else "python.list",
                [_typed(item, active) for item in value],
            ]
        finally:
            active.remove(marker)
    if value is None:
        return ["none"]
    if value is pd.NA:
        return ["pandas.NA"]
    if value is pd.NaT:
        return ["pandas.NaT"]
    if isinstance(value, pd.Timestamp):
        return ["pandas.Timestamp", value.isoformat(), str(value.tzinfo), value.fold]
    if isinstance(value, pd.Timedelta):
        return ["pandas.Timedelta", value.isoformat()]
    if isinstance(value, pd.Period):
        return ["pandas.Period", value.ordinal, value.freqstr]
    if isinstance(value, np.generic):
        # Raw scalar bytes and their explicit dtype retain precision, units,
        # signed zero and datetime/timedelta units. No decimal rendering occurs.
        if value.dtype.hasobject:
            raise ValueError("object numpy scalar cannot be fingerprinted")
        return ["numpy.scalar", value.dtype.str, value.tobytes().hex()]
    if isinstance(value, bool):
        return ["python.bool", value]
    if isinstance(value, int):
        return ["python.int", str(value)]
    if isinstance(value, float):
        return ["python.float", value.hex()]
    if isinstance(value, str):
        return ["python.str", value]
    if isinstance(value, bytes):
        return ["python.bytes", value.hex()]
    if isinstance(value, datetime):
        return ["python.datetime", value.isoformat(), value.fold]
    if isinstance(value, date):
        return ["python.date", value.isoformat()]
    if isinstance(value, timedelta):
        return ["python.timedelta", value.days, value.seconds, value.microseconds]
    if isinstance(value, Decimal):
        return ["decimal.Decimal", str(value)]
    raise ValueError(
        f"unsupported exact data value type: {type(value).__module__}.{type(value).__qualname__}"
    )


def _dtype(dtype):
    result: dict[str, Any] = {
        "name": str(dtype),
        "class": type(dtype).__module__ + "." + type(dtype).__qualname__,
    }
    if isinstance(dtype, pd.CategoricalDtype):
        result.update(ordered=dtype.ordered, categories=[_typed(item) for item in dtype.categories])
    return result


def _axis(index):
    value = {
        "class": type(index).__module__ + "." + type(index).__qualname__,
        "dtype": _dtype(index.dtype),
        "names": [_typed(name) for name in index.names],
        "values": [_typed(item) for item in index],
    }
    if isinstance(index, pd.MultiIndex):
        value.update(
            levels=[_axis(level) for level in index.levels],
            codes=[code.tolist() for code in index.codes],
            sortorder=index.sortorder,
        )
    if isinstance(index, pd.RangeIndex):
        value["range"] = [index.start, index.stop, index.step]
    if isinstance(index, (pd.DatetimeIndex, pd.TimedeltaIndex)):
        value["frequency"] = index.freqstr
    return value


def sha256_frame_exact(frame):
    """Bind the unsorted frame and exact typed timestamp cells/metadata."""
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("a DataFrame is required")
    value = {
        "schema": "exact-pandas-dataframe/v2",
        "index": _axis(frame.index),
        "columns": _axis(frame.columns),
        "attrs": _typed(frame.attrs),
        "allows_duplicate_labels": frame.flags.allows_duplicate_labels,
        "data": [
            {
                "dtype": _dtype(frame.iloc[:, position].dtype),
                "values": [_typed(item) for item in frame.iloc[:, position].array],
            }
            for position in range(len(frame.columns))
        ],
    }
    return sha256_bytes(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    )


def sha256_series_exact(series):
    if not isinstance(series, pd.Series):
        raise ValueError("a Series is required")
    value = {
        "schema": "exact-pandas-series/v2",
        "name": _typed(series.name),
        "frame_sha256": sha256_frame_exact(series.to_frame("__series_value__")),
    }
    return sha256_bytes(canonical_json(value).encode())


def data_hashes(data_map, *, version=EXACT_DATA_IDENTITY):
    """Hash according to a stored version; plain finite JSON keeps legacy hashes.

    Legacy preserves all historical coercions. Exact mode refuses unsupported
    or nonfinite JSON rather than allowing a NaN/null or truncated-Series alias.
    """
    if not isinstance(version, str) or version not in {LEGACY_DATA_IDENTITY, EXACT_DATA_IDENTITY}:
        raise ValueError("unknown data identity version")
    if not isinstance(data_map, Mapping):
        raise ValueError("data_map must be a mapping")
    result = {}
    for key, value in data_map.items():
        if version == LEGACY_DATA_IDENTITY:
            result[key] = (
                sha256_frame(value)
                if isinstance(value, pd.DataFrame)
                else sha256_bytes(canonical_json(value).encode())
            )
            continue
        if not isinstance(key, str) or not key.strip():
            raise ValueError("nonempty string data key required")
        if isinstance(value, pd.DataFrame):
            result[key] = sha256_frame_exact(value)
        elif isinstance(value, pd.Series):
            result[key] = sha256_series_exact(value)
        else:
            # These are JSON inputs, not an implicit arbitrary-object repr.
            # Validate every leaf first; keep the existing canonical JSON digest.
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "finite JSON or explicit pandas input required in exact/v2"
                ) from exc
            result[key] = sha256_bytes(canonical_json(value).encode())
    return result
