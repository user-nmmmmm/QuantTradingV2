# R6 monitoring operations

## EOD reconciliation

Export venue/account facts to JSON:

~~~json
{"cash": {"USDT": "1000"}, "positions": {"BTC/USDT": {"qty": "0.1"}}}
~~~

Schedule this command after the venue trading day closes:

~~~powershell
python -m research.audit.reconciliation_job --ledger-db reports/ledger.db --account-id primary --base-currency USDT --external-state reports/external_eod.json
~~~

The job atomically writes one structured report per account/day under
`reports/reconciliation/`. A clean report is still persisted with zero
discrepancies. The command exits 0 for a clean match and 2 for discrepancies.

## External position policy

Account synchronization compares venue quantity with the persisted fill/lot
projection. A nonzero mismatch, or a lot missing owner/entry time, is explicitly
unowned. Live management records `external_position_actions` in the state
database before execution and submits a named `ExternalPositionExit` with a
fixed zero target for the affected symbol. It applies even when ordinary
protective orders are disabled. Partial or unconfirmed exits retain the same
action identity across restarts; pending orders are reconciled before retrying.

No strategy owner, historical entry price/time or alpha PnL is invented. The
policy flattens the whole affected net position when owned and unowned inventory
are mixed. A missing fresh quote, rejected order, unresolved cancellation or
symbol outside the run's allowlist leaves the action pending and new risk
blocked. Inspect the action's `last_error`, order ledger and venue position;
resolve the missing evidence or perform an independently verified venue exit.
Do not remove a pending action or edit the JSON dashboard to release the block.

To restore management instead of exiting, reconstruct the actual entry fills
and their verified strategy attribution, timestamps, costs and stop/risk inputs
through the existing controlled ledger recovery process. A matching owner
projection ends the pending action as `ownership_restored`. An external exit
without recoverable entry history remains an attribution/reconciliation issue
after becoming flat; its fills are retained, not counted as a fictional closed
strategy trade. Operator reconciliation is required before adding risk again.

## Checkpoint compatibility

The authoritative portfolio breaker payload includes `checkpoint_version=2`
and its UTC `trading_day`; the compatibility day/boolean values are committed
in the same transaction. On legacy checkpoints lacking an embedded day, a
different independently saved day cannot prove expiry: retain the daily halt
for the recovery day and record `legacy_breaker_day_unverified`. The next
verified UTC-day boundary follows the normal daily reset policy.

Event replay accepts registered historical `core.events:*`,
`core.event_types:*` and `core.ledger:CashEvent/MarkPriceEvent` wire names.
It normalizes those names only for idempotency comparison; stored documents
remain unchanged, and changed business payloads still raise conflicts.

## Alert hysteresis

The default live alert chain is wrapped by `HysteresisAlertSink`. The first
incident trigger is delivered, repeated equivalent contexts are suppressed, and
the ninth suppression emits `alert_suppression_summary`. Call `ack(event)`
when the incident recovers; the live health monitor does this for health alerts.

## Read-only dashboard

~~~powershell
python -m dashboard
python -m dashboard --json --alert-limit 20
~~~

The CLI reads `reports/live_status.json` and `reports/live_alerts.jsonl`.
It never opens the state database and displays equity, cash, positions, detailed
health reasons, and recent alerts.

## SQLite snapshots and rollback

The live engine snapshots its state database hourly and keeps 24 snapshots by
default. Both values are constructor options. Manual operations are:

~~~powershell
python -m core.sqlite_backup backup reports/live_status_state.db --retention 24
python -m core.sqlite_backup restore reports/live_status_state.db.snapshots/live_status_state.db.TIMESTAMP.sqlite3 reports/live_status_state.db
~~~

Stop the live engine before manual rollback. Backup and restore use SQLite's
online backup API, validate `PRAGMA integrity_check`, and atomically replace the
target during restore.

If the target database is corrupt, restore requires an independently verified
runtime identity file. Its JSON contains exactly `exchange`, `environment`,
`account`, and `market_type`, matching the approved runtime and snapshot. A
readable target supplies its own identity; a conflicting requested identity or
snapshot is rejected. Never copy an identity from an unrelated account merely
to make restore proceed.

~~~powershell
python -m core.sqlite_backup restore SNAPSHOT.sqlite3 TARGET.db --identity-file approved_runtime_identity.json
~~~

For corrupt targets, restore first preserves the original bytes as
`TARGET.db.corrupt.<unique-id>` (including any WAL/SHM evidence). The replacement
uses a unique temporary file, validates it before the atomic replacement, and
does not delete another recovery session's temporary file. Keep every writer
stopped until orders, fills, positions, risk actions and bar watermarks have
been compared with the venue. `integrity_check=ok` alone does not release the
risk halt. Resume responsibility belongs to the operator who verifies the
account facts and records that decision, not the backup command.

The repository includes an executable **offline** recovery drill. It creates a
new directory with synthetic sandbox facts, freezes a 30-second local recovery
target and zero lost pre-backup records, backs up the order/state/risk databases,
damages only those synthetic databases, and invokes the actual restore CLI.
Use a fresh output path for every run; an existing directory is rejected.

~~~powershell
.venv/Scripts/python.exe scripts/run_offline_recovery_drill.py --output reports/offline-recovery-UNIQUE-RUN --rto-seconds 30
.venv/Scripts/python.exe scripts/run_portable_tests.py tests/test_sys_local_closure.py tests/test_live_risk_action_lifecycle.py tests/test_r7_fault_injection.py tests/test_g1_live_safety.py -q
~~~

Review `protocol.json`, `command_results.json` and `recovery_drill.json`. A
nonzero command exit, changed fact, duplicate intent/fill, lost watermark, or
missed RTO is a failed drill and must be kept alongside the succeeding attempt.
This measures stopped-writer backup/restore on one local machine. It does not
measure production data loss since the last scheduled backup, human response
time, real alert delivery, remote restart, or venue reconciliation latency.
Those require a separately identified operational rehearsal.

The local fault matrix covers writes before submission, an attempted send with
no authoritative outcome, a lost response after venue acceptance, a response
before local persistence, a fill committed before its order state, and partial
state committed before restart. Each restart is driven by the stored intent
and queried venue fact. An attempted but unverified send stays UNKNOWN with
its risk reservation; do not turn a missing lookup into permission to resubmit.
Disk-full/replace failure preserves the previous complete target. Unknown
orders, connection failure, corrupt state and alert delivery failure retain
their fail-closed handling; local test sinks are not receipts from real users.

## Reconciliation evidence scope

The live periodic recovery status declares `scope=order_recovery`. Its `ok`
field describes unresolved orders only; `account_reconciliation=unverified`
explicitly records the missing independent opening-capital, cash-flow and
valuation inputs. It must not be presented as a full account reconciliation.
`core.account_reconciliation.reconcile_account_snapshots` compares independently
normalized spot/spot-margin snapshots, validates duplicate fills, quantity and
fee conservation, timestamped marks, cash free/locked/total and both equity
bridges. It creates no second ledger and infers no deposit from a cash mismatch.
The caller must establish the independent sources before using it in a live
gate; passing synthetic snapshots proves only the comparison contract.

Account sync rejects missing total quote cash, nonfinite balances, an invalid
free-plus-locked identity and unavailable position responses; it preserves the
previous verified portfolio on these failures. An explicit empty position list
is a flat fact. A missing response is unknown. `account_snapshot` retains the
observed source balance with account/mode and collection time and marks the
capital bridge unverified. External holdings remain under the controlled exit
policy above.

## Telegram notifications

Two independent channels, both driven by the same bot credentials:

1. **Real-time critical alerts.** Set `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID` in the environment before starting the live engine.
   `build_default_alert_sink()` (`core/alerting.py`) picks them up
   automatically and adds a `TelegramAlertSink` alongside the existing
   logging/JSONL/webhook sinks — halts, circuit-breaker trips, and
   reconciliation discrepancies are pushed the moment they happen, subject
   to the same `HysteresisAlertSink` de-duplication as every other channel.
   No code change or extra flag is needed; omitting the two env vars just
   skips this sink.

2. **Periodic heartbeat**, independent of whether anything happened:

   ~~~powershell
   $env:TELEGRAM_BOT_TOKEN = "<bot token from @BotFather>"
   $env:TELEGRAM_CHAT_ID = "<chat id — message the bot once, then check
     https://api.telegram.org/bot<token>/getUpdates for the numeric id>"
   python -m core.telegram_heartbeat --status reports/live_status.json --alerts reports/live_alerts.jsonl
   ~~~

   Schedule this on a timer (every 1-4 hours is typical for a sandbox soak
   run) — Windows Task Scheduler, or `crontab`/`systemd timer` on Linux. It
   reads the same `live_status.json`/`live_alerts.jsonl` the CLI dashboard
   reads (never opens the state database) and posts equity, health state,
   positions, and the last few alerts. Exit code 2 means the status
   snapshot itself was invalid — a heartbeat still gets sent in that case,
   saying exactly that, rather than silently going quiet.
