# R6 monitoring operations

## EOD reconciliation

Export venue/account facts to JSON:

~~~json
{"cash": {"USDT": "1000"}, "positions": {"BTC/USDT": {"qty": "0.1"}}}
~~~

Schedule this command after the venue trading day closes:

~~~powershell
python -m core.reconciliation_job --ledger-db reports/ledger.db --account-id primary --base-currency USDT --external-state reports/external_eod.json
~~~

The job atomically writes one structured report per account/day under
`reports/reconciliation/`. A clean report is still persisted with zero
discrepancies. The command exits 0 for a clean match and 2 for discrepancies.

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
