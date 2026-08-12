# Backtest fixed baselines (schema v1)

Each JSON bundle freezes input data, run metadata, and `orders`, `fills`,
`closed_trades`, `equity`, and `metrics`. The scenarios cover no-trade,
hand-calculable, and fixed-history/deterministic-synthetic behavior.

Timestamps are UTC and bounds are inclusive. `data_sha256` is calculated from
compact JSON with sorted keys. Arrays are ordered. Never overwrite a reviewed
baseline after a behavior change; add a version and document the difference.
Generated runs belong in a temporary directory, `tests/.generated/`, or
`docs/baselines/generated/` and are not committed.

**These bundles are hand-authored fixture data.** `TestBacktestFixedBaselines`
in `tests/test_backtest_regression.py` round-trips their `artifacts` field
through JSON to check internal consistency (e.g. hand-calculable PnL math) —
it never calls `BacktestEngine.run()`.

```powershell
python -m unittest tests.test_backtest_regression -v
python -m unittest discover -s tests -p "test_*.py" -v
```

## `engine/engine_baseline_v1.json` — real-engine equivalence baseline

This one is different: it's a snapshot of `BacktestEngine.run()` actually
executing against a deterministic synthetic dataset (`DataFetcher.generate_
scenario`, fixed seed), driving the full production path —
`HistoricalMarketDataAdapter` → `EventProcessor.process_symbol` → `Router` →
strategies → `Broker`. `TestBacktestEngineEquivalenceBaseline` re-runs the
same input and asserts the output (`trades`/`equity_curve`/`benchmark`/
`metrics`) is byte-for-byte identical to what's recorded here. It's the
regression guard for architecture-level changes to the hot path (positional
indexing, incremental indicators, event-pipeline memory bounds, etc.) — run
it before *and* after such a change and both runs must pass unmodified.

Only regenerate after a reviewed, intentional behavior change, and diff the
old vs. new fixture before committing:

```powershell
python -m tests.generate_engine_baseline
```
