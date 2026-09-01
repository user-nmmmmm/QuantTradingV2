"""SR3 tests: ranking, correlated risk budgets and account cost semantics.

Covers docs/current_strategy_remediation_roadmap.md §13.3 for the parts SR3
implements: candidate ranking that is not a disguised alphabetical order,
correlation-cluster exposure and risk caps, spot-margin quote borrow, and the
account-mode / fee-schedule contract.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import pandas as pd

from core.account_cost_contract import (
    AccountCostContractError,
    canonical_runtime_account_mode,
    default_runtime_market_type,
    validate_account_cost_contract,
    validate_runtime_account_cost_contract,
)
from core.broker import Broker
from core.candidate_scoring import CandidateScorePolicy, score_breakout_candidate
from core.metrics_attribution import calculate_attribution
from core.phase4 import EntryCandidate, PortfolioSignalAllocator
from core.portfolio import Portfolio
from core.portfolio_risk import (
    CorrelationClusterPolicy,
    PortfolioRiskGovernor,
    exposure_by_cluster,
    open_risk_by_cluster,
)
from core.risk import RiskManager


class _StubStrategy:
    """Records the order in which the allocator submitted candidates."""

    def __init__(self, name: str, sink: list) -> None:
        self.name = name
        self._sink = sink

    def submit_entry_candidate(self, candidate, **kwargs):
        self._sink.append((candidate.strategy_name, candidate.symbol))
        return SimpleNamespace(accepted=True)


def _candidate(strategy, symbol: str, score: float) -> EntryCandidate:
    frame = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.date_range("2021-05-19", periods=2, freq="D", tz="UTC"),
    )
    return EntryCandidate(symbol, strategy, 1, frame, None, {"action": "buy"}, score)


class TestCandidateScoring(unittest.TestCase):
    def test_breakout_extent_dominates_a_marginal_poke(self):
        policy = CandidateScorePolicy()
        strong = score_breakout_candidate(
            reference_price=110.0, channel_level=100.0, atr=5.0, policy=policy,
        )
        weak = score_breakout_candidate(
            reference_price=100.5, channel_level=100.0, atr=5.0, policy=policy,
        )
        self.assertGreater(strong.total, weak.total)
        self.assertAlmostEqual(strong.components["breakout_extent"], 2.0)
        self.assertAlmostEqual(weak.components["breakout_extent"], 0.1)

    def test_components_are_comparable_across_price_levels(self):
        """A 2-ATR breakout scores the same on a $3 coin and a $60k coin."""
        policy = CandidateScorePolicy()
        cheap = score_breakout_candidate(
            reference_price=3.2, channel_level=3.0, atr=0.1, policy=policy,
        )
        rich = score_breakout_candidate(
            reference_price=64_000.0, channel_level=60_000.0, atr=2_000.0,
            policy=policy,
        )
        self.assertAlmostEqual(
            cheap.components["breakout_extent"],
            rich.components["breakout_extent"],
        )

    def test_short_side_scores_a_breakdown_positively(self):
        policy = CandidateScorePolicy()
        breakdown = score_breakout_candidate(
            reference_price=90.0, channel_level=100.0, atr=5.0,
            policy=policy, side="short",
        )
        self.assertAlmostEqual(breakdown.components["breakout_extent"], 2.0)

    def test_disabled_policy_scores_zero_without_pretending_otherwise(self):
        result = score_breakout_candidate(
            reference_price=110.0, channel_level=100.0, atr=5.0,
            policy=CandidateScorePolicy(enabled=False),
        )
        self.assertEqual(result.total, 0.0)
        self.assertEqual(result.components, {})


class TestAllocationOrdering(unittest.TestCase):
    def test_scores_decide_the_order_not_the_symbol_name(self):
        submitted: list = []
        strategy = _StubStrategy("TrendBreakout", submitted)
        allocator = PortfolioSignalAllocator()
        decisions = allocator.allocate(
            [
                _candidate(strategy, "ZEC/USDT", 3.0),
                _candidate(strategy, "AAVE/USDT", 1.0),
            ],
            portfolio=Portfolio(), broker=None, risk_manager=None,
            current_prices={},
        )
        self.assertEqual([symbol for _, symbol in submitted],
                         ["ZEC/USDT", "AAVE/USDT"])
        self.assertTrue(all(d.ordering == "score" for d in decisions))

    def test_an_all_zero_batch_is_reported_not_silently_alphabetical(self):
        """STR-P1-03: the tie-break may happen, but it must be visible."""
        submitted: list = []
        strategy = _StubStrategy("TrendBreakout", submitted)
        allocator = PortfolioSignalAllocator()
        with self.assertLogs("core.phase4", level="WARNING") as captured:
            decisions = allocator.allocate(
                [
                    _candidate(strategy, "ETH/USDT", 0.0),
                    _candidate(strategy, "BTC/USDT", 0.0),
                ],
                portfolio=Portfolio(), broker=None, risk_manager=None,
                current_prices={},
            )
        self.assertEqual(allocator.degenerate_batches, 1)
        self.assertTrue(all(d.ordering == "tie_break_alphabetical" for d in decisions))
        self.assertIn("degenerate", "\n".join(captured.output))

    def test_ordering_does_not_depend_on_the_input_sequence(self):
        """SR3 exit gate: the rank must come from the scores, not arrival order."""
        forward: list = []
        backward: list = []
        candidates = [
            ("BTC/USDT", 1.0), ("ETH/USDT", 3.0), ("SOL/USDT", 2.0),
        ]
        for sink, order in ((forward, candidates), (backward, candidates[::-1])):
            strategy = _StubStrategy("TrendBreakout", sink)
            PortfolioSignalAllocator().allocate(
                [_candidate(strategy, symbol, score) for symbol, score in order],
                portfolio=Portfolio(), broker=None, risk_manager=None,
                current_prices={},
            )
        self.assertEqual(forward, backward)
        self.assertEqual([symbol for _, symbol in forward],
                         ["ETH/USDT", "SOL/USDT", "BTC/USDT"])

    def test_a_single_candidate_is_not_flagged_as_degenerate(self):
        submitted: list = []
        strategy = _StubStrategy("TrendBreakout", submitted)
        allocator = PortfolioSignalAllocator()
        allocator.allocate(
            [_candidate(strategy, "BTC/USDT", 0.0)],
            portfolio=Portfolio(), broker=None, risk_manager=None,
            current_prices={},
        )
        self.assertEqual(allocator.degenerate_batches, 0)


class TestCorrelationClusters(unittest.TestCase):
    def _policy(self, **kwargs) -> CorrelationClusterPolicy:
        base = {
            "clusters": {"BTC": "major", "ETH": "major"},
            "default_cluster": "crypto_beta",
        }
        base.update(kwargs)
        return CorrelationClusterPolicy(**base)

    def test_unmapped_symbols_are_assumed_correlated(self):
        policy = self._policy()
        self.assertEqual(policy.cluster_for("BTC/USDT"), "major")
        self.assertEqual(policy.cluster_for("NEAR-USDT"), "crypto_beta")

    def test_exposure_and_open_risk_group_by_cluster(self):
        policy = self._policy()
        portfolio = Portfolio(initial_capital=100_000.0)
        timestamp = pd.Timestamp("2021-05-19T00:00:00Z")
        portfolio.update_position(
            "BTC/USDT", qty_delta=1.0, price=40_000.0, time=timestamp,
            strategy_id="TrendBreakout", order_id="O1", stop_price=38_000.0,
        )
        portfolio.update_position(
            "NEAR/USDT", qty_delta=100.0, price=5.0, time=timestamp,
            strategy_id="TrendBreakout", order_id="O2", stop_price=4.5,
        )
        prices = {"BTC/USDT": 40_000.0, "NEAR/USDT": 5.0}
        exposure = exposure_by_cluster(policy, portfolio, prices)
        self.assertAlmostEqual(exposure["major"], 40_000.0)
        self.assertAlmostEqual(exposure["crypto_beta"], 500.0)
        risk = open_risk_by_cluster(policy, portfolio)
        self.assertAlmostEqual(risk["major"], 2_000.0)
        self.assertAlmostEqual(risk["crypto_beta"], 50.0)

    def test_cluster_cap_reduces_the_allowed_notional(self):
        manager = RiskManager(max_leverage=3.0, max_pos_size_pct=1.0)
        manager.cluster_policy = self._policy(max_cluster_exposure_pct=1.0)
        portfolio = Portfolio(initial_capital=100_000.0)
        timestamp = pd.Timestamp("2021-05-19T00:00:00Z")
        portfolio.update_position(
            "BTC/USDT", qty_delta=2.0, price=40_000.0, time=timestamp,
            strategy_id="TrendBreakout", order_id="O1",
        )
        prices = {"BTC/USDT": 40_000.0, "ETH/USDT": 2_000.0}
        # 'major' already holds 80k of a 100k-equity cap: only 20k is left,
        # even though gross leverage would still allow far more.
        self.assertTrue(manager.check_entry_risk(
            portfolio, "ETH/USDT", 5.0, 2_000.0,
            current_prices=prices, action="buy",
        ))
        self.assertFalse(manager.check_entry_risk(
            portfolio, "ETH/USDT", 15.0, 2_000.0,
            current_prices=prices, action="buy",
        ))

    def test_crypto_beta_cap_binds_across_clusters(self):
        manager = RiskManager(max_leverage=3.0, max_pos_size_pct=1.0)
        manager.cluster_policy = self._policy(max_crypto_beta_exposure=1.0)
        portfolio = Portfolio(initial_capital=100_000.0)
        timestamp = pd.Timestamp("2021-05-19T00:00:00Z")
        portfolio.update_position(
            "BTC/USDT", qty_delta=2.0, price=40_000.0, time=timestamp,
            strategy_id="TrendBreakout", order_id="O1",
        )
        prices = {"BTC/USDT": 40_000.0, "NEAR/USDT": 5.0}
        # A different cluster does not create new capacity: they share the
        # crypto beta factor (STR-P1-04).
        self.assertFalse(manager.check_entry_risk(
            portfolio, "NEAR/USDT", 6_000.0, 5.0,
            current_prices=prices, action="buy",
        ))


class TestPortfolioRiskGovernor(unittest.TestCase):
    def _governor(self, **kwargs) -> PortfolioRiskGovernor:
        return PortfolioRiskGovernor(CorrelationClusterPolicy(**kwargs))

    def test_one_session_shares_a_single_entry_risk_budget(self):
        governor = self._governor(max_same_session_entry_risk=0.06)
        governor.begin_session("2021-05-19")
        portfolio = Portfolio(initial_capital=100_000.0)
        for _ in range(3):
            decision = governor.evaluate(
                symbol="BTC/USDT", planned_risk=2_000.0, equity=100_000.0,
                portfolio=portfolio,
            )
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.scale, 1.0)
            governor.commit(decision, symbol="BTC/USDT")
        # The 6% session budget is now spent: a fourth full-risk entry is not
        # simply "another independent 2%".
        blocked = governor.evaluate(
            symbol="ETH/USDT", planned_risk=2_000.0, equity=100_000.0,
            portfolio=portfolio,
        )
        self.assertFalse(blocked.allowed)
        self.assertIn("same_session_entry_risk", blocked.reason)

    def test_a_partially_affordable_entry_is_scaled_not_dropped(self):
        governor = self._governor(max_same_session_entry_risk=0.03)
        governor.begin_session("2021-05-19")
        portfolio = Portfolio(initial_capital=100_000.0)
        first = governor.evaluate(
            symbol="BTC/USDT", planned_risk=2_000.0, equity=100_000.0,
            portfolio=portfolio,
        )
        governor.commit(first, symbol="BTC/USDT")
        second = governor.evaluate(
            symbol="ETH/USDT", planned_risk=2_000.0, equity=100_000.0,
            portfolio=portfolio,
        )
        self.assertTrue(second.allowed)
        self.assertAlmostEqual(second.scale, 0.5)
        self.assertAlmostEqual(second.allowed_risk, 1_000.0)

    def test_a_new_session_restores_the_budget(self):
        governor = self._governor(max_same_session_entry_risk=0.02)
        portfolio = Portfolio(initial_capital=100_000.0)
        governor.begin_session("2021-05-19")
        first = governor.evaluate(
            symbol="BTC/USDT", planned_risk=2_000.0, equity=100_000.0,
            portfolio=portfolio,
        )
        governor.commit(first, symbol="BTC/USDT")
        governor.begin_session("2021-05-20")
        second = governor.evaluate(
            symbol="BTC/USDT", planned_risk=2_000.0, equity=100_000.0,
            portfolio=portfolio,
        )
        self.assertTrue(second.allowed)
        self.assertEqual(second.scale, 1.0)

    def test_correlated_stop_risk_counts_what_is_already_open(self):
        governor = self._governor(
            clusters={"BTC": "major", "ETH": "major"},
            max_correlated_stop_risk=0.05,
        )
        governor.begin_session("2021-05-19")
        portfolio = Portfolio(initial_capital=100_000.0)
        timestamp = pd.Timestamp("2021-05-19T00:00:00Z")
        # 4,000 of open initial risk already sits in the 'major' cluster.
        portfolio.update_position(
            "BTC/USDT", qty_delta=1.0, price=40_000.0, time=timestamp,
            strategy_id="TrendBreakout", order_id="O1", stop_price=36_000.0,
        )
        decision = governor.evaluate(
            symbol="ETH/USDT", planned_risk=2_000.0, equity=100_000.0,
            portfolio=portfolio,
        )
        self.assertTrue(decision.allowed)
        self.assertAlmostEqual(decision.allowed_risk, 1_000.0)
        self.assertIn("correlated_stop_risk", decision.reason)


class TestCorrelatedShockStress(unittest.TestCase):
    """SR3 exit gate: a correlated move through every stop stays inside budget."""

    def test_simultaneous_stop_out_of_one_cluster_respects_the_budget(self):
        policy = CorrelationClusterPolicy(
            clusters={"BTC": "major", "ETH": "major"},
            max_correlated_stop_risk=0.05,
            max_same_session_entry_risk=1.0,
        )
        governor = PortfolioRiskGovernor(policy)
        portfolio = Portfolio(initial_capital=100_000.0)
        equity = 100_000.0
        timestamp = pd.Timestamp("2021-05-19T00:00:00Z")
        opened = 0
        for index in range(10):
            governor.begin_session(f"2021-05-{19 + index:02d}")
            decision = governor.evaluate(
                symbol="ETH/USDT", planned_risk=2_000.0, equity=equity,
                portfolio=portfolio,
            )
            governor.commit(decision, symbol="ETH/USDT")
            if not decision.allowed or decision.allowed_risk <= 0:
                continue
            # Book the accepted risk as a real lot so the next evaluation sees
            # it in open_risk_by_cluster.
            qty = decision.allowed_risk / 100.0
            portfolio.update_position(
                "ETH/USDT", qty_delta=qty, price=2_000.0, time=timestamp,
                strategy_id="TrendBreakout", order_id=f"O{index}",
                stop_price=1_900.0,
            )
            opened += 1
        total_cluster_risk = open_risk_by_cluster(policy, portfolio)["major"]
        # The whole cluster stopping out at once costs no more than the
        # pre-registered 5% - not opened * 2% (STR-P1-04).
        self.assertLessEqual(total_cluster_risk, equity * 0.05 + 1e-6)
        self.assertGreater(opened, 1)


class TestSpotMarginQuoteBorrow(unittest.TestCase):
    def _broker(self) -> Broker:
        portfolio = Portfolio(
            initial_capital=100_000.0, account_mode="spot_margin",
            initial_margin_rate=0.3333333333,
        )
        return Broker(portfolio, default_borrow_rate_annual=0.08)

    def _bar(self, timestamp: str, close: float) -> pd.Series:
        bar = pd.Series({"open": close, "high": close, "low": close,
                         "close": close, "volume": 1e9})
        bar.name = pd.Timestamp(timestamp)
        return bar

    def test_leveraged_long_accrues_quote_borrow_interest(self):
        """STR-P1-05: gross above equity is margin debt, and debt costs."""
        broker = self._broker()
        portfolio = broker.portfolio
        portfolio.update_position(
            "BTC/USDT", qty_delta=3.0, price=50_000.0,
            time=pd.Timestamp("2021-05-18T00:00:00Z"),
            strategy_id="TrendBreakout", order_id="O1",
        )
        # 150k of longs against 100k equity -> 50k borrowed.
        broker.accrue_carry({"BTC/USDT": self._bar("2021-05-19T00:00:00Z", 50_000.0)})
        entries = broker.accrue_carry(
            {"BTC/USDT": self._bar("2021-05-20T00:00:00Z", 50_000.0)}
        )
        quote = [e for e in entries if e["kind"] == "quote_borrow"]
        self.assertEqual(len(quote), 1)
        self.assertAlmostEqual(quote[0]["notional"], 50_000.0)
        # 50,000 * 8% * (1/365) = 10.958904...
        self.assertAlmostEqual(quote[0]["amount"], 50_000.0 * 0.08 / 365.0, places=6)
        self.assertLess(portfolio.cash, 100_000.0)

    def test_unleveraged_long_accrues_nothing(self):
        broker = self._broker()
        broker.portfolio.update_position(
            "BTC/USDT", qty_delta=1.0, price=50_000.0,
            time=pd.Timestamp("2021-05-18T00:00:00Z"),
            strategy_id="TrendBreakout", order_id="O1",
        )
        broker.accrue_carry({"BTC/USDT": self._bar("2021-05-19T00:00:00Z", 50_000.0)})
        entries = broker.accrue_carry(
            {"BTC/USDT": self._bar("2021-05-20T00:00:00Z", 50_000.0)}
        )
        self.assertEqual([e for e in entries if e["kind"] == "quote_borrow"], [])


class _StubConfig:
    def __init__(self, payload):
        self._payload = payload

    def get(self, section, key=None):
        value = self._payload.get(section)
        return value if key is None else (value or {}).get(key)

    def require(self, section, key=None):
        value = self.get(section, key)
        if value is None:
            raise KeyError(section)
        return value


class TestAccountCostContract(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "execution": {
                "commission_rate_taker": 0.001,
                "commission_rate_maker": 0.001,
                "fee_schedule": {
                    "venue": "binance", "market_type": "spot_margin",
                    "source": "spot tier",
                },
            },
            "account": {
                "mode": "spot_margin",
                "default_borrow_rate_annual": 0.08,
                "funding_rate_required": True,
            },
        }
        for section, values in overrides.items():
            payload[section] = {**payload[section], **values}
        return payload

    def test_matching_mode_and_fee_schedule_validate(self):
        contract = validate_account_cost_contract(_StubConfig(self._payload()))
        self.assertEqual(contract.account_mode, "spot_margin")
        self.assertTrue(contract.borrow_modeled)

    def test_futures_fees_under_a_spot_margin_account_are_refused(self):
        payload = self._payload()
        payload["execution"]["fee_schedule"]["market_type"] = "perpetual"
        with self.assertRaisesRegex(AccountCostContractError, "incompatible"):
            validate_account_cost_contract(_StubConfig(payload))

    def test_spot_margin_without_a_borrow_rate_is_refused(self):
        payload = self._payload(account={"default_borrow_rate_annual": 0.0})
        with self.assertRaisesRegex(AccountCostContractError, "borrow"):
            validate_account_cost_contract(_StubConfig(payload))

    def test_perpetual_requires_mandatory_funding(self):
        payload = self._payload(
            account={"mode": "perpetual", "funding_rate_required": False},
        )
        payload["execution"]["fee_schedule"]["market_type"] = "perpetual"
        with self.assertRaisesRegex(AccountCostContractError, "funding"):
            validate_account_cost_contract(_StubConfig(payload))

    def test_a_missing_fee_schedule_is_refused(self):
        payload = self._payload()
        payload["execution"].pop("fee_schedule")
        with self.assertRaisesRegex(AccountCostContractError, "fee_schedule"):
            validate_account_cost_contract(_StubConfig(payload))

    def test_the_shipped_configuration_is_self_consistent(self):
        from config.config import config
        contract = validate_account_cost_contract(config)
        self.assertEqual(contract.account_mode, contract.market_type)

    def test_runtime_margin_alias_matches_spot_margin_contract(self):
        contract = validate_runtime_account_cost_contract(
            _StubConfig(self._payload()), market_type="margin",
        )
        self.assertEqual(contract.account_mode, "spot_margin")
        self.assertEqual(canonical_runtime_account_mode("margin"), "spot_margin")

    def test_runtime_spot_cannot_bypass_spot_margin_contract(self):
        with self.assertRaisesRegex(AccountCostContractError, "runtime market_type"):
            validate_runtime_account_cost_contract(
                _StubConfig(self._payload()), market_type="spot",
            )

    def test_runtime_derivative_aliases_resolve_to_perpetual(self):
        for value in ("future", "futures", "swap", "perpetual"):
            with self.subTest(value=value):
                self.assertEqual(canonical_runtime_account_mode(value), "perpetual")

    def test_configured_mode_has_a_matching_runtime_default(self):
        self.assertEqual(default_runtime_market_type("spot"), "spot")
        self.assertEqual(default_runtime_market_type("spot_margin"), "margin")
        self.assertEqual(default_runtime_market_type("perpetual"), "swap")


class TestControlAttribution(unittest.TestCase):
    def test_alpha_and_risk_overlay_split_reconciles_to_the_total(self):
        trades = [
            {"net_pnl": 100.0, "exit_reason": "signal"},
            {"net_pnl": -30.0, "exit_reason": "hard_stop"},
            {"net_pnl": 500.0, "exit_reason": "DailyLossLimit"},
            {"net_pnl": 20.0, "exit_reason": "MaxHoldingPeriod"},
        ]
        result = calculate_attribution(trades)
        control = result["control_attribution"]
        self.assertAlmostEqual(control["alpha_only"], 70.0)
        self.assertAlmostEqual(control["risk_overlay"], 500.0)
        self.assertAlmostEqual(control["router_and_system"], 20.0)
        self.assertAlmostEqual(control["combined"], 590.0)
        self.assertTrue(control["reconciles"])
        self.assertAlmostEqual(control["risk_overlay_share"], 500.0 / 590.0)

    def test_trade_counts_are_reported_per_controller(self):
        trades = [
            {"net_pnl": 1.0, "exit_reason": "signal"},
            {"net_pnl": 1.0, "exit_reason": "DailyLossLimit"},
            {"net_pnl": 1.0, "exit_reason": "DailyLossLimit"},
        ]
        counts = calculate_attribution(trades)["trade_count_by_exit_controller"]
        self.assertEqual(counts["strategy"], 1)
        self.assertEqual(counts["account_risk"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
