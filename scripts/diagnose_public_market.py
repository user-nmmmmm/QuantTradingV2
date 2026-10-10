"""Bounded, anonymous Binance spot diagnostics; never changes network settings.

Outputs only proxy-presence booleans and sanitized exception classifications.
DNS/TCP/TLS probe the selected official service; HTTP honors environment routing.
No alternate-host failover, credentials, orders, or certificate bypass is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from queue import Queue, Empty
import socket
import ssl
from threading import Thread
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

OFFICIAL_HOSTS = ("api.binance.com", "data-api.binance.vision")
OFFICIAL_DOCS = [
    "https://developers.binance.com/en/docs/products/spot/rest-api",
    "https://developers.binance.com/en/docs/products/spot/faqs/market_data_only",
    "https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market",
]


def safe_error(exc):
    """Exception strings can contain authenticated proxy URLs: never emit them."""
    chain, codes, seen = [], [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        chain.append(type(exc).__name__)
        codes.extend({"field": field, "value": getattr(exc, field)} for field in ("errno", "winerror")
                     if isinstance(getattr(exc, field, None), int))
        exc = exc.__cause__ or exc.__context__
    return {"exception_chain": chain, "os_error_codes": codes}


def bounded_probe(function, timeout):
    result = Queue(maxsize=1)
    def run():
        try:
            result.put({"status": "ok", "details": function()})
        except Exception as exc:
            result.put({"status": "failed", **safe_error(exc)})
    started = time.monotonic()
    Thread(target=run, daemon=True).start()
    try:
        row = result.get(timeout=timeout)
    except Empty:
        row = {"status": "timeout", "exception_chain": ["DiagnosticDeadline"], "os_error_codes": []}
    return {**row, "elapsed_seconds": time.monotonic()-started}


def proxy_presence(url):
    from requests.utils import get_environ_proxies
    names = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    return {"environment_variables_set": {k: bool(os.environ.get(k)) for k in names},
            "effective_proxy_types_set": {k: bool(v) for k, v in get_environ_proxies(url).items()
                                          if k in {"http", "https", "all", "no"}},
            "proxy_addresses_and_credentials_redacted": True}


def trace_ccxt_initialization(fetcher):
    """Run the installed SDK metadata path against a local stub, never network."""
    routes = []
    client = fetcher.client
    original = client.fetch
    def capture(url, method="GET", headers=None, body=None):
        parsed = urlparse(url)
        routes.append({"host": parsed.hostname, "path": parsed.path, "method": method})
        return {"symbols": []}
    client.fetch = capture
    try:
        client.load_markets()
    finally:
        client.fetch = original
    return {"routes": routes, "unrelated_derivative_endpoints": any(
        row["path"].startswith(("/fapi/", "/dapi/", "/eapi/")) for row in routes),
        "private_endpoints": any(row["path"].startswith("/sapi/") for row in routes)}


def diagnose(*, host="api.binance.com", timeout_seconds=3., network=True):
    if host not in OFFICIAL_HOSTS or not 0 < timeout_seconds <= 10:
        raise ValueError("an allowlisted official Binance spot host and timeout <= 10 seconds are required")
    import ccxt
    import requests
    from core.quote_observations import _PublicBookFetcher, anonymous_public_auth
    from core.request_budget import request_scope
    url = f"https://{host}/api/v3/ping"
    client = _PublicBookFetcher("binance", "spot", "live", timeout_seconds)
    # Explicit selection of Binance's documented market-data service, never
    # automatic retry on another host after a refusal or regional restriction.
    if host != "api.binance.com":
        client.client.urls["api"]["public"] = f"https://{host}/api/v3"
    result = {"schema": "public-market-diagnostic/v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "host": host, "market_type": "spot", "public_read_only": True, "orders_submitted": 0,
        "network_settings_changed": False, "credentials_used": False, "timeout_seconds": timeout_seconds,
        "proxy_presence": proxy_presence(url), "official_sources": OFFICIAL_DOCS,
        "ccxt": {"version": ccxt.__version__, "default_type": client.client.options.get("defaultType"),
                 "fetch_markets": client.client.options.get("fetchMarkets"),
                 "fetch_currencies": client.client.options.get("fetchCurrencies"),
                 "requests_trust_env": client.client.session.trust_env,
                 "credential_attributes_present": {k: bool(getattr(client.client, k, None))
                                                  for k in ("apiKey", "secret", "password")}}}
    result["ccxt"]["initialization_trace_no_network"] = trace_ccxt_initialization(client)
    if not network:
        client.close()
        result.update(status="configuration_only", layers={})
        return result
    layers = {}
    addresses = []
    def dns():
        addresses.extend(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        return {"address_count": len({row[4][0] for row in addresses}),
                "families": sorted({row[0].name for row in addresses})}
    layers["dns"] = bounded_probe(dns, timeout_seconds)
    def tcp():
        if not addresses:
            raise RuntimeError("DNS unavailable")
        row = next((row for row in addresses if row[0] == socket.AF_INET), addresses[0])
        with socket.socket(row[0], row[1], row[2]) as sock:
            sock.settimeout(timeout_seconds)
            sock.connect(row[4])
            return {"connected": True, "route": "direct_tcp_diagnostic_no_http"}
    layers["tcp_443"] = bounded_probe(tcp, timeout_seconds) if layers["dns"]["status"] == "ok" else {
        "status": "skipped", "reason": "dns_resolution_unavailable"}
    def tls():
        if not addresses:
            raise RuntimeError("DNS unavailable")
        row = next((row for row in addresses if row[0] == socket.AF_INET), addresses[0])
        with socket.socket(row[0], row[1], row[2]) as raw:
            raw.settimeout(timeout_seconds)
            raw.connect(row[4])
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as secure:
                return {"tls_version": secure.version(), "certificate_verified": True,
                        "route": "direct_tls_diagnostic_no_http"}
    layers["tls"] = bounded_probe(tls, timeout_seconds) if layers["tcp_443"]["status"] == "ok" else {
        "status": "skipped", "reason": "tcp_connection_unavailable"}
    def http():
        with requests.Session() as session:
            # Requests' normal env-routing behavior is preserved, not removed.
            session.auth = anonymous_public_auth
            response = session.get(url, timeout=timeout_seconds, allow_redirects=False)
            return {"http_status": response.status_code, "body_bytes": len(response.content),
                    "requests_trust_env": session.trust_env,
                    "service_refusal": response.status_code in {403, 418, 429, 451}}
    layers["https_public_ping"] = bounded_probe(http, timeout_seconds+.5)
    refused = layers["https_public_ping"].get("details", {}).get("service_refusal", False)
    routes = []
    original = client.client.fetch
    def fetch(url, method="GET", headers=None, body=None):
        parsed = urlparse(url)
        routes.append({"host": parsed.hostname, "path": parsed.path, "method": method})
        return original(url, method, headers, body)
    client.client.fetch = fetch
    def bbo():
        with request_scope(deadline=time.monotonic()+timeout_seconds, priority="research"):
            book = client("BTC/USDT")
        return {"bids": len(book.get("bids", [])), "asks": len(book.get("asks", [])),
                "exchange_timestamp_present": book.get("timestamp") is not None}
    layers["ccxt_spot_order_book"] = ({"status": "skipped", "reason": "service_refusal_no_retries"}
        if refused else bounded_probe(bbo, timeout_seconds+.5))
    result["ccxt"]["actual_request_routes"] = routes
    # Closing a client still running after a diagnostic deadline would race its
    # network operation. Its daemon call expires under the original SDK timeout.
    if layers["ccxt_spot_order_book"]["status"] != "timeout":
        client.close()
    conclusions = []
    if layers["dns"]["status"] != "ok":
        conclusions.append("dns_unavailable")
    if any(code["value"] in {13, 10013} for layer in layers.values() for code in layer.get("os_error_codes", [])):
        conclusions.append("operating_system_or_sandbox_denied_network_access")
    if "ProxyError" in layers["https_public_ping"].get("exception_chain", []):
        conclusions.append("configured_proxy_connection_failed")
    if refused:
        conclusions.append("service_refused_request_no_host_failover_attempted")
    if not result["ccxt"]["requests_trust_env"] and result["proxy_presence"]["effective_proxy_types_set"]:
        conclusions.append("anonymous_ccxt_client_ignores_configured_environment_proxy")
    if result["ccxt"]["initialization_trace_no_network"]["unrelated_derivative_endpoints"]:
        conclusions.append("spot_initialization_touches_unrelated_derivative_endpoints")
    result.update(layers=layers, conclusions=conclusions,
        status="public_spot_accessible" if layers["ccxt_spot_order_book"]["status"] == "ok" else "unavailable",
        completed_at=datetime.now(timezone.utc).isoformat())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", choices=OFFICIAL_HOSTS, default="api.binance.com")
    parser.add_argument("--timeout-seconds", type=float, default=3.)
    parser.add_argument("--configuration-only", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("diagnostic evidence is immutable; choose a fresh output")
    output = diagnose(host=args.host, timeout_seconds=args.timeout_seconds, network=not args.configuration_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    print(json.dumps({"status": output["status"], "conclusions": output.get("conclusions", []),
                      "layers": {k: v["status"] for k, v in output["layers"].items()}, "output": str(args.output)}))


if __name__ == "__main__":
    main()
