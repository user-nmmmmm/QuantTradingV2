import unittest
from datetime import datetime, timezone

import pandas as pd

from core.events import FillEvent, OrderEvent, TradingEventPipeline
from core.domain import OrderStatus
from core.market_data import HistoricalMarketDataAdapter, LiveMarketDataAdapter
from core.portfolio import Portfolio
from core.runtime import EventProcessor
from live_trading.execution_adapter import RecordedExecutionAdapter


def bars():
    index = pd.date_range("2024-01-01", periods=3, freq="D")
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.0, 101.0, 102.0],
            "volume": [10.0, 10.0, 10.0],
        },
        index=index,
    )


class Fetcher:
    def __init__(self, frame):
        self.frame = frame

    def fetch_ccxt(self, *_args, **_kwargs):
        return self.frame.copy()


class Risk:
    def reset_daily_breaker(self):
        pass

    def check_circuit_breaker(self, *_args):
        return False


class StateMachine:
    def get_state(self, _df, i):
        return i


class Execution:
    def __init__(self):
        self.portfolio = Portfolio(1000)
        self.market_times = []

    def on_market_data(self, event):
        self.market_times.append(event.timestamp)


class Router:
    def __init__(self):
        self.calls = []

    def collect_candidate(self, symbol, i, df, state, *_args):
        self.calls.append((symbol, i, state, float(df["close"].iloc[i])))
        return None


class Allocator:
    def allocate(self, *_args, **_kwargs):
        return []


class Broker:
    def __init__(self):
        self.portfolio = Portfolio(1000)
        self.event_pipeline = TradingEventPipeline(run_id="recorded")
        self.submissions = 0

    def submit_intent(self, _intent):
        self.submissions += 1


class TestSharedRuntimeGoldenScenario(unittest.TestCase):
    def test_historical_and_live_adapters_feed_identical_business_path(self):
        frame = bars()
        historical = HistoricalMarketDataAdapter(
            {"BTC/USDT": frame}, timeframe="1d"
        )
        live = LiveMarketDataAdapter(
            ["BTC/USDT"], Fetcher(frame), timeframe="1d", close_grace_seconds=0
        )
        live_events = live.poll(datetime(2024, 1, 5, tzinfo=timezone.utc))

        outputs = []
        for events in (list(historical.stream()), live_events):
            execution = Execution()
            router = Router()
            processor = EventProcessor(
                portfolio=execution.portfolio,
                execution=execution,
                risk_manager=Risk(),
                state_machine=StateMachine(),
                router=router,
                allocator=Allocator(),
            )
            for event in events:
                processor.process(event)
            outputs.append(router.calls)

        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(len(outputs[0]), 3)


class TestRecordedExecutionReplay(unittest.TestCase):
    def test_order_and_fill_facts_replay_without_resubmission(self):
        source = TradingEventPipeline(run_id="source")
        order = source.publish(
            OrderEvent(
                client_order_id="order-1",
                status=OrderStatus.FILLED,
                requested_qty=2,
                filled_qty=2,
                remaining_qty=0,
                average_fill_price=100,
            ),
            occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            source="exchange",
        )
        fill = source.publish(
            FillEvent(
                fill_id="fill-1",
                client_order_id="order-1",
                symbol="BTC/USDT",
                side="buy",
                qty=2,
                price=100,
                fee=1,
            ),
            occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            source="exchange",
        )
        broker = Broker()
        adapter = RecordedExecutionAdapter(broker)
        replayed = adapter.replay([order, fill, fill], apply_fills=True)

        self.assertEqual(len(replayed), 3)
        self.assertEqual(broker.submissions, 0)
        self.assertEqual(broker.portfolio.get_position("BTC/USDT")["qty"], 2)
        self.assertEqual(len(adapter.events), 2)


if __name__ == "__main__":
    unittest.main()
