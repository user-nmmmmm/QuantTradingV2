"""No-network diagnostics and anonymous same-spot-service routing contracts."""
from threading import Event

import pytest
import requests

from core.quote_observations import _PublicBookFetcher, anonymous_public_auth, quote_error_details
from scripts.diagnose_public_market import bounded_probe, diagnose, proxy_presence, safe_error


def test_anonymous_spot_client_honors_env_without_netrc_or_unrelated_metadata(monkeypatch):
    def no_netrc(*args, **kwargs):
        raise AssertionError("public quote request may not read credentials")
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", no_netrc)
    fetcher = _PublicBookFetcher("binance", "spot", "live", 3.)
    try:
        assert fetcher.client.session.trust_env
        assert not fetcher.client.options["fetchCurrencies"]
        assert not fetcher.client.options["fetchMargins"]
        request = fetcher.client.session.prepare_request(requests.Request("GET", "https://api.binance.com/api/v3/ping",
            headers={"Authorization": "must-be-removed"}))
        assert "Authorization" not in request.headers
        result = diagnose(network=False)
        trace = result["ccxt"]["initialization_trace_no_network"]
        assert trace["routes"] == [{"host": "api.binance.com", "path": "/api/v3/exchangeInfo", "method": "GET"}]
        assert not trace["unrelated_derivative_endpoints"] and not trace["private_endpoints"]
        assert not any(result["ccxt"]["credential_attributes_present"].values())
    finally:
        fetcher.close()


def test_proxy_presence_and_exception_chain_never_expose_addresses_or_secrets(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://secret-user:secret-password@private-host:123")
    info = proxy_presence("https://api.binance.com/api/v3/ping")
    assert info["environment_variables_set"]["HTTPS_PROXY"]
    assert "secret" not in str(info) and "private-host" not in str(info)
    try:
        try:
            raise PermissionError(13, "http://secret-user:secret-password@private-host:123")
        except PermissionError as exc:
            raise RuntimeError("sensitive SDK text") from exc
    except RuntimeError as exc:
        safe = safe_error(exc)
        assert safe == quote_error_details(exc)
        assert safe["exception_chain"] == ["RuntimeError", "PermissionError"]
        assert safe["os_error_codes"] == [{"field": "errno", "value": 13}]
        assert "sensitive" not in str(safe) and "secret" not in str(safe)


def test_diagnostic_deadline_and_host_allowlist_are_bounded():
    release = Event()
    result = bounded_probe(lambda: release.wait(.2), .01)
    release.set()
    assert result["status"] == "timeout" and result["elapsed_seconds"] < .15
    with pytest.raises(ValueError):
        diagnose(host="untrusted.example", network=False)
    with pytest.raises(ValueError):
        diagnose(timeout_seconds=11, network=False)


def test_separate_sessions_append_same_quote_store_without_rewriting_past(tmp_path, monkeypatch):
    import json
    import sys
    from scripts import collect_execution_quotes as cli
    from core.quote_observations import BackgroundQuoteSampler, read_quote_observations

    real_sampler = BackgroundQuoteSampler
    def factory(**kwargs):
        return real_sampler(**kwargs, fetcher=lambda symbol: {"bids": [[99., 1.]], "asks": [[101., 1.]], "timestamp": None})
    monkeypatch.setattr(cli, "BackgroundQuoteSampler", factory)
    first, second = tmp_path/"first", tmp_path/"second"
    def run(folder, extra=()):
        monkeypatch.setattr(sys, "argv", ["collect_execution_quotes.py", "--symbols", "BTC/USDT", "ETH/USDT",
            "--duration-seconds", ".12", "--interval-seconds", ".02", "--output", str(folder), *extra])
        cli.main()
    run(first)
    store_path = first/"execution_quotes.sqlite3"
    old = read_quote_observations(store_path)
    manifest_bytes = (first/"manifest.json").read_bytes()
    run(second, ("--resume-store", str(store_path)))
    all_rows = read_quote_observations(store_path)
    assert old and len(all_rows) > len(old)
    assert all_rows[:len(old)] == old
    assert (first/"manifest.json").read_bytes() == manifest_bytes
    new = json.loads((second/"quote_observations.json").read_text())
    assert new and min(row["sequence"] for row in new) > old[-1]["sequence"]
    assert {row["collector_run_id"] for row in old}.isdisjoint(row["collector_run_id"] for row in new)
    assert json.loads((second/"summary.json").read_text())["real_venue_calibration"] is False
