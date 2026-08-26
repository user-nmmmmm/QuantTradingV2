"""Guards against strategies that are registered but never routed (M-16).

`core/system_factory.py`'s registry and `router/router.py`'s regime_map are
two independently maintained collections. Nothing previously caught the case
where a strategy gets added to the registry but the routing table (backed by
``params.yaml``'s ``routing`` section) never references it — it would sit in
the registry, instantiated every run, but be structurally unreachable from
any market regime. That drift is exactly what happened historically with
``TrendUpStrategy``/``TrendDownStrategy`` before they were removed from the
registry (see P1-19).
"""
import unittest

from core.system_factory import build_router, build_strategy_registry


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
        router = build_router(registry, allow_short=True)

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
        router = build_router(registry, allow_short=True)

        routed_strategy_names = {
            name for name in router.regime_map.values() if name != "Cash"
        }
        unregistered = routed_strategy_names - set(registry.keys())
        self.assertEqual(
            unregistered, set(),
            "Routing table references strategy names with no matching "
            f"registry entry: {sorted(unregistered)}.",
        )


if __name__ == "__main__":
    unittest.main()
