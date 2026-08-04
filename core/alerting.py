"""Minimal structured alerting port for live-trading safety events."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Protocol


class AlertSink(Protocol):
    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        """Publish a structured operational event."""


class LoggingAlertSink:
    """Default alert sink that writes structured JSON through standard logging."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger

    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        log_method = getattr(self.logger, level.lower(), self.logger.error)
        log_method(
            "operational_alert event=%s context=%s",
            event,
            json.dumps(context, sort_keys=True, default=str),
        )
