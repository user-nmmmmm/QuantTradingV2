"""Minimal structured alerting port for live-trading safety events."""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Protocol


class AlertSink(Protocol):
    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        """Publish a structured operational event."""

    def trigger(self, level: str, event: str, context: Dict[str, Any]) -> bool:
        """Trigger an alert and return whether a notification was delivered."""

    def ack(self, event: str, context: Optional[Dict[str, Any]] = None) -> int:
        """Acknowledge matching active alerts."""


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

class JsonlAlertSink:
    """Durable local alert trail, independent from log rotation/configuration."""

    def __init__(self, path: str = "reports/live_alerts.jsonl") -> None:
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level.lower(),
            "event": event,
            "context": context,
        }
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())


class WebhookAlertSink:
    """Minimal JSON webhook sink for external operational alert delivery."""

    def __init__(self, url: str, timeout_seconds: float = 5.0) -> None:
        if not url:
            raise ValueError("webhook URL is required")
        self.url = url
        self.timeout_seconds = timeout_seconds

    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        payload = json.dumps(
            {"level": level.lower(), "event": event, "context": context},
            default=str,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            response.read()


class TelegramAlertSink:
    """Telegram Bot API sink for real-time critical-event notifications."""

    def __init__(
        self, bot_token: str, chat_id: str, *, timeout_seconds: float = 5.0,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        if not chat_id:
            raise ValueError("chat_id is required")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds

    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        send_telegram_message(
            self.bot_token, self.chat_id, format_telegram_alert(level, event, context),
            timeout_seconds=self.timeout_seconds,
        )


def format_telegram_alert(level: str, event: str, context: Dict[str, Any]) -> str:
    lines = [f"[{level.upper()}] {event}"]
    for key, value in sorted(context.items()):
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def send_telegram_message(
    bot_token: str, chat_id: str, text: str, *, timeout_seconds: float = 5.0,
) -> None:
    """POST a plain-text message to a Telegram chat via the Bot API.

    No parse_mode is set: the message is sent as literal text, so alert
    context containing Markdown/HTML-special characters (e.g. "_", "<")
    cannot break formatting or be misinterpreted as markup.
    """
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response.read()


class CompositeAlertSink:
    """Fan out alerts without allowing one failed destination to block others."""

    def __init__(self, sinks: Iterable[AlertSink], logger: logging.Logger) -> None:
        self.sinks = tuple(sinks)
        self.logger = logger

    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        for sink in self.sinks:
            try:
                sink.notify(level, event, context)
            except Exception as exc:
                self.logger.error(
                    "alert_delivery_failed sink=%s event=%s category=%s",
                    type(sink).__name__, event, type(exc).__name__,
                )



class AlertState(str, Enum):
    ACKNOWLEDGED = "acknowledged"
    TRIGGERED = "triggered"
    SUPPRESSED = "suppressed"


@dataclass
class _AlertIncident:
    level: str
    event: str
    context: Dict[str, Any]
    state: AlertState = AlertState.TRIGGERED
    suppressed_count: int = 0
    reported_count: int = 0


class HysteresisAlertSink:
    """De-duplicate repeated alerts through trigger/suppress/ack states."""

    _VOLATILE_KEYS = {
        "timestamp", "assessed_at", "observed_at", "last_update",
        "poll", "retry_attempts",
    }

    def __init__(self, sink: AlertSink, *, summary_every: int = 9) -> None:
        if summary_every <= 0:
            raise ValueError("summary_every must be positive")
        self.sink = sink
        self.summary_every = summary_every
        self._incidents: Dict[str, _AlertIncident] = {}
        self._lock = threading.RLock()

    @classmethod
    def _stable_context(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._stable_context(item)
                for key, item in sorted(value.items())
                if key not in cls._VOLATILE_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [cls._stable_context(item) for item in value]
        return value

    @classmethod
    def _key(cls, event: str, context: Dict[str, Any]) -> str:
        stable = json.dumps(
            cls._stable_context(context),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"{event}:{stable}"

    def notify(self, level: str, event: str, context: Dict[str, Any]) -> None:
        self.trigger(level, event, context)

    def trigger(self, level: str, event: str, context: Dict[str, Any]) -> bool:
        key = self._key(event, context)
        with self._lock:
            incident = self._incidents.get(key)
            if incident is None or incident.state == AlertState.ACKNOWLEDGED:
                self._incidents[key] = _AlertIncident(level, event, dict(context))
                self.sink.notify(level, event, context)
                return True
            return self._suppress_locked(incident)

    def suppress(self, event: str, context: Dict[str, Any]) -> bool:
        key = self._key(event, context)
        with self._lock:
            incident = self._incidents.get(key)
            if incident is None or incident.state == AlertState.ACKNOWLEDGED:
                return False
            return self._suppress_locked(incident)

    def _suppress_locked(self, incident: _AlertIncident) -> bool:
        incident.state = AlertState.SUPPRESSED
        incident.suppressed_count += 1
        pending = incident.suppressed_count - incident.reported_count
        if pending >= self.summary_every:
            self._emit_summary(incident, pending, acknowledged=False)
            incident.reported_count = incident.suppressed_count
            return True
        return False

    def _emit_summary(
        self, incident: _AlertIncident, count: int, *, acknowledged: bool,
    ) -> None:
        self.sink.notify(incident.level, "alert_suppression_summary", {
            "original_event": incident.event,
            "suppressed_count": count,
            "total_suppressed": incident.suppressed_count,
            "acknowledged": acknowledged,
            "original_context": incident.context,
        })

    def ack(self, event: str, context: Optional[Dict[str, Any]] = None) -> int:
        with self._lock:
            if context is None:
                matches = [
                    incident for incident in self._incidents.values()
                    if incident.event == event
                    and incident.state != AlertState.ACKNOWLEDGED
                ]
            else:
                incident = self._incidents.get(self._key(event, context))
                matches = (
                    [incident]
                    if incident is not None
                    and incident.state != AlertState.ACKNOWLEDGED
                    else []
                )
            for incident in matches:
                pending = incident.suppressed_count - incident.reported_count
                if pending:
                    self._emit_summary(incident, pending, acknowledged=True)
                    incident.reported_count = incident.suppressed_count
                incident.state = AlertState.ACKNOWLEDGED
            return len(matches)

    def state(self, event: str, context: Dict[str, Any]) -> AlertState:
        with self._lock:
            incident = self._incidents.get(self._key(event, context))
            return incident.state if incident else AlertState.ACKNOWLEDGED

def build_default_alert_sink(
    logger: logging.Logger, *, record_path: str = "reports/live_alerts.jsonl",
) -> AlertSink:
    """Build a durable local sink plus optional external webhook/Telegram sinks."""
    sinks = [LoggingAlertSink(logger), JsonlAlertSink(record_path)]
    webhook_url = os.getenv("LIVE_ALERT_WEBHOOK_URL", "").strip()
    if webhook_url:
        sinks.append(WebhookAlertSink(webhook_url))
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if bot_token and chat_id:
        sinks.append(TelegramAlertSink(bot_token, chat_id))
    return HysteresisAlertSink(CompositeAlertSink(sinks, logger))
