"""Type-preserving JSON encode/decode for event payload values.

Split out of core/events.py (A4) — see docs/architecture_review.md.
Depends on core.events.types for the payload/value types it round-trips.
"""
from __future__ import annotations

import importlib
import json
import math
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Type, Union
from uuid import UUID

from core.events.types import StructuredPayload, _aware_utc, _normalize_value


def _qualified_name(value: Union[Type[Any], Any]) -> str:
    cls = value if isinstance(value, type) else type(value)
    return f"{cls.__module__}:{cls.__qualname__}"


def _resolve_type(name: str) -> Type[Any]:
    if not isinstance(name, str) or ":" not in name:
        raise ValueError("Invalid qualified type name")
    module_name, qualname = name.split(":", 1)
    if not module_name or not qualname or "<locals>" in qualname:
        raise ValueError(f"Unsupported qualified type: {name!r}")
    value: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        value = getattr(value, part)
    if not isinstance(value, type):
        raise TypeError(f"Resolved value is not a type: {name!r}")
    return value


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN and Infinity are not valid event values")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal NaN and Infinity are not valid event values")
        return {"__qt_type__": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        utc_value = _aware_utc(value)
        return {"__qt_type__": "datetime", "value": utc_value.isoformat().replace("+00:00", "Z")}
    if isinstance(value, UUID):
        return {"__qt_type__": "uuid", "value": str(value)}
    if isinstance(value, Enum):
        return {
            "__qt_type__": "enum",
            "class": _qualified_name(value),
            "value": _encode_value(value.value),
        }
    if isinstance(value, StructuredPayload):
        return {
            "__qt_type__": "structured_payload",
            "class": _qualified_name(value),
            "data": _encode_value(value.data),
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__qt_type__": "dataclass",
            "class": _qualified_name(value),
            "fields": {
                item.name: _encode_value(getattr(value, item.name))
                for item in fields(value)
            },
        }
    if isinstance(value, Mapping):
        items = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("Event mapping keys must be strings")
            items.append([key, _encode_value(value[key])])
        return {"__qt_type__": "mapping", "items": items}
    if isinstance(value, tuple):
        return {"__qt_type__": "tuple", "items": [_encode_value(item) for item in value]}
    if isinstance(value, list):
        return {"__qt_type__": "list", "items": [_encode_value(item) for item in value]}
    raise TypeError(f"Unsupported event value type: {type(value).__name__}")


def _decode_value(value: Any) -> Any:
    if not isinstance(value, dict) or "__qt_type__" not in value:
        if isinstance(value, list):
            return [_decode_value(item) for item in value]
        return value
    tag = value.get("__qt_type__")
    if tag == "decimal":
        result = Decimal(value["value"])
        if not result.is_finite():
            raise ValueError("Decimal NaN and Infinity are not valid event values")
        return result
    if tag == "datetime":
        raw = value["value"]
        if not isinstance(raw, str):
            raise TypeError("Encoded datetime must be a string")
        return _aware_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    if tag == "uuid":
        return UUID(value["value"])
    if tag == "enum":
        cls = _resolve_type(value["class"])
        if not issubclass(cls, Enum):
            raise TypeError("Encoded enum class is not an Enum")
        return cls(_decode_value(value["value"]))
    if tag == "mapping":
        result: Dict[str, Any] = {}
        for item in value.get("items", ()):
            if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
                raise ValueError("Invalid encoded mapping item")
            if item[0] in result:
                raise ValueError(f"Duplicate encoded mapping key: {item[0]}")
            result[item[0]] = _decode_value(item[1])
        return result
    if tag in {"tuple", "list"}:
        decoded = [_decode_value(item) for item in value.get("items", ())]
        return tuple(decoded) if tag == "tuple" else decoded
    if tag == "structured_payload":
        cls = _resolve_type(value["class"])
        if not issubclass(cls, StructuredPayload):
            raise TypeError("Encoded payload class is not StructuredPayload")
        data = _decode_value(value["data"])
        return cls(data)
    if tag == "dataclass":
        cls = _resolve_type(value["class"])
        if not is_dataclass(cls):
            raise TypeError("Encoded class is not a dataclass")
        raw_fields = value.get("fields")
        if not isinstance(raw_fields, dict):
            raise TypeError("Encoded dataclass fields must be an object")
        return cls(**{key: _decode_value(item) for key, item in raw_fields.items()})
    raise ValueError(f"Unknown encoded event value type: {tag!r}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _encode_value(_normalize_value(value)),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
