import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from composition.factory import build_risk_manager, build_strategy_registry
from config.config import config
from core.portfolio import Portfolio
from core.order_store import OrderStore
from core.state_store_v2 import StateStore
from core.runtime_identity import RuntimeIdentity
from core.logger import configure_logging, get_logger
from core.live_safety import (
    SafetyConfigurationError,
    StartupSafetyPolicy,
    credentials_from_environment,
    verify_live_permissions,
)
from core.risk.persistent_guard import PersistentOrderSafetyGuard
from core.gray_release import GrayReleasePolicy, write_release_record
from core.account_cost_contract import (
    AccountCostContractError,
    default_runtime_market_type,
    validate_runtime_account_cost_contract,
)
from core.startup_preflight import build_startup_report, write_startup_report
from core.strategy_governance import (
    GovernanceError,
    assert_live_admission,
    routed_strategy_names,
)
from core.live_broker.safe import SafeLiveBroker
from live_trading.engine import LiveTradingEngine

configure_logging()
logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QuantTrading exchange engine")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--account-id", default=os.getenv("QUANT_ACCOUNT_ID"),
                        help="Non-secret stable account identifier for state isolation")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--sandbox", dest="live", action="store_false",
        help="Use the exchange sandbox/testnet (default)",
    )
    mode.add_argument(
        "--live", dest="live", action="store_true",
        help="Explicitly enable the real-money exchange endpoint",
    )
    parser.set_defaults(live=False)
    parser.add_argument("--exchange", default="binance")
    parser.add_argument(
        "--market-type", default=None,
        choices=["spot", "future", "futures", "swap", "margin"],
    )
    parser.add_argument("--base-currency", default="USDT")
    parser.add_argument(
        "--preflight-only", action="store_true",
        help="Connect, write the startup report, and exit before the main loop",
    )
    parser.add_argument(
        "--preflight-report", default="reports/startup_preflight.json",
    )
    parser.add_argument(
        "--r8-evidence",
        help="passed R7 or Phase 6 admission JSON (required for --live)",
    )
    parser.add_argument("--rollback-snapshot", help="validated state snapshot (required for --live)")
    parser.add_argument("--r8-max-order-notional", type=float)
    parser.add_argument("--r8-max-daily-risk", type=float)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.market_type is None:
        try:
            args.market_type = default_runtime_market_type(
                config.require("account", "mode")
            )
        except AccountCostContractError as exc:
            parser.error(str(exc))

    credentials = credentials_from_environment()
    if not credentials.get("apiKey") or not credentials.get("secret"):
        parser.error(
            "exchange credentials must be provided through environment variables "
            "or an external secret manager"
        )
    try:
        policy = StartupSafetyPolicy.from_environment(
            sandbox=not args.live,
            exchange_id=args.exchange,
            account_type=args.market_type,
            symbols=args.symbols,
            base_currency=args.base_currency,
        )
    except SafetyConfigurationError as exc:
        parser.error(str(exc))

    # SR3-4: refuse to trade a cost model that does not match the account.
    try:
        validate_runtime_account_cost_contract(
            config, market_type=args.market_type,
        )
    except AccountCostContractError as exc:
        parser.error(str(exc))

    if not args.account_id:
        parser.error("--account-id or QUANT_ACCOUNT_ID is required for account state isolation")
    identity = RuntimeIdentity(args.exchange, "live" if args.live else "sandbox", args.account_id, args.market_type)
    identity.directory.mkdir(parents=True, exist_ok=True)
    account = config.get("account") or {}
    portfolio = Portfolio(account_mode=account["mode"],
                          initial_margin_rate=account.get("initial_margin_rate", 1.0),
                          maintenance_margin_rate=account.get("maintenance_margin_rate", 0.05))
    risk_manager = build_risk_manager(config)
    safety_guard = PersistentOrderSafetyGuard(policy, str(identity.directory / "safety.db"), identity=identity)
    from core.alerting import build_default_alert_sink
    from core.logger import get_logger
    broker = SafeLiveBroker(
        portfolio=portfolio,
        exchange_id=args.exchange,
        sandbox=not args.live,
        market_type=args.market_type,
        base_currency=args.base_currency,
        safety_guard=safety_guard,
        alert_sink=build_default_alert_sink(get_logger("live_runtime"), record_path=str(identity.directory / "alerts.jsonl")),
        require_market_metadata=True,
        require_resident_protection=True,
        account_id=identity.key,
        order_store=OrderStore(str(identity.directory / "orders.db"), identity=identity),
    )
    if args.live:
        # SR0-2: a strategy without current admission evidence may run in
        # research/shadow/sandbox, never with real money.
        try:
            assert_live_admission(config, routed_strategy_names(config))
        except GovernanceError as exc:
            safety_guard.close()
            parser.error(str(exc))
        try:
            if not all((args.r8_evidence, args.rollback_snapshot, args.r8_max_order_notional, args.r8_max_daily_risk)):
                raise SafetyConfigurationError("--live requires R8 evidence, rollback snapshot, and explicit risk caps")
            gray_policy = GrayReleasePolicy(
                args.exchange,
                args.symbols[0] if len(args.symbols) == 1 else "",
                args.r8_max_order_notional,
                args.r8_max_daily_risk,
                args.r8_evidence,
                args.rollback_snapshot,
                runtime_identity=identity,
            )
            gray_policy.validate(policy, broker.exchange)
            write_release_record(
                "reports/r8_release.json",
                gray_policy,
                {
                    "r7_evidence": args.r8_evidence,
                    "rollback_snapshot": args.rollback_snapshot,
                },
            )
        except SafetyConfigurationError as exc:
            safety_guard.close()
            parser.error(str(exc))

    logger.info(
        "Startup policy configured: mode=%s exchange=%s account_type=%s symbols=%s base_currency=%s",
        "LIVE" if args.live else "SANDBOX",
        args.exchange,
        args.market_type,
        len(args.symbols),
        args.base_currency,
    )
    engine = LiveTradingEngine(
        symbols=args.symbols,
        strategies=build_strategy_registry(config),
        broker=broker,
        risk_manager=risk_manager,
        configuration=config,
        interval_seconds=args.interval,
        state_file=str(identity.directory / "live_status.json"),
        state_store=StateStore(str(identity.directory / "state.db"), identity=identity),
        reconciliation_interval_seconds=config.require(
            "execution", "reconciliation_interval_seconds"
        ),
        strategy_failure_threshold=config.require(
            "execution", "strategy_failure_threshold"
        ),
        state_export_interval_ticks=config.require(
            "execution", "state_export_interval_ticks"
        ),
    )
    try:
        engine.initialize()
        report = build_startup_report(policy, credentials, engine)
        report_path = write_startup_report(report, args.preflight_report)
        if not report["ok"]:
            logger.critical(
                "Startup health baseline failed; report=%s reason_codes=%s",
                report_path,
                ",".join(report["health_reason_codes"]) or "STARTUP_CHECK_FAILED",
            )
            return 2
        logger.info("Startup health baseline passed; report=%s", report_path)
        if args.preflight_only:
            logger.info("Preflight-only run completed; main loop was not started")
            return 0
        engine.run()
        return 0
    finally:
        broker.close()
        safety_guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
