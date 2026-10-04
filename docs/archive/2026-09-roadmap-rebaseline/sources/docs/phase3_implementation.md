# Phase 3 implementation and acceptance map

This document maps the Phase 3 roadmap tasks to the implemented contracts and
their verification evidence.  It is an engineering acceptance record; it does
not approve the strategy for production capital.

| Task | Implemented contract | Primary evidence |
|---|---|---|
| T-3.1 | Portfolio drawdown is measured from the historical equity high-water mark. | `core/risk/`; `TestHighWaterDrawdownProtection` |
| T-3.2 | `daily_loss_limit` is independent from all portfolio drawdown thresholds. | `config/params.yaml`; daily reset test |
| T-3.3 | Sticky reduce, block-new, liquidate and lock levels; named manual recovery is required. | `BreakerAction`, `RiskManager.manual_resume`, breaker audit |
| T-3.4 | Explicit `spot`, `spot_margin` and `perpetual` account modes with different cash/PnL semantics. | `core/accounts.py`; `core/portfolio.py` |
| T-3.5 | Per-bar initial, maintenance and available margin reconciliation. | `MarginSnapshot`; `margin_ledger` |
| T-3.6 | Historical perpetual `funding_rate` accrued by settlement bucket; missing required data fails closed. | `Broker.accrue_carry`; funding tests |
| T-3.7 | Margin shorts enforce auditable borrow availability and accrue time-weighted borrow cost. | borrow-limit/cost tests; `financing_ledger` |
| T-3.8 | Mark-price maintenance-margin liquidation uses the canonical fill, lot and CloseEvent path. | `Portfolio.margin_snapshot`; `Broker.force_liquidate` |
| T-3.9 | Bid/ask spread and intrabar-volatility slippage are persisted on each fill. | dynamic execution test; trade fields |
| T-3.10 | Non-linear participation impact and shared per-bar volume budgets create partial fills across bars. | `Broker.process_orders`; capacity audit |
| T-3.11 | 10k, 100k, 1m and 10m runs report returns, fills, rejection rates, costs and explained path changes. | `backtest/capacity.py`; `phase3_capacity_report.json` |
| T-3.12 | Account, risk, financing, liquidation, execution and capacity acceptance tests. | `tests/test_phase3_risk_margin_execution.py` |

## Gate interpretation

- **G7 — account-model consistency:** the selected account mode is a required
  configuration value.  Spot cannot short; margin/perpetual collateral does not
  exchange full position notional; all explicit financing costs feed equity and
  the accounting identity.
- **G8 — real portfolio drawdown protection:** high-water state persists across
  day boundaries and cannot be lowered by an automatic reset.  Only the daily
  loss flag resets automatically.
- **G9 — explainable capacity:** every observed path change in the four-level
  verification report is linked to participation-limited partial fills and
  non-linear impact.  The checked-in report uses deterministic synthetic data
  and therefore validates the mechanism, not a deployable AUM limit.

Phase 3 makes costs and risk behavior more realistic; it does not establish
positive alpha.  Holdout research, Paper Trading and live-capital approval remain
separate later-phase gates.
