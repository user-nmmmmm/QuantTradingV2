"""Typed, JSON-safe metric contract that distinguishes zero from unavailable."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Literal, Optional

MetricStatus = Literal["ok", "undefined", "insufficient", "not_modeled"]


@dataclass(frozen=True)
class MetricResult:
    name: str
    value: Optional[float]
    status: MetricStatus = "ok"
    sample_size: int = 0
    unit: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.sample_size < 0:
            raise ValueError("sample_size cannot be negative")
        if self.status == "ok":
            if self.value is None or not math.isfinite(float(self.value)):
                raise ValueError("ok metrics require a finite value")
        elif self.value is not None:
            raise ValueError("non-ok metrics must use value=None")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "required": ["name", "value", "status", "sample_size", "unit", "reason"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "value": {"type": ["number", "null"]},
                "status": {"enum": ["ok", "undefined", "insufficient", "not_modeled"]},
                "sample_size": {"type": "integer", "minimum": 0},
                "unit": {"type": ["string", "null"]},
                "reason": {"type": ["string", "null"]},
            },
        }
