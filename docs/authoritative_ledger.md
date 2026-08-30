# P1.2 Authoritative Ledger

## Scope and invariants

The authoritative state path is:

```text
EventEnvelope -> SQLiteEventStore(sequence)
              -> Fill/Fee/Cash ledgers
              -> average-cost Position reducer
              -> PortfolioProjection
              -> PortfolioSnapshot
```

The mutable legacy `Portfolio` remains available for existing engines, but it
is not an audit source. A ledger-backed snapshot is rebuilt exclusively from
persisted events.

Core invariants:

- The event table accepts INSERT only. SQLite triggers reject UPDATE and DELETE.
- `event_id` is unique. Re-delivery of the same business event is a no-op;
  different content with the same ID raises an idempotency conflict.
- Projection order is the store's monotonic `sequence`, not exchange timestamp.
- Quantities, prices, fees, cash, cost basis, and PnL use `Decimal`.
- A partial close preserves average cost. A fill crossing zero realizes the old
  side and opens the residual at the crossing fill price.
- Fees are independent postings and debit their actual currency.
- Every non-base cash or position value requires an explicit FX rate at snapshot
  time. Missing marks or FX rates fail closed.
- Snapshot materialization does not write authoritative state. It is a disposable
  projection and can be recreated at any sequence.

## Accounting formulas

For signed fill quantity `dq` (buy positive, sell negative):

```text
quote cash delta = -(dq * fill price)
fee cash delta   = -fee amount in fee currency
```

For an existing signed position `q` at average cost `c`, an opposing fill
closes `min(abs(q), abs(dq))`:

```text
realized delta = closed quantity * (fill price - c) * sign(q)
unrealized PnL = current quantity * (mark price - average cost)
```

If `q + dq` changes sign, the residual's average cost is the current fill
price. This rule is symmetric for long-to-short and short-to-long reversals.

Equity uses cash-flow accounting:

```text
equity = sum(FX(cash balance)) + sum(FX(position quantity * mark))
gross  = sum(abs(FX(position quantity * mark)))
net    = sum(FX(position quantity * mark))
```

Realized PnL is retained independently of open positions, so it remains present
after a position is completely flattened. Reported realized PnL is gross of
fees; fee totals are separately available for net attribution.

## Main APIs

- `SQLiteEventStore(path)`: append, append_many, read/read_records, point lookup,
  sequence checkpoints, durable reopen.
- `AuthoritativeLedger`: records cash, marks, and canonical fill envelopes and
  keeps a live projection.
- `PortfolioProjection.rebuild(...)`: resets all derived state and replays an
  account through an optional authoritative sequence.
- `PortfolioProjection.snapshot(...)`: materializes positions, multi-currency
  cash, marks, realized/unrealized PnL, fees, exposure, and sequence watermark.
- `PortfolioProjection.reconcile(...)`: compares exchange cash and positions,
  applies tolerances, and emits structured warning/critical discrepancies.

## Minimal use

```python
from datetime import datetime, timezone
from core.event_store import SQLiteEventStore
from research.audit.ledger import AuthoritativeLedger

store = SQLiteEventStore("state/ledger.sqlite3")
ledger = AuthoritativeLedger(
    store, account_id="sandbox", base_currency="USD", run_id="session-1"
)
ledger.record_cash(
    "USD", "10000", reason="opening_balance",
    occurred_at=datetime.now(timezone.utc),
    idempotency_key="opening-balance-v1",
)

# Canonical FillEvent envelopes from backtest/live adapters are passed to:
# ledger.record_fill(fill_envelope)

snapshot = ledger.snapshot(fx_rates={"EUR/USD": "1.10"})

# Restart/recovery:
recovered = AuthoritativeLedger(
    store, account_id="sandbox", base_currency="USD", run_id="session-2"
)
assert recovered.snapshot(fx_rates={"EUR/USD": "1.10"}) == snapshot
```

## Reconciliation behavior

Reconciliation compares the union of local and external keys, so an asset or
position missing from either side is reported rather than silently ignored.
Differences within tolerance are omitted. Larger differences are classified as
`warning` or `critical`; critical items require manual review. The report
does not mutate the ledger or auto-book exchange values because reconciliation
facts and corrective accounting events must remain explicit and auditable.

## Compatibility boundary

The original `PortfolioSnapshot` positional fields remain unchanged. Ledger
fields were appended with defaults. Existing callers therefore continue to
construct snapshots as before, while ledger-aware callers receive positions,
currency balances, PnL, fee totals, base currency, and the sequence watermark.

The legacy `Portfolio.update_position` also receives the reversal-cost fix so
existing backtests do not retain a long cost basis after crossing into a short
position (or vice versa).
