"""Local, read-only web view of the live operations snapshot.

Run ``python -m dashboard.web`` from the repository root.  The server binds to
loopback only and never imports the trading engine or exposes write endpoints.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlsplit

from core.status_snapshot import load_dashboard

from dashboard.web_demo import demo_snapshot
from dashboard.visual_data import list_backtests, list_markets, load_backtest, load_candles


ASSETS = {
    "/": ("web_index.html", "text/html; charset=utf-8"),
    "/assets/style.css": ("web_style.css", "text/css; charset=utf-8"),
    "/assets/theme.css": ("web_theme.css", "text/css; charset=utf-8"),
    "/assets/theme.js": ("web_theme.js", "text/javascript; charset=utf-8"),
    "/assets/visual.css": ("web_visual.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("web_app.js", "text/javascript; charset=utf-8"),
    "/assets/visual.js": ("web_visual.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("web_favicon.svg", "image/svg+xml"),
}
WEB_DIR = Path(__file__).parent
PROJECT_ROOT = WEB_DIR.parent


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _json_safe(value: Any) -> Any:
    """Keep the API valid JSON even if an upstream report contains NaN."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _snapshot_age(timestamp: Any) -> int | None:
    if not isinstance(timestamp, str):
        return None
    try:
        point = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if point.tzinfo is None:
        return None
    return max(0, int((datetime.now(timezone.utc) - point).total_seconds()))


class DashboardSource:
    """Owns only presentation state: paths and in-memory observed equity."""

    def __init__(
        self,
        status_path: str | Path,
        alerts_path: str | Path,
        phase6_path: str | Path | None = None,
        *,
        demo: bool = False,
        data_dir: str | Path | None = None,
        reports_dir: str | Path | None = None,
    ) -> None:
        self.status_path = Path(status_path)
        self.alerts_path = Path(alerts_path)
        self.phase6_path = Path(phase6_path) if phase6_path else None
        self.demo = demo
        self.data_dir = Path(data_dir) if data_dir else PROJECT_ROOT / "data" / "binance" / "1d"
        self.reports_dir = Path(reports_dir) if reports_dir else PROJECT_ROOT / "reports"
        self._history: deque[dict[str, Any]] = deque(maxlen=120)
        self._lock = Lock()

    def _details(self, dashboard: dict[str, Any]) -> dict[str, Any]:
        # The shared loader validates the status first. Read optional fields
        # only when this second read still refers to that same snapshot.
        try:
            raw = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        if (
            (raw.get("last_update") or raw.get("timestamp")) != dashboard.get("timestamp")
            or raw.get("healthy") != dashboard.get("healthy")
            or raw.get("equity") != dashboard.get("equity")
            or raw.get("cash") != dashboard.get("cash")
        ):
            return {}
        fields = (
            "account_entry_gate",
            "reconciliation",
            "portfolio_breaker",
            "strategy_health",
            "protective_orders",
            "unresolved_unknown_order",
            "consecutive_strategy_failures",
            "symbols",
        )
        return {key: raw[key] for key in fields if key in raw}

    def snapshot(self) -> dict[str, Any]:
        if self.demo:
            return demo_snapshot()

        dashboard = load_dashboard(
            str(self.status_path),
            str(self.alerts_path),
            alert_limit=30,
            phase6_path=str(self.phase6_path) if self.phase6_path else None,
        )
        valid = dashboard["status_valid"]
        equity = _finite_number(dashboard.get("equity")) if valid else None
        timestamp = dashboard.get("timestamp") if valid else None
        if equity is not None and timestamp:
            with self._lock:
                if not self._history or self._history[-1]["timestamp"] != timestamp:
                    self._history.append({"timestamp": timestamp, "equity": equity})
        with self._lock:
            history = list(self._history)
        return _json_safe({
            **dashboard,
            "details": self._details(dashboard) if valid else {},
            "history": history if valid else [],
            "snapshot_age_seconds": _snapshot_age(timestamp),
            "mode": "live",
            "server_time": datetime.now(timezone.utc).isoformat(),
        })


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], source: DashboardSource):
        super().__init__(server_address, DashboardHandler)
        self.source = source


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:
        self._respond(include_body=True)

    def do_HEAD(self) -> None:
        self._respond(include_body=False)

    def do_POST(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Read-only dashboard")

    def _respond(self, *, include_body: bool) -> None:
        request = urlsplit(self.path)
        route = request.path
        if route == "/api/status":
            try:
                payload = self.server.source.snapshot()
            except (OSError, UnicodeError, ValueError) as exc:
                self.log_error("Dashboard data unavailable: %s", type(exc).__name__)
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Dashboard data unavailable")
                return
            self._send_json(payload, include_body)
            return
        if route in {"/api/markets", "/api/candles", "/api/backtests", "/api/backtest"}:
            query = parse_qs(request.query)
            try:
                if route == "/api/markets":
                    payload = {"markets": list_markets(self.server.source.data_dir)}
                elif route == "/api/candles":
                    symbol = query.get("symbol", ["BTC/USDT"])[0]
                    limit = int(query.get("limit", ["120"])[0])
                    payload = load_candles(self.server.source.data_dir, symbol, limit)
                elif route == "/api/backtests":
                    payload = {"runs": list_backtests(self.server.source.reports_dir)}
                else:
                    payload = load_backtest(self.server.source.reports_dir, query.get("id", [""])[0])
            except (ValueError, TypeError) as exc:
                self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND, "Data not found")
                return
            except OSError as exc:
                self.log_error("Visualization data unavailable: %s", type(exc).__name__)
                self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Visualization data unavailable")
                return
            self._send_json(payload, include_body)
            return
        asset = ASSETS.get(route)
        if asset is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = asset
        try:
            body = (WEB_DIR / filename).read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._send(body, content_type, include_body)

    def _send_json(self, payload: Any, include_body: bool) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", include_body)

    def _send(self, body: bytes, content_type: str, include_body: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if include_body:
            self.wfile.write(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local read-only quant monitoring page")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--status", default="reports/live_status.json")
    parser.add_argument("--alerts", default="reports/live_alerts.jsonl")
    parser.add_argument("--phase6-report", default="reports/phase6/phase6_report.json")
    parser.add_argument("--demo", action="store_true", help="show clearly labelled sample data")
    args = parser.parse_args()
    source = DashboardSource(args.status, args.alerts, args.phase6_report, demo=args.demo)
    server = DashboardHTTPServer(("127.0.0.1", args.port), source)
    print(f"Dashboard: http://127.0.0.1:{server.server_port}/")
    print("Mode: DEMO (sample data)" if args.demo else "Mode: live snapshot (read-only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
