"""Account fact sync: cash and position reconciliation against the exchange.

Split out of core/live_broker.py (A4) — see docs/architecture_review.md. ``AccountSync`` is a collaborator held by
``LiveBroker`` (``_account_sync``); see core/live_broker/submission.py for how it reaches the shared state.
"""
from __future__ import annotations

import math
from copy import deepcopy
from datetime import timezone
from typing import Any, Dict, Optional

from core.trading.account_reconciliation import reconcile_account_snapshots
from core.trading.account_source import (
    AccountSourceError, build_local_account_projection, normalize_account_source,
)
from core.foundation.domain import SyncResult
from core.foundation.logger import get_logger
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.live_broker import LiveBroker

# Same logger name as core.live_broker (logging.getLogger caches by name, so
# this is the identical object) -- tests use assertLogs("core.live_broker")
# and must keep catching records logged from this module too.
logger = get_logger("core.live_broker")

DERIVATIVE_TYPES = {"future", "futures", "swap", "perpetual"}


class AccountSync:
    """Fetch and apply the exchange's cash/position facts to the portfolio.

    Needs from the host: ``portfolio``, ``exchange``, ``exchange_boundary``,
    ``market_type``, ``base_currency``, ``last_account_sync_at``, plus the
    shared plumbing on ``LiveBroker`` itself (``_retry_exchange_call``,
    ``_alert``, ``_clock``, ``_as_float``).
    """

    def __init__(self, host: "LiveBroker") -> None:
        self._host = host

    @property
    def account_source_configured(self) -> bool:
        host = self._host
        return getattr(host, "_account_source", None) is not None

    def configure_account_source(self, source, *, environment: str,
                                 maximum_snapshot_age_seconds: float = 90) -> None:
        """Inject a read-only source before syncing a full account.

        ``source.read(identity=..., checked_at=...)`` returns AccountSourceBundle.
        See core.trading.account_source for the pinned export contract and the trusted
        external provenance verifier. Configuration never grants new risk.
        Runtime consumers must check ``account_reconciliation_report`` and its
        capture age on every new-risk decision; arithmetic-only fixture success
        is deliberately insufficient. Both periodic and EOD consumers can
        persist ``reconcile_full_account()`` without another ledger or schema.
        """
        host = self._host
        if not isinstance(environment, str) or not environment.strip():
            raise ValueError("an explicit account environment is required")
        if (isinstance(maximum_snapshot_age_seconds, bool)
                or not math.isfinite(maximum_snapshot_age_seconds)
                or maximum_snapshot_age_seconds <= 0):
            raise ValueError("maximum_snapshot_age_seconds must be finite and positive")
        if not callable(getattr(source, "read", None)):
            raise ValueError("account source must implement read")
        host._account_source = source
        host._account_source_environment = environment
        host._account_source_maximum_age = maximum_snapshot_age_seconds
        host.account_reconciliation_report = host._account_source_failure("account_source_not_checked")

    def _account_source_failure(self, reason, *, checked_at=None):
        host = self._host
        return {"schema_version": 1, "scope": "independent_normalized_account_snapshots",
                "checked_at": (checked_at or host._clock()).astimezone(timezone.utc).isoformat(),
                "ok": False, "allows_new_risk": False,
                "production_account_source_verified": False,
                "issues": [reason], "differences": [], "equity_bridges": {}}

    def reconcile_full_account(self, *, checked_at=None):
        """Compare durable local facts with an independently supplied export.

        This performs no venue calls and preserves the original export on disk.
        Unconfigured/failed sources always replace a previous successful report.
        Raw balances from sync are also compared when available, so an export
        cannot hide a newly observed transfer or unknown asset.
        """
        host = self._host
        now = checked_at or host._clock()
        try:
            if now.tzinfo is None or now.utcoffset() is None:
                raise AccountSourceError("checked_at_requires_timezone")
            source = getattr(host, "_account_source", None)
            if source is None:
                raise AccountSourceError("independent_account_source_unavailable")
            identity = {"exchange": host.exchange_id, "environment": host._account_source_environment,
                        "account": host.account_id,
                        "market_type": "spot_margin" if host.market_type == "margin" else host.market_type,
                        "base_currency": host.base_currency}
            if identity["market_type"] not in {"spot", "spot_margin"}:
                raise AccountSourceError("account_mode_unsupported")
            bundle = source.read(identity=identity, checked_at=now)
            data = normalize_account_source(bundle, identity=identity, checked_at=now,
                                            maximum_age_seconds=host._account_source_maximum_age)
            host._rebuild_fill_projection()
            expected = build_local_account_projection(host, data, identity=identity, checked_at=now)
            report = reconcile_account_snapshots(expected, data["snapshot"], checked_at=now,
                        maximum_snapshot_age_seconds=host._account_source_maximum_age)
            report["source"] = {"source_id": data["source_id"], "sha256": bundle.sha256,
                                "evidence_kind": data["evidence_kind"],
                                "captured_at": data["captured_at"], "coverage": data["coverage"],
                                "opening_capital_source_id": data["opening_capital"]["source_id"]}
            report["maximum_snapshot_age_seconds"] = host._account_source_maximum_age
            host._compare_observed_account(data["snapshot"], report, now)
            report["ok"] = not report["issues"] and not report["differences"]
            report["production_account_source_verified"] = bool(
                bundle.production_provenance_verified is True and data["evidence_kind"] == "independent_export")
            report["allows_new_risk"] = report["ok"] and report["production_account_source_verified"]
            report["limitations"] = [
                "Source integrity and arithmetic do not prove exporter completeness or independent collection.",
                "Opening inventory and third-currency fee inventory migration require separate evidence.",
                "This report is one observation, not continuous-run or production-admission evidence.",
            ]
        except Exception as exc:
            reason = str(exc) if isinstance(exc, AccountSourceError) else "account_source_failed:" + type(exc).__name__
            report = host._account_source_failure(reason, checked_at=now if now.tzinfo else host._clock())
        host.account_reconciliation_report = report
        return deepcopy(report)

    def _compare_observed_account(self, actual, report, now):
        host = self._host
        observed = getattr(host, "account_snapshot", None)
        if not isinstance(observed, dict):
            # EOD may reconcile a pinned complete export without fetching live
            # balances. Runtime sync always supplies its separate observation.
            report["venue_balance_observation_compared"] = False
            return
        from core.trading.account_source import _time
        if not 0 <= (now - _time(observed.get("observed_at"), "balance.observed_at")).total_seconds() <= host._account_source_maximum_age:
            report["issues"].append("observed_account_balance_stale_or_future")
        if host.market_type == "margin":
            facts = host.account_balance_facts[host.base_currency]
            cash = {"free": facts["free"] - facts["liabilities"] - facts["interest"],
                    "locked": facts["locked"], "total": facts["net"]}
        else:
            raw = observed["raw_balance"]
            try:
                cash = {name: host._balance_number(raw[key][host.base_currency])
                        for name, key in (("free", "free"), ("locked", "used"), ("total", "total"))}
            except (KeyError, TypeError, ValueError) as exc:
                raise AccountSourceError("observed_free_locked_total_unavailable") from exc
        if any(abs(cash[key] - float(actual["cash"][key])) > 1e-8 for key in cash):
            report["issues"].append("observed_account_cash_differs_from_export")
        positions = {row["record_id"]: float(row["qty"]) for row in actual["positions"] if abs(float(row["qty"])) > 1e-12}
        venue = {key: float(row["qty"]) for key, row in observed["positions"].items() if abs(float(row["qty"])) > 1e-12}
        if set(positions) != set(venue) or any(abs(positions[key] - venue[key]) > 1e-8 for key in set(positions) & set(venue)):
            report["issues"].append("observed_account_positions_differ_from_export")
        report["venue_balance_observation_compared"] = True

    def sync(self) -> SyncResult:
        host = self._host
        try:
            next_cash, positions = host._retry_exchange_call(host._load_portfolio_fact)
            host.portfolio.cash = next_cash
            host.portfolio.positions = positions
            host._rebuild_fill_projection()
            host.unowned_positions = {}
            for symbol in set(positions) | set(host.portfolio.lot_books):
                position = positions.get(symbol, {"qty": 0.0})
                book = host.portfolio.lot_books.get(symbol)
                missing_identity = any(
                    not lot.strategy_id or lot.entry_time is None
                    for lot in host.portfolio.open_lots(symbol)
                )
                if (abs(float(position["qty"]) - (book.net_qty if book else 0.0)) > 1e-8
                        or missing_identity):
                    host.projection_issues.append(f"unowned_position:{symbol}")
                    if abs(float(position["qty"])) > 1e-12:
                        host.unowned_positions[symbol] = {
                            "observed_qty": float(position["qty"]),
                            "projected_qty": book.net_qty if book else 0.0,
                            "owner": None,
                            "entry_time": None,
                            "policy": "flatten_unowned",
                        }
            if getattr(host, "_account_source", None) is not None:
                if not host.reconcile_full_account()["ok"]:
                    host._alert("error", "account_reconciliation_failed", {
                        "issues": host.account_reconciliation_report["issues"],
                    })
                    return SyncResult(False, host._clock(), "account_reconciliation_failed")
            else:
                host.account_reconciliation_report = host._account_source_failure("independent_account_source_unavailable")
            host.last_account_sync_at = host._clock()
            return SyncResult(True, host.last_account_sync_at)
        except Exception as exc:
            host.account_reconciliation_report = host._account_source_failure("account_balance_sync_failed")
            logger.exception("Failed to sync portfolio category=%s", type(exc).__name__)
            host._alert("error", "account_sync_failed", {
                "error": type(exc).__name__,
                "retry_attempts": host.retry_max_attempts,
            })
            return SyncResult(False, host._clock(), type(exc).__name__)

    def _load_portfolio_fact(self):
        host = self._host
        balance = host.exchange.fetch_balance()
        if not isinstance(balance, dict):
            raise ValueError("account balance response is unavailable")
        if host.market_type == "margin":
            next_cash, positions = host._sync_margin_account(balance)
        else:
            total = balance.get("total")
            if not isinstance(total, dict) or host.base_currency not in total:
                raise ValueError("total quote cash is unavailable; free cash cannot stand in for total")
            next_cash = host._balance_number(total[host.base_currency])
            free, used = balance.get("free"), balance.get("used")
            if isinstance(free, dict) and isinstance(used, dict):
                for currency in set(free) | set(used) | set(total):
                    if currency not in free or currency not in used or currency not in total:
                        raise ValueError("cash free/locked/total facts are incomplete")
                    if abs(host._balance_number(free[currency]) + host._balance_number(used[currency])
                           - host._balance_number(total[currency])) > 1e-8:
                        raise ValueError("cash free plus locked differs from total")
            positions = (
                host._sync_derivatives_positions(balance)
                if host.market_type in DERIVATIVE_TYPES
                else host._sync_spot_positions(balance)
            )
        # Keep the observed source payload for review; this is a venue fact,
        # not evidence that opening capital/fund flows/PnL have reconciled.
        host.account_snapshot = {
            "schema_version": 1, "source": "exchange.fetch_balance",
            "observed_at": host._clock().isoformat(),
            "exchange": host.exchange_id, "account": host.account_id,
            "market_type": host.market_type, "base_currency": host.base_currency,
            "cash": next_cash, "positions": deepcopy(positions),
            "raw_balance": deepcopy(balance),
            "capital_bridge_status": "unverified",
        }
        return next_cash, positions

    @staticmethod
    def _balance_number(value):
        if isinstance(value, bool) or value is None:
            raise ValueError("balance quantity is unavailable")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("balance quantity must be finite and nonnegative")
        return number

    def _sync_margin_account(self, balance):
        # Binance cross-margin userAssets are assets less principal and interest,
        # unlike derivatives position contracts. Never infer liabilities as zero.
        host = self._host
        assets = (balance.get("info") or {}).get("userAssets")
        if not isinstance(assets, list):
            raise ValueError("Binance margin assets/liabilities are unavailable")
        positions, facts, net_cash = {}, {}, 0.0
        for asset in assets:
            currency = asset["asset"]
            if currency in facts:
                raise ValueError("duplicate margin asset balance")
            free, locked, debt, interest = (float(asset[key]) for key in ("free", "locked", "borrowed", "interest"))
            if not all(math.isfinite(v) and v >= 0 for v in (free, locked, debt, interest)):
                raise ValueError("invalid margin balance fact")
            net = free + locked - debt - interest
            if "netAsset" in asset and abs(net - float(asset["netAsset"])) > 1e-8:
                raise ValueError("margin asset liability identity failed")
            facts[currency] = {"free": free, "locked": locked, "assets": free + locked,
                               "liabilities": debt, "interest": interest, "net": net}
            if currency == host.base_currency:
                net_cash = net
            elif abs(net) > 1e-12:
                symbol = f"{currency}/{host.base_currency}"
                lots = host.portfolio.open_lots(symbol)
                denom = sum(lot.qty_open for lot in lots)
                avg = sum(lot.entry_price * lot.qty_open for lot in lots) / denom if denom else 0.0
                positions[symbol] = {"qty": net, "avg_price": avg}
        if host.base_currency not in facts:
            raise ValueError("quote margin balance is unavailable")
        # Portfolio margin cash is capital plus realized PnL; convert net asset
        # balances to that representation so cash + unrealized equals NAV.
        cash = net_cash + sum(pos["qty"] * pos["avg_price"] for pos in positions.values())
        host.account_balance_facts = facts
        return cash, positions

    def _sync_spot_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        host = self._host
        positions: Dict[str, Dict[str, float]] = {}
        for currency, amount in (balance.get("total", {}) or {}).items():
            qty = host._balance_number(amount)
            if currency != host.base_currency and qty != 0:
                positions[f"{currency}/{host.base_currency}"] = {"qty": qty, "avg_price": 0.0}
        return positions

    def _sync_derivatives_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        host = self._host
        fetch_positions = getattr(host.exchange, "fetch_positions", None)
        info = balance.get("info", {}) if isinstance(balance, dict) else {}
        if callable(fetch_positions):
            # An explicit, even empty, response from the dedicated endpoint is
            # an authoritative fact: genuinely flat.
            raw_positions = fetch_positions()
        elif isinstance(balance, dict) and "positions" in balance:
            raw_positions = balance.get("positions")
        elif isinstance(info, dict) and "positions" in info:
            raw_positions = info.get("positions")
        else:
            # No source at all exposes derivative positions; treating this as
            # "flat" would let trading continue on an unverified fake empty
            # snapshot. Fail closed instead.
            raise ValueError(
                "exchange does not expose derivative positions via fetch_positions "
                "or balance payload; cannot verify account fact"
            )
        if not isinstance(raw_positions, list):
            raise ValueError("derivative position response is unavailable or malformed")
        positions: Dict[str, Dict[str, float]] = {}
        for raw in raw_positions:
            parsed = host.exchange_boundary.position_parser.parse(raw)
            if parsed is None:
                continue
            symbol = parsed.symbol
            if symbol in positions:
                raise ValueError(f"multiple derivative position legs are unsupported for {symbol}")
            positions[symbol] = {
                "qty": float(parsed.qty),
                "avg_price": float(parsed.average_entry_price),
            }
        return positions

    def _extract_qty(self, payload: Dict[str, Any]) -> float:
        host = self._host
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        direction = host._position_direction(payload, info)

        for value in (payload.get("positionAmt"), info.get("positionAmt")):
            qty = host._as_float(value)
            if qty == 0:
                continue
            if direction is not None and (qty > 0) != (direction > 0):
                raise ValueError("derivative position side conflicts with signed quantity")
            return qty

        for value in (
            payload.get("contracts"),
            payload.get("qty"),
            payload.get("size"),
            info.get("contracts"),
            info.get("qty"),
            info.get("size"),
        ):
            magnitude = abs(host._as_float(value))
            if magnitude == 0:
                continue
            if direction is None:
                raise ValueError("derivative position direction is unavailable")
            return magnitude * direction
        return 0.0

    @staticmethod
    def _position_direction(
        payload: Dict[str, Any], info: Dict[str, Any]
    ) -> Optional[float]:
        for value in (
            payload.get("side"),
            payload.get("positionSide"),
            info.get("side"),
            info.get("positionSide"),
        ):
            normalized = str(value or "").strip().lower()
            if normalized in {"long", "buy"}:
                return 1.0
            if normalized in {"short", "sell"}:
                return -1.0
        return None

    def _extract_avg_price(self, payload: Dict[str, Any]) -> float:
        host = self._host
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        for value in (payload.get("entryPrice"), payload.get("avgPrice"), payload.get("average"), info.get("entryPrice"), info.get("avgPrice")):
            price = host._as_float(value)
            if price > 0:
                return price
        return 0.0
