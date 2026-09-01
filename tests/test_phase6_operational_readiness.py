import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.gray_release import GrayReleasePolicy
from core.live_safety import StartupSafetyPolicy
from core.admission_gates import (
    EXPANSION_DIMENSIONS,
    MONITORING_DIMENSIONS,
    RECONCILIATION_LAYERS,
    audit_paper_run,
    evaluate_phase6,
    reconcile_lifecycle,
    review_expansion,
)
from dashboard.__main__ import load_dashboard, render_text


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def lifecycle() -> dict:
    return {
        layer: [{"record_id": f"{layer}-1", "value": index + 1.0}]
        for index, layer in enumerate(RECONCILIATION_LAYERS)
    }


def passing_bundle() -> dict:
    signals = [{
        "record_id": "signal-1",
        "strategy": "TrendBreakout",
        "symbol": "BTC/USDT",
        "bar_time": START.isoformat(),
        "action": "buy",
        "rule_version": "phase5-frozen-v1",
    }]
    paper = [
        {
            "timestamp": (START + timedelta(days=day)).isoformat(),
            "regime": "TREND_UP" if day < 28 else "RANGE",
            "incident_status": "resolved",
        }
        for day in range(56)
    ]
    monitoring = [{
        "timestamp": START.isoformat(),
        **{dimension: {"ok": True} for dimension in MONITORING_DIMENSIONS},
    }]
    scope = {
        "capital": 100.0,
        "symbols": ["BTC/USDT"],
        "exchanges": ["binance"],
        "strategies": ["TrendBreakout"],
        "leverage": 1.0,
        "unattended_hours": 0,
    }
    return {
        "backtest_signals": signals,
        "shadow_signals": [dict(signals[0], observed_at=START.isoformat())],
        "paper_observations": paper,
        "expected_lifecycle": lifecycle(),
        "actual_lifecycle": lifecycle(),
        "execution_observations": [{
            "modeled_slippage_bps": 2.0,
            "actual_slippage_bps": 2.5,
            "modeled_spread_bps": 1.0,
            "actual_spread_bps": 1.5,
            "modeled_latency_ms": 100.0,
            "actual_latency_ms": 125.0,
            "modeled_rejected": False,
            "actual_rejected": False,
        }],
        "monitoring_snapshots": monitoring,
        "alert_delivery_verified": True,
        "p0_issues": [{"id": "P0-1", "status": "closed"}],
        "holdout_report": {
            "decision": "admit",
            "gates": {"pf": True, "drawdown": True, "cost": True},
        },
        "admission_approval": {
            "approved": True,
            "operator": "risk-owner",
            "approved_at": START.isoformat(),
        },
        "micro_live_scope": {
            "exchange": "binance",
            "symbol": "BTC/USDT",
            "strategy": "TrendBreakout",
            "capital": 100.0,
            "leverage": 1.0,
        },
        "micro_live_observations": [{"event_id": "event-1", "status": "matched"}],
        "micro_live_reconciliation": {"passed": True, "coverage": 1.0},
        "current_scope": scope,
        "proposed_scope": dict(scope, capital=150.0),
        "capacity_review": {"passed": True},
        "cost_review": {"passed": True},
        "risk_review": {"passed": True},
        "expansion_approval": {
            "approved": True,
            "operator": "risk-owner",
            "approved_at": (START + timedelta(days=60)).isoformat(),
        },
    }


class Phase6EvidenceTests(unittest.TestCase):
    def test_complete_evidence_bundle_passes_all_eight_tasks(self):
        report = evaluate_phase6(passing_bundle())

        self.assertTrue(report["passed"])
        self.assertTrue(report["admission_passed"])
        self.assertEqual(set(report["tasks"]), {f"T-6.{number}" for number in range(1, 9)})
        self.assertTrue(all(task["passed"] for task in report["tasks"].values()))

    def test_paper_gate_requires_full_eight_weeks_and_multiple_regimes(self):
        observations = [
            {"timestamp": (START + timedelta(days=day)).isoformat(), "regime": "RANGE"}
            for day in range(55)
        ]

        report = audit_paper_run(observations)

        self.assertFalse(report["passed"])
        self.assertIn("paper_duration_too_short:55<56", report["issues"])
        self.assertIn("market_regime_coverage_too_low:1<2", report["issues"])

    def test_reconciliation_requires_every_layer_and_exact_records(self):
        expected = lifecycle()
        actual = lifecycle()
        actual["costs"][0]["value"] = 999.0

        report = reconcile_lifecycle(expected, actual)

        self.assertFalse(report["passed"])
        self.assertLess(report["coverage"], 1.0)
        self.assertFalse(report["layers"]["costs"]["passed"])

    def test_open_p0_blocks_admission_and_all_downstream_tasks(self):
        bundle = passing_bundle()
        bundle["p0_issues"][0]["status"] = "open"

        report = evaluate_phase6(bundle)

        self.assertFalse(report["admission_passed"])
        self.assertFalse(report["tasks"]["T-6.7"]["passed"])
        self.assertFalse(report["tasks"]["T-6.8"]["passed"])

    def test_expansion_changes_only_one_dimension(self):
        current = {field: 1 for field in EXPANSION_DIMENSIONS}
        proposed = dict(current, capital=2, leverage=2)
        approval = {
            "approved": True,
            "operator": "risk-owner",
            "approved_at": START.isoformat(),
        }

        report = review_expansion(
            micro_live_report={"passed": True},
            current_scope=current,
            proposed_scope=proposed,
            capacity_review={"passed": True},
            cost_review={"passed": True},
            risk_review={"passed": True},
            approval=approval,
        )

        self.assertFalse(report["passed"])
        self.assertEqual(report["changed_dimensions"], ["capital", "leverage"])

    def test_gray_release_accepts_phase6_admission_but_still_checks_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "phase6.json"
            snapshot = root / "state.db"
            evidence.write_text(
                json.dumps({"passed": False, "admission_passed": True}),
                encoding="utf-8",
            )
            snapshot.write_bytes(b"snapshot")
            startup = StartupSafetyPolicy(
                False, "binance", "spot", ("BTC/USDT",),
                ("binance",), ("spot",), ("BTC/USDT",), "USDT", 10, 20,
            )
            exchange = MagicMock()
            exchange.fetch_api_permissions.return_value = {
                "enableWithdrawals": False,
                "enableSpotAndMarginTrading": True,
            }
            policy = GrayReleasePolicy(
                "binance", "BTC/USDT", 10, 20, str(evidence), str(snapshot),
            )
            with patch.dict("os.environ", {"QUANT_R8_APPROVED": "approved"}):
                policy.validate(startup, exchange)

    def test_dashboard_displays_all_phase6_monitoring_dimensions(self):
        report = evaluate_phase6(passing_bundle())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "status.json"
            alerts = root / "alerts.jsonl"
            phase6 = root / "phase6.json"
            status.write_text(json.dumps({
                "healthy": True,
                "operational_state": "HEALTHY",
                "health_assessment": {"reasons": []},
            }), encoding="utf-8")
            alerts.write_text("", encoding="utf-8")
            phase6.write_text(json.dumps(report), encoding="utf-8")

            dashboard = load_dashboard(
                str(status), str(alerts), phase6_path=str(phase6),
            )
            rendered = render_text(dashboard)

        self.assertIn("Phase 6 monitoring:", rendered)
        for dimension in MONITORING_DIMENSIONS:
            self.assertIn(f"{dimension}: OK", rendered)


if __name__ == "__main__":
    unittest.main()
