# Backtest fixed baselines (schema v1)

Each JSON bundle freezes input data, run metadata, and `orders`, `fills`,
`closed_trades`, `equity`, and `metrics`. The scenarios cover no-trade,
hand-calculable, and fixed-history/deterministic-synthetic behavior.

Timestamps are UTC and bounds are inclusive. `data_sha256` is calculated from
compact JSON with sorted keys. Arrays are ordered. Never overwrite a reviewed
baseline after a behavior change; add a version and document the difference.
Generated runs belong in a temporary directory, `tests/.generated/`, or
`docs/baselines/generated/` and are not committed.

```powershell
python -m unittest tests.test_backtest_regression -v
python -m unittest discover -s tests -p "test_*.py" -v
```
