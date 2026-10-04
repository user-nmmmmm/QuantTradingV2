"""Local monitoring and isolated, offline historical backtest workspace.

Run ``python -m dashboard.web`` from the repository root.  The server binds to
loopback only; live operations remain read-only and backtests run in subprocesses.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import sqlite3
import time
import webbrowser
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
from dashboard.visual_data import list_backtests, list_markets, load_backtest, load_candles, load_trades
from dashboard.backtest_jobs import BacktestJobs, JobConflict
from dashboard.experiment_compare import compare_experiments
from dashboard.market_analysis import load_market_analysis, load_data_quality
from dashboard.research_analytics import load_diagnostics
from dashboard.robust_research import RobustResearch


ASSETS = {
    "/": ("web_index.html", "text/html; charset=utf-8"),
    "/assets/style.css": ("web_style.css", "text/css; charset=utf-8"),
    "/assets/theme.css": ("web_theme.css", "text/css; charset=utf-8"),
    "/assets/theme.js": ("web_theme.js", "text/javascript; charset=utf-8"),
    "/assets/visual.css": ("web_visual.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("web_app.js", "text/javascript; charset=utf-8"),
    "/assets/visual.js": ("web_visual.js", "text/javascript; charset=utf-8"),
    "/assets/api.js": ("web_api.js", "text/javascript; charset=utf-8"),
    "/assets/workspace.js": ("web_workspace.js", "text/javascript; charset=utf-8"),
    "/assets/workspace.css": ("web_workspace.css", "text/css; charset=utf-8"),
    "/assets/research.js": ("web_research.js", "text/javascript; charset=utf-8"),
    "/assets/lab.js": ("web_lab.js", "text/javascript; charset=utf-8"),
    "/assets/lab.css": ("web_lab.css", "text/css; charset=utf-8"),
    "/assets/charts.js": ("web_charts.js", "text/javascript; charset=utf-8"),
    "/assets/strategy.js": ("web_strategy.js", "text/javascript; charset=utf-8"),
    "/assets/market.js": ("web_market.js", "text/javascript; charset=utf-8"),
    "/assets/robust.js": ("web_robust.js", "text/javascript; charset=utf-8"),
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

    def __init__(self, server_address: tuple[str, int], source: DashboardSource, *,
                 backtests_enabled: bool = True):
        super().__init__(server_address, DashboardHandler)
        self.source = source
        self.jobs = BacktestJobs(source.data_dir, source.reports_dir, enabled=backtests_enabled)
        self.research = RobustResearch(self.jobs)
        self._catalog_lock = Lock()
        self._catalog_scanned_at = 0.0
        self._catalog_registered: set[str] = set()

    def sync_experiment_reports(self) -> None:
        with self._catalog_lock:
            if time.monotonic() - self._catalog_scanned_at < 10:
                return
            for run in list_backtests(self.source.reports_dir, limit=250):
                if run["id"] not in self._catalog_registered:
                    self.jobs.store.register_report(run["id"], run["parameters"], run["modified_at"])
                    self._catalog_registered.add(run["id"])
            self._catalog_scanned_at = time.monotonic()

    def server_close(self) -> None:
        self.jobs.close()
        super().server_close()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:
        self._respond(include_body=True)

    def do_HEAD(self) -> None:
        self._respond(include_body=False)

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route not in {"/api/backtest-jobs", "/api/backtest-jobs/cancel", "/api/experiments/metadata",
                         "/api/strategy-presets", "/api/strategy-presets/delete", "/api/strategy-preview",
                         "/api/research-jobs", "/api/research-jobs/cancel"}:
            self._error(HTTPStatus.METHOD_NOT_ALLOWED, "This endpoint is read-only")
            return
        if not self._trusted_host():
            self._error(HTTPStatus.FORBIDDEN, "Invalid local Host")
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + self.headers.get("Host", ""):
            self._error(HTTPStatus.FORBIDDEN, "Cross-origin requests are not allowed")
            return
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            self._error(HTTPStatus.FORBIDDEN, "Cross-origin requests are not allowed")
            return
        token = self.headers.get("X-CSRF-Token", "")
        if not secrets.compare_digest(token.encode("utf-8"), self.server.jobs.csrf_token.encode("utf-8")):
            self._error(HTTPStatus.FORBIDDEN, "Missing or invalid request token; reload the page")
            return
        if self.headers.get_content_type() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
            return
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked requests are not accepted")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32768:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON request must contain 1 to 32768 bytes")
                return
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if route == "/api/experiments/metadata":
                identifier = payload.pop("id", None)
                result = self.server.jobs.store.update_metadata(identifier, payload)
                self._send_json({"experiment": result}, True)
            elif route == "/api/strategy-presets":
                self._send_json({"preset": self.server.jobs.save_preset(payload)}, True)
            elif route == "/api/strategy-presets/delete":
                if set(payload) != {"id"}:
                    raise ValueError("Expected preset id only")
                self.server.jobs.delete_preset(payload["id"])
                self._send_json({"deleted": True}, True)
            elif route == "/api/strategy-preview":
                if set(payload) != {"strategy"}:
                    raise ValueError("Expected strategy only")
                self._send_json(self.server.jobs.preview_strategy(payload["strategy"]), True)
            elif route == "/api/research-jobs":
                self._send_json({"job": self.server.research.submit(payload)}, True, status=HTTPStatus.ACCEPTED)
            elif route.endswith("/cancel"):
                if not isinstance(payload, dict) or set(payload) != {"id"}:
                    raise ValueError("Expected job id only")
                job = self.server.jobs.cancel(payload["id"])
                self._send_json({"job": job}, True)
            else:
                job = self.server.jobs.submit(payload)
                self._send_json({"job": job}, True, status=HTTPStatus.ACCEPTED)
        except JobConflict as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except FileNotFoundError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except (ValueError, TypeError, UnicodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except (OSError, sqlite3.Error):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Backtest service unavailable")

    def _trusted_host(self) -> bool:
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            allowed.update({"127.0.0.1", "localhost"})
        return self.headers.get("Host", "").lower() in allowed

    def _error(self, status: HTTPStatus, message: str, *, include_body: bool = True) -> None:
        self._send_json({"error": message}, include_body, status=status)

    def _respond(self, *, include_body: bool) -> None:
        if not self._trusted_host():
            self._error(HTTPStatus.FORBIDDEN, "Invalid local Host", include_body=include_body)
            return
        request = urlsplit(self.path)
        route = request.path
        if route in {"/api/backtest-options", "/api/backtest-jobs"}:
            try:
                payload = self.server.jobs.options() if route.endswith("options") else {"jobs": self.server.jobs.list()}
                self._send_json(payload, include_body)
            except (ValueError, OSError):
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Backtest options unavailable", include_body=include_body)
            return
        if route == "/api/status":
            try:
                payload = self.server.source.snapshot()
            except (OSError, UnicodeError, ValueError) as exc:
                self.log_error("Dashboard data unavailable: %s", type(exc).__name__)
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Dashboard data unavailable", include_body=include_body)
                return
            self._send_json(payload, include_body)
            return
        if route in {"/api/markets", "/api/candles", "/api/backtests", "/api/backtest", "/api/backtest-trades",
                     "/api/backtest-diagnostics", "/api/compare", "/api/experiments", "/api/experiment",
                     "/api/strategy-catalog", "/api/strategy-presets", "/api/data-quality", "/api/market-analysis",
                     "/api/research-options", "/api/research-jobs", "/api/research-result"}:
            query = parse_qs(request.query)
            try:
                if route == "/api/research-options":
                    payload = self.server.research.options()
                elif route == "/api/research-jobs":
                    payload = {"jobs": [job for job in self.server.jobs.list() if job.get("kind") == "robust"]}
                elif route == "/api/research-result":
                    payload = self.server.research.result(query.get("id", [""])[0])
                elif route == "/api/strategy-catalog":
                    payload = self.server.jobs.strategy_catalog()
                elif route == "/api/strategy-presets":
                    payload = {"presets": self.server.jobs.list_presets()}
                elif route == "/api/experiments":
                    # Register validated, completed legacy reports once; INSERT OR IGNORE
                    # preserves annotations and the original durable job payload.
                    self.server.sync_experiment_reports()
                    favorite = query.get("favorite", [""])[0]
                    if favorite not in {"", "true", "false"}:
                        raise ValueError("favorite must be true or false")
                    payload = self.server.jobs.history(query=query.get("q", [""])[0], status=query.get("status", [""])[0],
                        tag=query.get("tag", [""])[0], kind=query.get("kind", [""])[0],
                        favorite=None if not favorite else favorite == "true",
                        limit=int(query.get("limit", ["15"])[0]), offset=int(query.get("offset", ["0"])[0]))
                elif route == "/api/experiment":
                    payload = {"experiment": self.server.jobs.store.get(query.get("id", [""])[0])}
                elif route == "/api/compare":
                    payload = compare_experiments(self.server.source.reports_dir, query.get("ids", [""])[0].split(","))
                elif route == "/api/backtest-diagnostics":
                    payload = load_diagnostics(self.server.source.reports_dir, query.get("id", [""])[0],
                                               int(query.get("page", ["1"])[0]), int(query.get("page_size", ["25"])[0]))
                elif route == "/api/data-quality":
                    selected = query.get("symbols", [""])[0]
                    payload = load_data_quality(self.server.source.data_dir, selected.split(",") if selected else None,
                                                query.get("start", [None])[0], query.get("end", [None])[0])
                elif route == "/api/market-analysis":
                    payload = load_market_analysis(self.server.source.data_dir, query.get("symbol", ["BTC/USDT"])[0],
                                                   int(query.get("limit", ["120"])[0]))
                elif route == "/api/markets":
                    payload = {"markets": list_markets(self.server.source.data_dir)}
                elif route == "/api/candles":
                    symbol = query.get("symbol", ["BTC/USDT"])[0]
                    limit = int(query.get("limit", ["120"])[0])
                    payload = load_candles(self.server.source.data_dir, symbol, limit)
                elif route == "/api/backtests":
                    payload = {"runs": list_backtests(self.server.source.reports_dir)}
                elif route == "/api/backtest-trades":
                    payload = load_trades(self.server.source.reports_dir, query.get("id", [""])[0],
                                          int(query.get("page", ["1"])[0]),
                                          int(query.get("page_size", ["25"])[0]))
                else:
                    payload = load_backtest(self.server.source.reports_dir, query.get("id", [""])[0])
            except (ValueError, TypeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc), include_body=include_body)
                return
            except FileNotFoundError:
                self._error(HTTPStatus.NOT_FOUND, "Data not found", include_body=include_body)
                return
            except (OSError, sqlite3.Error) as exc:
                self.log_error("Visualization data unavailable: %s", type(exc).__name__)
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, "Visualization data unavailable", include_body=include_body)
                return
            self._send_json(payload, include_body)
            return
        asset = ASSETS.get(route)
        if asset is None:
            self._error(HTTPStatus.NOT_FOUND, "Not found", include_body=include_body)
            return
        filename, content_type = asset
        try:
            body = (WEB_DIR / filename).read_bytes()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Asset unavailable", include_body=include_body)
            return
        etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
        if self.headers.get("If-None-Match") == etag:
            self._send(b"", content_type, False, status=HTTPStatus.NOT_MODIFIED, cache="no-cache", etag=etag)
        else:
            self._send(body, content_type, include_body, cache="no-cache", etag=etag)

    def _send_json(self, payload: Any, include_body: bool, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", include_body, status=status)

    def _send(self, body: bytes, content_type: str, include_body: bool, *,
              status: HTTPStatus = HTTPStatus.OK, cache: str = "no-store", etag: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if status != HTTPStatus.NOT_MODIFIED:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if etag:
            self.send_header("ETag", etag)
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
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Local quant monitoring and offline backtest workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--status", default="reports/live_status.json")
    parser.add_argument("--alerts", default="reports/live_alerts.jsonl")
    parser.add_argument("--phase6-report", default="reports/phase6/phase6_report.json")
    parser.add_argument("--demo", action="store_true", help="show clearly labelled sample data")
    parser.add_argument("--open", action="store_true", help="open the local page in your browser")
    parser.add_argument("--no-backtests", action="store_true", help="disable backtest execution")
    parser.add_argument("--data-dir", default=str(PROJECT_ROOT / "data" / "binance" / "1d"))
    parser.add_argument("--reports-dir", default=str(PROJECT_ROOT / "reports"))
    args = parser.parse_args()
    source = DashboardSource(args.status, args.alerts, args.phase6_report, demo=args.demo,
                             data_dir=args.data_dir, reports_dir=args.reports_dir)
    server = DashboardHTTPServer(("127.0.0.1", args.port), source, backtests_enabled=not args.no_backtests)
    print(f"Dashboard: http://127.0.0.1:{server.server_port}/")
    print("Mode: DEMO (sample data)" if args.demo else "Mode: live snapshot (read-only)")
    print("Offline backtests: disabled" if args.no_backtests else "Offline backtests: enabled (local/synthetic only)")
    if args.open:
        webbrowser.open(f"http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
