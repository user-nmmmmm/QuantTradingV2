import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.alerting import (
    TelegramAlertSink,
    build_default_alert_sink,
    format_telegram_alert,
    send_telegram_message,
)
from core.telegram_heartbeat import build_heartbeat_message, send_heartbeat


class TestTelegramAlertSink(unittest.TestCase):
    def test_rejects_missing_credentials(self):
        with self.assertRaises(ValueError):
            TelegramAlertSink("", "chat")
        with self.assertRaises(ValueError):
            TelegramAlertSink("token", "")

    @patch("core.alerting.urllib.request.urlopen")
    def test_notify_posts_to_bot_api_with_chat_id_and_text(self, urlopen_mock):
        urlopen_mock.return_value.__enter__.return_value.read.return_value = b"{}"
        sink = TelegramAlertSink("TOKEN123", "chat-42")

        sink.notify("critical", "risk_halt", {"reason_codes": ["MARKET_DATA_STALE"]})

        request = urlopen_mock.call_args[0][0]
        self.assertEqual(
            request.full_url, "https://api.telegram.org/bot TOKEN123/sendMessage".replace(" ", ""),
        )
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["chat_id"], "chat-42")
        self.assertIn("risk_halt", body["text"])
        self.assertIn("MARKET_DATA_STALE", body["text"])

    def test_format_telegram_alert_includes_level_event_and_context(self):
        text = format_telegram_alert("critical", "circuit_breaker_triggered", {"equity": 900.0})
        self.assertIn("[CRITICAL]", text)
        self.assertIn("circuit_breaker_triggered", text)
        self.assertIn("equity: 900.0", text)

    @patch("core.alerting.urllib.request.urlopen")
    def test_build_default_alert_sink_adds_telegram_when_env_vars_set(self, urlopen_mock):
        urlopen_mock.return_value.__enter__.return_value.read.return_value = b"{}"
        with tempfile.TemporaryDirectory() as directory:
            record_path = os.path.join(directory, "alerts.jsonl")
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "TOKEN", "TELEGRAM_CHAT_ID": "42"},
                clear=False,
            ):
                sink = build_default_alert_sink(MagicMock(), record_path=record_path)
            sink.notify("critical", "risk_halt", {"reason": "test"})
            urlopen_mock.assert_called_once()

    def test_build_default_alert_sink_skips_telegram_without_env_vars(self):
        with tempfile.TemporaryDirectory() as directory:
            record_path = os.path.join(directory, "alerts.jsonl")
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
                os.environ.pop("TELEGRAM_CHAT_ID", None)
                sink = build_default_alert_sink(MagicMock(), record_path=record_path)
            composite = sink.sink
            self.assertFalse(
                any(type(s).__name__ == "TelegramAlertSink" for s in composite.sinks)
            )


class TestTelegramHeartbeat(unittest.TestCase):
    def test_build_heartbeat_message_for_invalid_status(self):
        message = build_heartbeat_message({"status_valid": False})
        self.assertIn("STATUS FILE INVALID", message)
        self.assertIn("RISK_HALTED", message)

    def test_build_heartbeat_message_for_healthy_status(self):
        dashboard = {
            "status_valid": True, "timestamp": "2026-08-09T00:00:00Z",
            "healthy": True, "operational_state": "HEALTHY",
            "equity": 10500.0, "cash": 4000.0,
            "positions": {"BTC/USDT": {"qty": 0.5}},
            "health_reason_codes": [],
            "recent_alerts": [{"level": "info", "event": "health_recovered"}],
        }
        message = build_heartbeat_message(dashboard)
        self.assertIn("HEALTHY", message)
        self.assertIn("10500.0", message)
        self.assertIn("BTC/USDT", message)
        self.assertIn("HEALTH_RECOVERED".replace("_", "_"), message.upper())

    @patch("core.telegram_heartbeat.send_telegram_message")
    def test_send_heartbeat_loads_status_and_posts(self, send_mock):
        with tempfile.TemporaryDirectory() as directory:
            status_path = os.path.join(directory, "live_status.json")
            alerts_path = os.path.join(directory, "live_alerts.jsonl")
            with open(status_path, "w", encoding="utf-8") as handle:
                json.dump({
                    "healthy": True, "operational_state": "HEALTHY",
                    "equity": 1000.0, "cash": 500.0, "positions": {},
                    "last_update": "2026-08-09T00:00:00Z",
                }, handle)

            dashboard = send_heartbeat(status_path, alerts_path, "TOKEN", "42")

            self.assertTrue(dashboard["status_valid"])
            send_mock.assert_called_once()
            args = send_mock.call_args[0]
            self.assertEqual(args[0], "TOKEN")
            self.assertEqual(args[1], "42")
            self.assertIn("HEALTHY", args[2])


if __name__ == "__main__":
    unittest.main()
