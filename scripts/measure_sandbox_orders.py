"""Explicitly gated testnet-only order ACK/cancel/fill observation probe."""
from datetime import datetime, timezone
import argparse
import json
import logging
import os
from pathlib import Path
import time
from urllib.parse import urlparse
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

from core.order_latency import OrderLatencyRecorder
from core.request_budget import install_exchange_budget, request_scope


def sandbox_exchange(venue):
    import ccxt
    exchange = getattr(ccxt, venue)({"apiKey": os.getenv("EXCHANGE_API_KEY"),
        "secret": os.getenv("EXCHANGE_SECRET"), "password": os.getenv("EXCHANGE_PASSWORD"),
        "enableRateLimit": True, "timeout": 5000,
        "options": {"defaultType": "spot", "fetchCurrencies": False, "fetchMarkets": {"types": ["spot"]}}})
    exchange.set_sandbox_mode(True)
    if venue == "binance":
        host = urlparse(exchange.urls["api"]["private"]).hostname
        if host not in {"testnet.binance.vision", "demo-api.binance.com"}:
            raise ValueError("refusing a non-testnet Binance order endpoint")
    elif venue == "okx":
        if (exchange.headers or {}).get("x-simulated-trading") != "1":
            raise ValueError("refusing OKX without simulated-trading header")
    else:
        raise ValueError("unsupported sandbox venue")
    install_exchange_budget(exchange, venue, priority="critical")
    return exchange


def probe(exchange, symbol, notional, recorder, *, phases=None):
    if not 0 < notional <= 20:
        raise ValueError("sandbox probe notional must be at most 20 quote units")
    exchange.load_markets()
    market = exchange.market(symbol)
    if not market.get("spot"):
        raise ValueError("probe only supports sandbox spot markets")
    ticker = exchange.fetch_ticker(symbol)
    price = float(ticker.get("ask") or ticker["last"])
    amount = float(exchange.amount_to_precision(symbol, notional / price))
    limit_price = float(exchange.price_to_precision(symbol, price * .8))
    minimum = ((market.get("limits") or {}).get("cost") or {}).get("min") or 0.
    if amount <= 0 or amount * limit_price < minimum:
        raise ValueError("sandbox minimum order exceeds this capped probe")
    balance = exchange.fetch_balance({"type": "spot"})
    if float((balance.get("free") or {}).get(market["quote"], 0)) < notional * 1.01:
        raise ValueError("insufficient simulated quote balance")
    outstanding = {}
    phases = [] if phases is None else phases
    def submit(kind, side, qty, order_price=None):
        client_id = "qtlat" + uuid4().hex[:20]
        # Writes are never retried. Unknown responses terminate the probe.
        try:
            row = recorder.call("submit_" + kind, exchange.create_order,
                symbol, kind, side, qty, order_price, {"clientOrderId": client_id})
        except Exception:
            phases.append({"phase": "submit", "status": "unknown_or_rejected", "client_order_id": client_id})
            raise
        if not row.get("id"):
            raise ValueError("sandbox order acknowledgement has no id")
        outstanding[row["id"]] = row
        phases.append({"phase": "submit", "status": row.get("status"), "client_order_id": client_id})
        return row
    def terminal(row):
        until = time.monotonic() + 5.
        while row.get("status") not in {"closed", "canceled", "expired", "rejected"}:
            if time.monotonic() >= until:
                raise TimeoutError("sandbox terminal observation timed out")
            time.sleep(.2)
            row = recorder.call("terminal_observation", exchange.fetch_order, row["id"], symbol)
        outstanding.pop(row["id"], None)
        return row
    try:
        limit = submit("limit", "buy", amount, limit_price)
        canceled = recorder.call("cancel", exchange.cancel_order, limit["id"], symbol)
        limit = terminal(canceled)
        if float(limit.get("filled") or 0) > 0:
            raise RuntimeError("resting probe filled unexpectedly; stop and inspect simulated inventory")
        buy = terminal(submit("market", "buy", amount))
        fees = buy.get("fees") or ([buy["fee"]] if buy.get("fee") else [])
        base_fee = sum(float(fee.get("cost") or 0) for fee in fees if fee.get("currency") == market["base"])
        sell_qty = float(exchange.amount_to_precision(symbol, float(buy.get("filled") or 0) - base_fee))
        if sell_qty <= 0:
            raise ValueError("sandbox buy produced no confirmed sellable fill")
        sell = terminal(submit("market", "sell", sell_qty))
        phases.append({"phase": "roundtrip", "status": sell.get("status"),
                       "residual_base": float(buy.get("filled") or 0) - base_fee - float(sell.get("filled") or 0)})
        return phases
    finally:
        # Cancel only this probe's acknowledged open orders, never account-wide.
        for order_id in list(outstanding):
            try:
                recorder.call("cleanup_cancel", exchange.cancel_order, order_id, symbol)
            except Exception:
                phases.append({"phase": "cleanup", "status": "unverified", "order_id": order_id})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", choices=["binance", "okx"], default="binance")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--notional", type=float, default=20.)
    parser.add_argument("--execute-sandbox-orders", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    report = {"schema": "sandbox-order-latency/v1", "venue": args.exchange,
              "symbol": args.symbol, "measured_at": datetime.now(timezone.utc).isoformat(),
              "environment": "sandbox_only", "status": "not_executed", "records": []}
    exit_code, exchange = 2, None
    recorder = OrderLatencyRecorder()
    present = bool(os.getenv("EXCHANGE_API_KEY") and os.getenv("EXCHANGE_SECRET"))
    if not present:
        report["status"] = "blocked_missing_sandbox_credentials"
    elif not args.execute_sandbox_orders:
        report["status"] = "ready_requires_explicit_execution_flag"
    else:
        try:
            exchange = sandbox_exchange(args.exchange)
            with request_scope(deadline=time.monotonic() + 30., priority="critical"):
                report["phases"] = []
                probe(exchange, args.symbol, args.notional, recorder, phases=report["phases"])
            report["status"], exit_code = "measured", 0
        except Exception as exc:
            report["status"], report["error_category"] = "failed_or_unverified", type(exc).__name__
        finally:
            if exchange is not None:
                exchange.session.close()
    report["latency"] = recorder.summary()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "output": str(args.output)}, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
