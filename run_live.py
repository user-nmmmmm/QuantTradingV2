import argparse
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.portfolio import Portfolio
from core.logger import configure_logging, get_logger
from core.live_safety import (
    SafetyConfigurationError,
    StartupSafetyPolicy,
    credentials_from_environment,
    verify_live_permissions,
)
from core.persistent_risk_guard import PersistentOrderSafetyGuard
from core.safe_live_broker import SafeLiveBroker
from core.system_factory import build_risk_manager, build_strategy_registry
from live_trading.engine import LiveTradingEngine

configure_logging()
logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QuantTrading exchange engine")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--interval", type=int, default=60)
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
        "--market-type", default="spot",
        choices=["spot", "future", "futures", "swap", "margin"],
    )
    parser.add_argument("--base-currency", default="USDT")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

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

    portfolio = Portfolio()
    risk_manager = build_risk_manager()
    safety_guard = PersistentOrderSafetyGuard(policy)
    broker = SafeLiveBroker(
        portfolio=portfolio,
        exchange_id=args.exchange,
        sandbox=not args.live,
        market_type=args.market_type,
        base_currency=args.base_currency,
        safety_guard=safety_guard,
    )
    if args.live:
        try:
            verify_live_permissions(broker.exchange, args.market_type)
        except SafetyConfigurationError as exc:
            safety_guard.close()
            parser.error(str(exc))

    logger.info(
        "Startup checks passed: mode=%s exchange=%s account_type=%s symbols=%s base_currency=%s",
        "LIVE" if args.live else "SANDBOX",
        args.exchange,
        args.market_type,
        len(args.symbols),
        args.base_currency,
    )
    engine = LiveTradingEngine(
        symbols=args.symbols,
        strategies=build_strategy_registry(),
        broker=broker,
        risk_manager=risk_manager,
        interval_seconds=args.interval,
    )
    try:
        engine.initialize()
        engine.run()
    finally:
        broker.close()
        safety_guard.close()


if __name__ == "__main__":
    main()
