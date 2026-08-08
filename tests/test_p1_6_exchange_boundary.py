import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from core.domain import OrderErrorCode, OrderIntent, OrderStatus
from core.exchange_boundary import (
    CCXTRequestMapper,
    CanonicalPosition,
    ExchangeBoundary,
    ExchangeCapabilities,
    MarketMetadataLoader,
    MarketSpecification,
    MetadataChangeAction,
    MetadataChangeHaltPolicy,
    MetadataChangedError,
    MetadataUnavailableError,
    OrderNormalizer,
    OrderParser,
    OrderValidationError,
    OrderValidator,
    PositionParser,
)
from core.live_broker import LiveBroker
from core.portfolio import Portfolio


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def intent(**changes):
    values = dict(
        exchange="venue",
        account="spot",
        symbol="BTC/USDT",
        timeframe="1m",
        bar_time=NOW.isoformat(),
        strategy_id="contract",
        action="buy",
        sequence=1,
        requested_qty=Decimal("0.01009"),
        order_type="limit",
        price=Decimal("50000.129"),
        time_in_force="gtc",
    )
    values.update(changes)
    return OrderIntent(**values)


def market(**changes):
    values = dict(
        symbol="BTC/USDT",
        market_type="spot",
        amount_step=Decimal("0.001"),
        price_step=Decimal("0.1"),
        min_amount=Decimal("0.01"),
        max_amount=Decimal("10"),
        min_notional=Decimal("10"),
        contract_size=Decimal("1"),
    )
    values.update(changes)
    return MarketSpecification(**values)


def raw_market(**changes):
    values = {
        "symbol": "BTC/USDT",
        "type": "spot",
        "active": True,
        "precision": {"amount": 0.001, "price": 0.1},
        "limits": {
            "amount": {"min": 0.01, "max": 10},
            "price": {"min": 1, "max": 1000000},
            "cost": {"min": 10, "max": None},
        },
        "base": "BTC",
        "quote": "USDT",
    }
    values.update(changes)
    return values


class FakeExchange:
    id = "venue"
    precisionMode = 4
    has = {
        "createMarketOrder": True,
        "createLimitOrder": True,
        "fetchPositions": True,
    }
    options = {}

    def __init__(self, markets=None):
        self.markets = markets or {"BTC/USDT": raw_market()}
        self.load_calls = 0

    def load_markets(self, reload=False):
        self.load_calls += 1
        return self.markets


class TestExchangeCapabilitiesAndMarketSpecification(unittest.TestCase):
    def test_ccxt_details_are_translated_to_canonical_capabilities(self):
        exchange = FakeExchange()
        capabilities = ExchangeCapabilities.from_ccxt(exchange)

        self.assertEqual(capabilities.exchange_id, "venue")
        self.assertEqual(capabilities.order_types, {"market", "limit"})
        self.assertTrue(capabilities.supports_fetch_positions)

    def test_market_specification_extracts_precision_limits_and_contract(self):
        spec = MarketSpecification.from_ccxt(
            raw_market(type="swap", contractSize="0.001", settle="USDT"), 4
        )

        self.assertEqual(spec.amount_step, Decimal("0.001"))
        self.assertEqual(spec.price_step, Decimal("0.1"))
        self.assertEqual(spec.min_notional, Decimal("10"))
        self.assertEqual(spec.contract_size, Decimal("0.001"))
        self.assertTrue(spec.is_derivative)


class TestMetadataLoaderCacheAndVersion(unittest.TestCase):
    def test_cache_has_stable_version_and_obeys_ttl(self):
        exchange = FakeExchange()
        current = [NOW]
        loader = MarketMetadataLoader(
            exchange, ttl=timedelta(minutes=5), clock=lambda: current[0]
        )

        first = loader.load()
        second = loader.load()
        current[0] += timedelta(minutes=6)
        third = loader.load()

        self.assertIs(first, second)
        self.assertEqual(first.version, third.version)
        self.assertEqual(exchange.load_calls, 2)

    def test_metadata_change_halts_until_operator_acknowledges(self):
        exchange = FakeExchange()
        loader = MarketMetadataLoader(exchange, ttl=timedelta(0), clock=lambda: NOW)
        first = loader.load()
        exchange.markets = {
            "BTC/USDT": raw_market(
                limits={
                    "amount": {"min": 0.02, "max": 10},
                    "price": {"min": 1, "max": 1000000},
                    "cost": {"min": 10, "max": None},
                }
            )
        }

        with self.assertRaisesRegex(MetadataChangedError, "trading halted"):
            loader.load()
        self.assertTrue(loader.halted)
        adopted = loader.acknowledge_change()

        self.assertFalse(loader.halted)
        self.assertNotEqual(first.version, adopted.version)

    def test_accept_policy_can_roll_version_without_halt(self):
        exchange = FakeExchange()
        loader = MarketMetadataLoader(
            exchange,
            ttl=timedelta(0),
            clock=lambda: NOW,
            change_policy=MetadataChangeHaltPolicy(MetadataChangeAction.ACCEPT),
        )
        first = loader.load()
        exchange.markets = {"BTC/USDT": raw_market(active=False)}

        second = loader.load()

        self.assertNotEqual(first.version, second.version)
        self.assertFalse(loader.halted)


class TestValidatorNormalizerAndMapper(unittest.TestCase):
    def setUp(self):
        self.capabilities = ExchangeCapabilities("venue")
        self.validator = OrderValidator()

    def test_validator_rejects_known_illegal_orders(self):
        cases = (
            (intent(requested_qty=0), "quantity"),
            (intent(order_type="stop"), "order type"),
            (intent(order_type="limit", price=None), "requires a price"),
            (intent(time_in_force="DAY"), "time in force"),
            (intent(requested_qty=Decimal("0.001")), "below minimum"),
            (intent(requested_qty=Decimal("11")), "exceeds maximum"),
            (intent(price=Decimal("100"), requested_qty=Decimal("0.01")), "notional"),
        )
        for command, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(OrderValidationError, message):
                    self.validator.validate(command, self.capabilities, market())

    def test_normalizer_rounds_down_without_changing_business_identity(self):
        original = intent()
        normalized = OrderNormalizer().normalize(original, market())

        self.assertEqual(normalized.requested_qty, 0.01)
        self.assertEqual(normalized.price, 50000.1)
        self.assertEqual(normalized.time_in_force, "GTC")
        self.assertEqual(normalized.client_order_id, original.client_order_id)

    def test_request_mapper_contains_all_venue_translation(self):
        command = intent(
            action="short", reduce_only=True, position_side="SHORT",
            position_mode="hedge",
        )
        capabilities = replace(
            self.capabilities,
            supports_reduce_only=True,
            supports_hedge_mode=True,
        )

        request = CCXTRequestMapper().map(command, capabilities)

        self.assertEqual(request.side, "sell")
        self.assertEqual(request.params["clientOrderId"], command.client_order_id)
        self.assertTrue(request.params["reduceOnly"])
        self.assertEqual(request.params["positionSide"], "SHORT")
        self.assertEqual(request.params["timeInForce"], "GTC")


class TestOrderAndPositionParsers(unittest.TestCase):
    def test_order_parser_canonicalizes_exchange_status_and_numbers(self):
        parsed = OrderParser().parse(
            {
                "id": 42,
                "clientOrderId": "client-1",
                "symbol": "BTC/USDT",
                "status": "open",
                "amount": "2",
                "filled": "0.5",
                "remaining": "1.5",
                "average": "50100.5",
            }
        )

        self.assertEqual(parsed.exchange_order_id, "42")
        self.assertEqual(parsed.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(parsed.filled_qty, Decimal("0.5"))

    def test_position_parser_handles_signed_and_side_magnitude_formats(self):
        parser = PositionParser()
        signed = parser.parse(
            {"symbol": "BTC/USDT", "positionAmt": "-2", "entryPrice": "50000"}
        )
        magnitude = parser.parse(
            {"symbol": "ETH/USDT", "contracts": "3", "side": "long", "entryPrice": 3000}
        )

        self.assertEqual(
            signed,
            CanonicalPosition("BTC/USDT", Decimal("-2"), Decimal("50000")),
        )
        self.assertEqual(magnitude.qty, Decimal("3"))
        with self.assertRaisesRegex(ValueError, "direction"):
            parser.parse({"symbol": "X/USDT", "contracts": "1"})


class TestExchangeContract(unittest.TestCase):
    def test_two_venues_produce_same_domain_validation_and_only_mapper_differs(self):
        exchange_a = FakeExchange()
        exchange_b = FakeExchange()
        caps_a = ExchangeCapabilities("a", supports_client_order_id=True)
        caps_b = ExchangeCapabilities("b", supports_client_order_id=False)
        boundary_a = ExchangeBoundary(
            caps_a, MarketMetadataLoader(exchange_a, "a", clock=lambda: NOW)
        )
        boundary_b = ExchangeBoundary(
            caps_b, MarketMetadataLoader(exchange_b, "b", clock=lambda: NOW)
        )
        command = intent()

        prepared_a = boundary_a.prepare(command)
        prepared_b = boundary_b.prepare(command)

        self.assertEqual(prepared_a.intent, prepared_b.intent)
        self.assertEqual(prepared_a.market, prepared_b.market)
        self.assertIn("clientOrderId", prepared_a.request.params)
        self.assertNotIn("clientOrderId", prepared_b.request.params)

    def test_live_broker_rejects_before_create_order_and_normalizes_valid_request(self):
        exchange = MagicMock()
        exchange.id = "binance"
        exchange.has = {"createMarketOrder": True, "createLimitOrder": True}
        exchange.options = {}
        exchange.precisionMode = 4
        exchange.load_markets.return_value = {"BTC/USDT": raw_market()}
        exchange.create_order.return_value = {
            "id": "e1", "status": "open", "amount": 0.01,
            "filled": 0, "remaining": 0.01,
        }

        with patch("core.live_broker.ccxt") as ccxt_mock:
            ccxt_mock.binance.return_value = exchange
            broker = LiveBroker(
                Portfolio(), clock=lambda: NOW, require_market_metadata=True
            )

            rejected = broker.submit_order(
                "BTC/USDT", "buy", 0.001, 50000, order_type="limit"
            )
            self.assertEqual(rejected.status, OrderStatus.REJECTED)
            self.assertEqual(rejected.error_code, OrderErrorCode.TRADING_RULE)
            exchange.create_order.assert_not_called()

            broker.set_bar_context("1m", NOW.replace(minute=1))
            accepted = broker.submit_order(
                "BTC/USDT", "buy", 0.01009, 50000.129, order_type="limit"
            )
            self.assertEqual(accepted.status, OrderStatus.ACCEPTED)
            call = exchange.create_order.call_args.kwargs
            self.assertEqual(call["amount"], 0.01)
            self.assertEqual(call["price"], 50000.1)
            broker.close()

    def test_strict_boundary_fails_closed_when_metadata_is_missing(self):
        exchange = FakeExchange(markets={})
        exchange.markets = {}
        boundary = ExchangeBoundary(
            ExchangeCapabilities("venue"),
            MarketMetadataLoader(exchange, clock=lambda: NOW),
            require_metadata=True,
        )

        with self.assertRaises(MetadataUnavailableError):
            boundary.prepare(intent())


if __name__ == "__main__":
    unittest.main()
