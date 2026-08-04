# Batch 0 fixed baseline

Status: accepted baseline v1  
Date: 2026-08-02  
Task: R0-B0 — documentation and fixed baseline

## Scope and contract

The baseline freezes three deliberately small scenarios under
`tests/fixtures/backtest/`: no trades, a hand-calculable round trip, and a
mixed frozen-history/deterministic-synthetic data set. Every bundle records
inclusive UTC `start`/`end`, seed, sorted symbols, complete execution config,
row/data-quality summary, and a canonical SHA-256 data digest.

Each bundle stores the five Batch 0 artifacts (`orders`, `fills`,
`closed_trades`, `equity`, and `metrics`). Stable IDs are fixture IDs, not a
claim that the production ledger contract is complete. Missing capabilities
are not inferred: the mixed sample keeps its open position out of
`closed_trades` and reports it explicitly.

## Environment and commands

- Python: 3.13.2 (`.python-version`)
- Runtime dependency lock: `requirements.lock.txt`
- Test framework: standard-library `unittest` (no additional test package)
- Focused gate: `python -m unittest tests.test_backtest_regression -v`
- Full gate: `python -m unittest discover -s tests -p "test_*.py" -v`

The regression gate validates the manifest and data digest, checks the manual
PnL arithmetic, materializes every artifact three consecutive times, parses
the files again, and compares normalized structures. Comparison therefore
ignores formatting but detects field, value, type, null, ordering, or row
changes.

## Generated-artifact rules

Reviewed fixture bundles and this evidence document are versioned. Temporary
materializations are written to OS temporary directories by tests. Manual
runs may use `tests/.generated/` or `docs/baselines/generated/`; those paths
must remain untracked. Normal runtime files remain under `reports/` and are not
baseline evidence unless copied into a versioned fixture through review.

## Compatibility, differences, and limitations

This batch adds test/documentation contracts only and does not change runtime
backtest behavior. It intentionally does not define the final `MetricResult`,
trade ledger, portfolio reconciliation, or JSON null/state semantics reserved
for later batches. The historical rows are a tiny offline snapshot suitable
for deterministic regression, not a statistically representative sample.

When behavior intentionally changes, retain the prior reviewed fixture,
introduce a new schema/formula version, and record old/new values and the
reason here before accepting the new baseline.
