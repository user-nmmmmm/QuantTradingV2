import io
import json
import logging
from unittest.mock import patch

import pytest

from core.alerting import JsonlAlertSink, WebhookAlertSink, format_telegram_alert
from core.domain import OrderIntent
from core.events.codec import _decode_value, _encode_value
from core.logger import SensitiveDataFilter
from core.redaction import safe_url


@pytest.mark.parametrize("tag", ["dataclass", "structured_payload", "enum"])
def test_untrusted_class_rejected_before_any_import(tag):
    with patch("importlib.import_module", side_effect=AssertionError("must not import")):
        with pytest.raises(ValueError, match="Unregistered"):
            _decode_value({"__qt_type__": "list", "items": [
                {"__qt_type__": tag, "class": "attacker_module:Payload", "fields": {}, "data": {}, "value": 1}
            ]})


def test_order_with_risk_metadata_roundtrips_without_identity_change():
    intent = OrderIntent("binance", "sandbox/margin/test", "BTC/USDT", "1d", "2026-06-30",
                         "ProtectiveStop", "sell", 17, 0.5, order_type="stop",
                         reference_price=100, trigger_price=90, initial_stop=80,
                         exit_reason="protective_stop", risk_action_id="replace:17")
    assert _decode_value(_encode_value(intent)) == intent


def test_complete_rendered_exception_is_sanitized():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SensitiveDataFilter())
    log = logging.Logger("fake_credentials_only")
    log.addHandler(handler)
    with patch.dict("os.environ", {}, clear=True):
        log.error("Authorization: Bearer FAKE_TOKEN")
        try:
            raise ValueError('Authorization: Bearer FAKE_TOKEN https://fakeuser:FAKE_PASS@proxy.test:80/path?signature=FAKE_SIG')
        except ValueError:
            log.exception("ordinary diagnostic")
    output = stream.getvalue()
    assert "ordinary diagnostic" in output and "ValueError" in output
    for secret in ("FAKE_TOKEN", "FAKE_PASS", "FAKE_SIG", "fakeuser"):
        assert secret not in output


def test_direct_alert_sinks_sanitize_nested_context(tmp_path):
    context = {"original_context": {"password": "FAKE_PASSWORD", "error": "https://u:FAKE_PROXY@host.test/x?token=FAKE_QUERY"}, "qty": 2}
    sink = JsonlAlertSink(str(tmp_path / "alerts.jsonl"))
    sink.notify("error", "ordinary_event", context)
    text = (tmp_path / "alerts.jsonl").read_text()
    assert "FAKE_" not in text
    assert json.loads(text)["context"]["qty"] == 2
    assert context["original_context"]["password"] == "FAKE_PASSWORD"
    with patch("urllib.request.urlopen") as send:
        WebhookAlertSink("https://webhook.test").notify("error", "event", context)
        assert b"FAKE_" not in send.call_args.args[0].data
    assert "FAKE_" not in format_telegram_alert("error", "event", context)
    assert safe_url("https://u:p@[::1]:8080/a?secret=x") == "https://[::1]:8080"


def test_public_alert_masks_configured_secret_in_free_text():
    with patch.dict("os.environ", {"EXCHANGE_SECRET": "FAKE_EXCHANGE_SECRET"}, clear=True):
        context = {"error": "failed with FAKE_EXCHANGE_SECRET", "api_secret": "FAKE_OTHER_SECRET"}
        with patch("urllib.request.urlopen") as send:
            WebhookAlertSink("https://webhook.test").notify("error", "event", context)
            assert b"FAKE_" not in send.call_args.args[0].data
        assert "FAKE_" not in format_telegram_alert("error", "event", context)
