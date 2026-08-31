"""Canonical exchange boundary for live order validation and CCXT adaptation.

Only the split modules imported below understand CCXT market metadata and
payload conventions.  The rest of the application consumes canonical
intents, orders, and positions through this facade (A4) — see
docs/architecture_review.md:

- core/exchange_metadata.py      — capabilities, market specs, metadata loading
- core/exchange_validation.py    — pre-submission order validation
- core/exchange_normalization.py — amount/price quantization to market increments
- core/exchange_ccxt_mapper.py   — canonical intent -> CCXT request kwargs
- core/exchange_parsers.py       — CCXT order/position payload -> canonical fact

``ExchangeBoundary`` (the orchestrator) and ``PreparedOrder`` stay here since
they compose all five split modules and did not have a single clean owner
among them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from core.domain import OrderIntent
from core.exchange_metadata import (
    DERIVATIVE_MARKET_TYPES,
    ExchangeBoundaryError,
    ExchangeCapabilities,
    MarketMetadataLoader,
    MarketSpecification,
    MetadataChangeAction,
    MetadataChangeHaltPolicy,
    MetadataChangedError,
    MetadataSnapshot,
    MetadataUnavailableError,
    OrderValidationError,
)
from core.exchange_validation import OrderValidator, ValidationResult
from core.exchange_normalization import OrderNormalizer
from core.exchange_ccxt_mapper import CCXTOrderRequest, CCXTRequestMapper
from core.exchange_parsers import CanonicalOrder, CanonicalPosition, OrderParser, PositionParser

__all__ = [
    "DERIVATIVE_MARKET_TYPES",
    "ExchangeBoundaryError",
    "OrderValidationError",
    "MetadataUnavailableError",
    "MetadataChangedError",
    "ExchangeCapabilities",
    "MarketSpecification",
    "MetadataSnapshot",
    "MetadataChangeAction",
    "MetadataChangeHaltPolicy",
    "MarketMetadataLoader",
    "ValidationResult",
    "OrderValidator",
    "OrderNormalizer",
    "CCXTOrderRequest",
    "CCXTRequestMapper",
    "CanonicalOrder",
    "OrderParser",
    "CanonicalPosition",
    "PositionParser",
    "PreparedOrder",
    "ExchangeBoundary",
]


@dataclass(frozen=True)
class PreparedOrder:
    intent: OrderIntent
    market: Optional[MarketSpecification]
    metadata_version: Optional[str]
    request: CCXTOrderRequest


class ExchangeBoundary:
    """Orchestrates metadata, validation, normalization, and request mapping."""

    def __init__(
        self,
        capabilities: ExchangeCapabilities,
        metadata: Optional[MarketMetadataLoader] = None,
        *,
        require_metadata: bool = True,
        validator: Optional[OrderValidator] = None,
        normalizer: Optional[OrderNormalizer] = None,
        request_mapper: Optional[CCXTRequestMapper] = None,
    ) -> None:
        self.capabilities = capabilities
        self.metadata = metadata
        self.require_metadata = require_metadata
        self.validator = validator or OrderValidator()
        self.normalizer = normalizer or OrderNormalizer()
        self.request_mapper = request_mapper or CCXTRequestMapper()
        self.order_parser = OrderParser()
        self.position_parser = PositionParser()

    def prepare(self, intent: OrderIntent, *, reference_price: Any = None) -> PreparedOrder:
        # Generic validation always happens before metadata I/O.
        self.validator.validate(intent, self.capabilities, None, reference_price=reference_price)
        snapshot: Optional[MetadataSnapshot] = None
        market: Optional[MarketSpecification] = None
        if self.metadata is not None:
            try:
                snapshot = self.metadata.load()
                market = snapshot.market(intent.symbol)
            except MetadataUnavailableError:
                if self.require_metadata:
                    raise
        elif self.require_metadata:
            raise MetadataUnavailableError("market metadata loader is not configured")
        normalized = self.normalizer.normalize(intent, market)
        # Validate after rounding because normalization can cross a venue limit.
        self.validator.validate(normalized, self.capabilities, market, reference_price=reference_price)
        return PreparedOrder(
            normalized,
            market,
            None if snapshot is None else snapshot.version,
            self.request_mapper.map(normalized, self.capabilities),
        )
