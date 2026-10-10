# Security policy

This repository contains trading research and exchange-connected code. It is
not production-approved: see `docs/unified_roadmap.md` for the current
admission status.

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
private vulnerability reporting ("Security" tab, "Report a vulnerability") for
this repository. Include affected files, impact and reproduction steps.

## Handling credentials

- Never commit API keys, secrets, passwords or account identifiers. The CLI has
  no credential flags on purpose; keys come from the environment.
- `run_live.py` defaults to sandbox/testnet; real endpoints need explicit
  `--live`.
- If a secret was ever committed, treat it as compromised: revoke and rotate it
  first, then clean history.

## Scope

In scope: credential leakage, unsafe order submission paths, bypass of the
live-safety gates (`docs/live_safety.md`), supply-chain issues in pinned
dependencies. Trading losses from strategy behavior are not security issues.
