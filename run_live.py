import argparse
import os
import sys
import hashlib
import json
from pathlib import Path

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
from core.gray_release import (
    AdmissionBoundOrderGuard, GrayReleasePolicy, read_pinned_admission_evidence, write_release_record,
)
from core.account_cost_contract import (
    AccountCostContractError,
    default_runtime_market_type,
    validate_runtime_account_cost_contract,
)
from core.startup_preflight import build_startup_report, write_startup_report
from core.account_source import PinnedAccountExport
from live_trading.recovery import account_new_risk_gate
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
    parser.add_argument("--account-facts", help="Read-only independent full-account export JSON")
    parser.add_argument("--account-facts-sha256", help="Externally pinned SHA-256 of the account export")
    parser.add_argument("--account-facts-source-id", help="Externally expected account export source identifier")
    parser.add_argument(
        "--preflight-only", action="store_true",
        help="Connect, write the startup report, and exit before the main loop",
    )
    parser.add_argument(
        "--preflight-report", default="reports/startup_preflight.json",
    )
    parser.add_argument(
        "--r8-evidence",
        help="raw r8-admission-evidence/v1 envelope (required for --live)",
    )
    parser.add_argument("--r8-evidence-sha256", help="Externally pinned SHA-256 of the complete R8 evidence file")
    parser.add_argument("--rollback-snapshot", help="validated state snapshot (required for --live)")
    parser.add_argument("--r8-max-order-notional", type=float)
    parser.add_argument("--r8-max-daily-risk", type=float)
    return parser


def account_source_from_args(args, parser):
    """Validate the entire read-only source group before credentials/network.

    Pinning is integrity validation, not independent collection attestation.
    The CLI has no flag that manufactures production provenance; a deployment
    must supply a trusted source adapter/verifier through the broker API.
    """
    values = (args.account_facts, args.account_facts_sha256, args.account_facts_source_id)
    if not any(value is not None for value in values):
        return None
    if not all(values):
        parser.error("--account-facts, --account-facts-sha256 and --account-facts-source-id must be provided together")
    try:
        source = PinnedAccountExport(args.account_facts, sha256=args.account_facts_sha256,
                                    source_id=args.account_facts_source_id)
        # File access is local and read-only. Identity/full source verification
        # happens inside broker reconciliation after its runtime identity exists.
        import hashlib
        if hashlib.sha256(source.path.read_bytes()).hexdigest() != source.sha256:
            raise ValueError("account export SHA-256 mismatch")
        return source
    except (OSError, ValueError) as exc:
        parser.error("invalid read-only account facts: " + str(exc))


def protection_only_startup_allowed(report):
    """A missing account proof must not disable known-position protection."""
    return (
        set(report.get("health_reason_codes", [])) == {"ACCOUNT_FACTS_UNVERIFIED"}
        and all(check.get("passed") is True for check in report.get("checks", [])
                if check.get("name") != "health_baseline")
        and bool(report.get("checks"))
    )


def current_release_identity(configuration=config):
    """Compute expected identity from actual controlled source and loaded configuration."""
    from config.config import ConfigLoader
    from scripts.roadmap_baseline import source_manifest, verify_source

    root = Path(__file__).resolve().parent
    manifest = source_manifest(root)
    config_path = (root / "config/params.yaml").resolve()
    if Path(configuration.config_path).resolve() != config_path:
        raise SafetyConfigurationError("R8 requires the authoritative runtime configuration path")
    actual_configuration = ConfigLoader(str(config_path))
    if actual_configuration._config != configuration._config:
        raise SafetyConfigurationError("loaded runtime configuration differs from the pinned file")
    strategies = routed_strategy_names(configuration)
    if len(strategies) != 1:
        raise SafetyConfigurationError("R8 requires exactly one routed non-Cash strategy")
    verify_source(root, manifest)
    return {"source_sha256": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            "config_sha256": manifest["config/params.yaml"], "strategy": strategies[0]}


def gray_policy_from_args(args, startup, identity, configuration=config):
    """Bind the review to independently computed expectations before creating a broker."""
    if not all((args.r8_evidence, args.r8_evidence_sha256, args.rollback_snapshot,
                args.r8_max_order_notional, args.r8_max_daily_risk)):
        raise SafetyConfigurationError("--live requires pinned R8 evidence, rollback snapshot, and explicit risk caps")
    assert_live_admission(configuration, routed_strategy_names(configuration))
    expected = current_release_identity(configuration)
    result = GrayReleasePolicy(
        args.exchange, args.symbols[0] if len(args.symbols) == 1 else "",
        args.r8_max_order_notional, args.r8_max_daily_risk, args.r8_evidence, args.rollback_snapshot,
        runtime_identity=identity, evidence_sha256=args.r8_evidence_sha256, **expected,
    )
    result.validate_evidence(startup)
    return result


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    account_source = account_source_from_args(args, parser)
    if args.live:
        try:
            read_pinned_admission_evidence(args.r8_evidence, args.r8_evidence_sha256)
        except SafetyConfigurationError as exc:
            parser.error(str(exc))
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
    gray_policy = None
    if args.live:
        try:
            gray_policy = gray_policy_from_args(args, policy, identity)
        except (SafetyConfigurationError, GovernanceError, OSError, ValueError) as exc:
            parser.error(str(exc))
    identity.directory.mkdir(parents=True, exist_ok=True)
    account = config.get("account") or {}
    portfolio = Portfolio(account_mode=account["mode"],
                          initial_margin_rate=account.get("initial_margin_rate", 1.0),
                          maintenance_margin_rate=account.get("maintenance_margin_rate", 0.05))
    risk_manager = build_risk_manager(config)
    safety_guard = PersistentOrderSafetyGuard(policy, str(identity.directory / "safety.db"), identity=identity)
    if gray_policy is not None:
        safety_guard = AdmissionBoundOrderGuard(safety_guard, gray_policy, current_release_identity)
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
    if account_source is not None:
        broker.configure_account_source(account_source, environment=identity.environment)
    if args.live:
        try:
            if current_release_identity() != {"source_sha256": gray_policy.source_sha256,
                                              "config_sha256": gray_policy.config_sha256,
                                              "strategy": gray_policy.strategy}:
                raise SafetyConfigurationError("R8 source or configuration changed during startup")
            verification = gray_policy.validate(policy, broker.exchange)
            write_release_record(
                "reports/r8_release.json",
                gray_policy,
                {
                    "r7_evidence": args.r8_evidence,
                    "rollback_snapshot": args.rollback_snapshot,
                    "admission_verification": verification,
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
        engine._run_reconciliation_if_due(engine._now(), force=True)
        report = build_startup_report(policy, credentials, engine)
        report["account_reconciliation"] = engine._reconciliation_status.get("account_reconciliation")
        report["account_entry_gate"] = account_new_risk_gate(broker, engine._now(),
            persistence_failed=getattr(engine, "_account_reconciliation_persistence_failed", False))
        report["allows_new_risk"] = report["ok"] and report["account_entry_gate"]["allows_new_risk"]
        report["protection_only_startup_allowed"] = protection_only_startup_allowed(report)
        report_path = write_startup_report(report, args.preflight_report)
        if not report["ok"]:
            logger.critical(
                "Startup health baseline failed; report=%s reason_codes=%s",
                report_path,
                ",".join(report["health_reason_codes"]) or "STARTUP_CHECK_FAILED",
            )
            if args.preflight_only or not report["protection_only_startup_allowed"]:
                return 2
            logger.warning("Starting protection-only loop; independent account evidence blocks new risk")
        else:
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
