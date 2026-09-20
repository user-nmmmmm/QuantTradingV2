"""S2 local engineering fixtures; deliberately synthetic, no alpha conclusion."""

from dataclasses import replace

import pytest

from core.selection import (
    DataProvenance, ExecutionQuote, FactorObservation, MembershipFact,
    RebalancePolicy, SelectionPolicy, plan_rebalance, select_targets,
    selection_input_digest,
)


DAY = "2024-02-01T00:00:00Z"
NEXT = "2024-02-01T01:00:00Z"


def listing(symbol, date="2024-01-01T00:00:00Z"):
    return MembershipFact(symbol, "listed", date, date)


def observation(symbol, score=1., *, observed=DAY, available=DAY, volume=1000.):
    return FactorObservation(symbol, observed, available, score, volume)


def select(facts=None, observations=None, **kwargs):
    facts = [listing("BTC/USDT"), listing("ETH/USDT")] if facts is None else facts
    observations = [observation("BTC/USDT", 2.), observation("ETH/USDT", 1.)] if observations is None else observations
    provenance = DataProvenance("fixture://s2-not-real-data", "synthetic",
                                selection_input_digest(facts, observations))
    return select_targets(facts=facts, observations=observations, provenance=provenance,
                          as_of=kwargs.pop("as_of", DAY),
                          policy=kwargs.pop("policy", SelectionPolicy(top_n=1, min_listing_days=0,
                                            min_quote_volume=100., gross_target=.6, max_symbol_weight=.6)),
                          allow_synthetic=True, **kwargs)


def quote(price=100., volume=1000., min_notional=1., step=.01):
    return ExecutionQuote(price, volume, NEXT, NEXT, step, min_notional)


def plan(selection=None, **kwargs):
    options = dict(selection=select() if selection is None else selection, at=NEXT,
                   holdings={}, quotes={"BTC-USDT": quote(), "ETH-USDT": quote()}, equity=1000.,
                   settled_cash=1000., approved_buy_notional={"BTC-USDT": 600., "ETH-USDT": 600.},
                   policy=RebalancePolicy(participation=.1, max_turnover=1., max_symbol_weight=.6,
                                          fee_bps=0., slippage_bps=0.), facts_reconciled=True)
    options.update(kwargs)
    return plan_rebalance(**options)


def test_hand_computed_cross_section_and_explicit_cash_allocation():
    result = select()
    assert result["targets"] == {"BTC-USDT": .6, "ETH-USDT": 0.}
    assert result["rows"]["BTC-USDT"]["zscore"] == 1.
    assert result["rows"]["ETH-USDT"]["zscore"] == -1.
    assert result["rows"]["BTC-USDT"]["percentile_rank"] == 1.
    assert result["cash_target"] == pytest.approx(.4)
    assert result["formal_routing_enabled"] is False


def test_future_observations_announcements_and_revisions_do_not_change_prefix():
    before = select()
    facts = [listing("BTC/USDT"), listing("ETH/USDT"),
             listing("NEW/USDT", "2024-02-03T00:00:00Z"),
             MembershipFact("BTC/USDT", "delisted", "2024-02-05T00:00:00Z", "2024-02-02T00:00:00Z")]
    observations = [observation("BTC/USDT", 2.), observation("ETH/USDT", 1.),
                    observation("NEW/USDT", 999., observed="2024-02-03T00:00:00Z", available="2024-02-03T00:00:00Z"),
                    observation("ETH/USDT", 999., available="2024-02-02T00:00:00Z")]
    after = select(facts, observations)
    assert before["decision_id"] == after["decision_id"]
    assert before["targets"] == after["targets"]
    assert before["provenance"]["dataset_sha256"] != after["provenance"]["dataset_sha256"]


def test_timezone_offsets_have_identical_decision_semantics():
    assert select(as_of="2024-02-01T08:00:00+08:00")["decision_id"] == select()["decision_id"]


def test_synthetic_requires_explicit_opt_in_and_content_digest_is_checked():
    facts, observations = [listing("BTC")], [observation("BTC")]
    provenance = DataProvenance("fixture://not-real", "synthetic", selection_input_digest(facts, observations))
    with pytest.raises(ValueError, match="synthetic"):
        select_targets(facts=facts, observations=observations, provenance=provenance,
                       as_of=DAY, policy=SelectionPolicy())
    with pytest.raises(ValueError, match="hash mismatch"):
        select_targets(facts=facts, observations=observations,
                       provenance=replace(provenance, dataset_sha256="0" * 64),
                       as_of=DAY, policy=SelectionPolicy(), allow_synthetic=True)


def test_real_label_never_claims_external_authenticity_verified():
    facts, observations = [listing("BTC")], [observation("BTC")]
    result = select_targets(facts=facts, observations=observations,
                            provenance=DataProvenance("https://example.invalid/vendor", "real",
                                                       selection_input_digest(facts, observations)),
                            as_of=DAY, policy=SelectionPolicy())
    assert result["provenance_authenticity"] == "requires_external_source_verification"


@pytest.mark.parametrize("item,reason", [
    (observation("BTC/USDT", None), "missing_or_invalid_score"),
    (observation("BTC/USDT", volume=None), "missing_or_invalid_liquidity"),
    (observation("BTC/USDT", volume=99.), "liquidity_filter"),
    (observation("BTC/USDT", observed="2024-01-29T00:00:00Z", available=DAY), "stale_factor"),
    (observation("BTC/USDT", observed="2023-12-31T00:00:00Z", available=DAY), "factor_precedes_listing"),
])
def test_missing_or_ineligible_factor_is_never_high_score_imputed(item, reason):
    result = select(observations=[item, observation("ETH/USDT")])
    assert result["rows"]["BTC-USDT"]["reason"] == reason
    assert result["targets"]["BTC-USDT"] == 0.
    assert result["targets"]["ETH-USDT"] == .6


def test_explicit_symbols_override_and_aliases_respect_pit_filters():
    result = select(explicit_symbols=["eth_usdt", "UNKNOWN"])
    assert result["targets"] == {"BTC-USDT": 0., "ETH-USDT": .6}
    assert result["rows"]["BTC-USDT"]["reason"] == "explicit_symbols_exclusion"


def test_constant_scores_are_deterministic_but_not_invented_zscores():
    result = select(observations=[observation("ETH/USDT"), observation("BTC/USDT")])
    assert result["targets"]["BTC-USDT"] == .6
    assert result["rows"]["BTC-USDT"]["percentile_rank"] == .75
    assert result["rows"]["BTC-USDT"]["zscore"] is None
    assert result["rows"]["BTC-USDT"]["zscore_status"] == "constant_cross_section"


def test_finite_extreme_scores_do_not_overflow_standardization():
    result = select(observations=[observation("BTC/USDT", 1e308), observation("ETH/USDT", -1e308)])
    assert result["rows"]["BTC-USDT"]["zscore"] == 1.
    assert result["rows"]["ETH-USDT"]["zscore"] == -1.


def test_delayed_listing_knowledge_and_recent_listing_age_are_not_bypassed():
    facts = [MembershipFact("BTC/USDT", "listed", "2024-01-01T00:00:00Z", "2024-02-02T00:00:00Z"),
             listing("ETH/USDT", "2024-01-31T00:00:00Z")]
    result = select(facts=facts, held_symbols=["BTC/USDT"],
                    policy=SelectionPolicy(top_n=1, min_listing_days=30))
    assert result["targets"] == {"BTC-USDT": 0., "ETH-USDT": 0.}
    assert result["rows"]["BTC-USDT"]["reason"] == "membership_unknown_or_not_yet_listed"
    assert result["rows"]["ETH-USDT"]["reason"] == "listing_age"


@pytest.mark.parametrize("facts,observations,message", [
    ([listing("BTC/USDT"), listing("BTC-USDT")], [], "duplicate membership"),
    ([listing("BTC/USDT")], [observation("BTC/USDT"), observation("BTC-USDT")], "duplicate factor"),
    ([listing("BTC/USDT")], [observation("BTC/USDT", available="2024-01-31T00:00:00Z")], "availability"),
])
def test_ambiguous_or_impossible_pit_inputs_are_rejected(facts, observations, message):
    with pytest.raises(ValueError, match=message):
        select(facts=facts, observations=observations)


def test_rank_buffer_retains_incumbent_without_bypassing_missing_data():
    policy = SelectionPolicy(top_n=1, buffer_ranks=1, min_listing_days=0,
                              min_quote_volume=100., gross_target=.6, max_symbol_weight=.6)
    result = select(policy=policy, held_symbols=["ETH/USDT"])
    assert result["targets"]["ETH-USDT"] == .6
    result = select(policy=policy, held_symbols=["ETH/USDT"], observations=[observation("BTC/USDT", 2.)])
    assert result["targets"]["ETH-USDT"] == 0.


def test_announced_delisting_exits_only_when_notice_available_not_by_future_last_bar():
    facts = [listing("BTC/USDT"), listing("ETH/USDT"),
             MembershipFact("BTC/USDT", "delisted", "2024-02-02T00:00:00Z", DAY)]
    selected = select(facts=facts, held_symbols=["BTC/USDT"])
    assert selected["forced_exits"] == ["BTC-USDT"]
    assert selected["targets"]["BTC-USDT"] == 0.
    assert selected["rows"]["BTC-USDT"]["tradable"] is True
    result = plan(selected, holdings={"BTC-USDT": 5.}, settled_cash=0.,
                  quotes={"BTC-USDT": quote(volume=10.), "ETH-USDT": quote()},
                  policy=RebalancePolicy(participation=.1, max_turnover=0., fee_bps=0., slippage_bps=0.),
                  last_rebalance_at=DAY)
    assert result["proposals"][0]["quantity"] == 1.
    assert result["proposals"][0]["reason"] == "delisting_exit"
    assert result["pending_targets"]["BTC-USDT"]["remaining_quantity"] == 4.
    assert result["discretionary_turnover"] == 0.
    assert result["forced_exit_turnover"] == .1


def test_delisting_deadline_blocks_fabricated_post_delisting_fill():
    facts = [listing("BTC/USDT"), MembershipFact("BTC/USDT", "delisted", NEXT, DAY)]
    selected = select(facts=facts, held_symbols=["BTC/USDT"])
    result = plan(selected, holdings={"BTC-USDT": 2.})
    assert result["proposals"] == []
    assert result["pending_targets"]["BTC-USDT"]["reason"] == "market_unavailable_pending_exit"


def test_quantity_fee_cash_and_approved_budget_constraints_by_hand():
    result = plan(settled_cash=100., approved_buy_notional={"BTC-USDT": 80.},
                  policy=RebalancePolicy(participation=.1, max_turnover=1., max_symbol_weight=.6,
                                         fee_bps=100., slippage_bps=100.))
    order = result["proposals"][0]
    assert order["quantity"] == .79
    assert order["estimated_price"] == 101.
    assert order["estimated_fee"] == pytest.approx(.7979)
    assert result["estimated_remaining_settled_cash"] == pytest.approx(100. - .79 * 101. * 1.01)
    assert order["quantity"] * order["estimated_price"] <= 80.


def test_total_turnover_and_shared_participation_reservations():
    result = plan(reserved_participation_qty={"BTC-USDT": 99.9},
                  committed_turnover_notional=190.,
                  policy=RebalancePolicy(participation=.1, max_turnover=.2, max_symbol_weight=.6,
                                         fee_bps=0., slippage_bps=0.))
    order = result["proposals"][0]
    assert order["quantity"] <= .1
    assert result["discretionary_turnover"] <= .01


def test_no_buys_financed_by_unfilled_sell_proposals_or_missing_authority():
    result = plan(holdings={"ETH-USDT": 5.}, settled_cash=0.)
    assert [(p["symbol"], p["side"]) for p in result["proposals"]] == [("ETH-USDT", "sell")]
    assert plan(approved_buy_notional={})["proposals"] == []


def test_pending_orders_prevent_duplicate_quantity_and_reserve_cash():
    result = plan(pending_buy_qty={"BTC-USDT": 6.}, pending_buy_cash=600.)
    assert result["proposals"] == []
    with pytest.raises(ValueError, match="reservation is incomplete"):
        plan(pending_buy_qty={"BTC-USDT": 6.}, pending_buy_cash=100.)
    selected = select(explicit_symbols=[], held_symbols=["BTC/USDT"])
    result = plan(selected, holdings={"BTC-USDT": 6.}, pending_sell_qty={"BTC-USDT": 6.})
    assert result["proposals"] == []


def test_forced_exit_requires_pending_buy_cancellation_and_then_reconciles_partial_fill():
    facts = [listing("BTC/USDT"), MembershipFact("BTC/USDT", "delisted", "2024-02-02T00:00:00Z", DAY)]
    selected = select(facts=facts, held_symbols=["BTC/USDT"])
    result = plan(selected, holdings={"BTC-USDT": 2.}, pending_buy_qty={"BTC-USDT": 1.}, pending_buy_cash=100.)
    assert result["proposals"] == []
    assert result["pending_targets"]["BTC-USDT"]["reason"] == "cancel_pending_buys_before_forced_exit"
    result = plan(selected, holdings={"BTC-USDT": 1.5}, pending_sell_qty={"BTC-USDT": .5})
    assert result["proposals"][0]["quantity"] == 1.


def test_minimum_notional_or_missing_liquidity_retains_target_without_fabricating_exit():
    selected = select(explicit_symbols=[], held_symbols=["BTC/USDT"])
    result = plan(selected, holdings={"BTC-USDT": .01}, quotes={"BTC-USDT": quote(min_notional=10.)})
    assert result["proposals"] == []
    assert result["pending_targets"]["BTC-USDT"]["reason"] == "minimum_notional_pending_target"
    assert plan(quotes={"BTC-USDT": quote(volume=0.), "ETH-USDT": quote()})["proposals"] == []


def test_replay_ids_are_stable_and_rebalance_frequency_is_enforced():
    assert plan() == plan()
    assert plan(last_rebalance_at=DAY)["proposals"] == []


@pytest.mark.parametrize("kwargs,message", [
    ({"at": DAY}, "after selection"),
    ({"facts_reconciled": False}, "reconciled"),
    ({"holdings": {"BTC-USDT": 1.}, "quotes": {}}, "valuation quotes"),
    ({"holdings": {"BTC/USDT": 1., "BTC-USDT": 2.}}, "duplicate normalized"),
    ({"pending_sell_qty": {"BTC-USDT": 1.}}, "pending sells"),
    ({"last_rebalance_at": "2024-02-02T00:00:00Z"}, "future"),
    ({"quotes": {"BTC-USDT": replace(quote(), available_at="2024-02-02T00:00:00Z")}}, "quote is future"),
])
def test_invalid_or_unreconciled_execution_facts_fail_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        plan(**kwargs)


def test_allocation_cap_cannot_expand_when_proposed_sells_are_unsettled():
    result = plan(holdings={"ETH-USDT": 9.},
                  policy=RebalancePolicy(participation=.1, max_turnover=1., max_gross_weight=1.,
                                         max_symbol_weight=.6, fee_bps=0., slippage_bps=0.))
    buys = [p for p in result["proposals"] if p["side"] == "buy"]
    assert sum(p["quantity"] * p["reference_price"] for p in buys) <= 100.


@pytest.mark.parametrize("policy", [lambda: SelectionPolicy(top_n=0),
                                   lambda: SelectionPolicy(gross_target=1.1),
                                   lambda: RebalancePolicy(participation=1.1),
                                   lambda: RebalancePolicy(slippage_bps=10000.)])
def test_invalid_policy_is_rejected(policy):
    with pytest.raises(ValueError):
        policy()
