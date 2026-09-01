"""Guards against strategies that are registered but never routed (M-16).

`composition/factory.py`'s registry and `router/router.py`'s regime_map are
two independently maintained collections. Nothing previously caught the case
where a strategy gets added to the registry but the routing table (backed by
``params.yaml``'s ``routing`` section) never references it — it would sit in
the registry, instantiated every run, but be structurally unreachable from
any market regime. That drift is exactly what happened historically with
``TrendUpStrategy``/``TrendDownStrategy`` before they were removed from the
registry (see P1-19).
"""
import unittest

from composition.factory import build_router, build_strategy_registry
from config.config import config


# Strategies deliberately kept out of the routing table, with the reason
# documented here. Phase 4 keeps these implementations available for isolated
# replay/research while their regimes deliberately route to Cash until the
# admission gates in docs/phase4_implementation.md are met.
INTENTIONALLY_UNROUTED_STRATEGIES: frozenset[str] = frozenset({
    "TrendBreakdown",
    "RangeMeanReversion",
    "VolatilityReversion",
})


class TestStrategyRoutingConsistency(unittest.TestCase):
    def test_every_registered_strategy_is_reachable_from_routing(self):
        registry = build_strategy_registry()
        router = build_router(registry, config, allow_short=True)

        routed_strategy_names = {
            name for name in router.regime_map.values() if name != "Cash"
        }
        registered_strategy_names = set(registry.keys())

        unrouted = (
            registered_strategy_names
            - routed_strategy_names
            - INTENTIONALLY_UNROUTED_STRATEGIES
        )
        self.assertEqual(
            unrouted, set(),
            "Strategies registered in build_strategy_registry() but not "
            "referenced by any regime in the routing table (params.yaml "
            "'routing' section): "
            f"{sorted(unrouted)}. Either wire them into 'routing', or add "
            "them to INTENTIONALLY_UNROUTED_STRATEGIES with a documented "
            "reason.",
        )

    def test_every_routed_strategy_name_is_registered(self):
        registry = build_strategy_registry()
        router = build_router(registry, config, allow_short=True)

        routed_strategy_names = {
            name for name in router.regime_map.values() if name != "Cash"
        }
        unregistered = routed_strategy_names - set(registry.keys())
        self.assertEqual(
            unregistered, set(),
            "Routing table references strategy names with no matching "
            f"registry entry: {sorted(unregistered)}.",
        )


    def test_allowed_states_match_the_regimes_actually_routed(self):
        """STR-P1-07: a strategy may not claim regimes production never gives it.

        ``TrendBreakoutStrategy`` used to declare ``VOLATILE`` while the
        routing table sent VOLATILE to Cash, so reading the class suggested a
        wider operating range than the run ever had.
        """
        registry = build_strategy_registry()
        router = build_router(registry, config, allow_short=True)

        routed_regimes: dict[str, set[str]] = {}
        for regime, strategy_name in router.regime_map.items():
            if strategy_name == "Cash":
                continue
            routed_regimes.setdefault(strategy_name, set()).add(regime)

        mismatches = {}
        for name, regimes in routed_regimes.items():
            declared = {state.name for state in registry[name].allowed_states}
            if declared != regimes:
                mismatches[name] = {"declared": sorted(declared),
                                    "routed": sorted(regimes)}
        self.assertEqual(
            mismatches, {},
            "Strategy allowed_states disagree with the regimes routed to them "
            f"(STR-P1-07): {mismatches}. Either route the regime or stop "
            "declaring it.",
        )

    def test_unrouted_strategies_are_declared_as_such(self):
        registry = build_strategy_registry()
        router = build_router(registry, config, allow_short=True)
        routed = {name for name in router.regime_map.values() if name != "Cash"}
        for name in set(registry) - routed:
            self.assertIn(
                name, INTENTIONALLY_UNROUTED_STRATEGIES,
                f"{name} is unreachable from any regime but is not documented "
                "as intentionally unrouted.",
            )


if __name__ == "__main__":
    unittest.main()
