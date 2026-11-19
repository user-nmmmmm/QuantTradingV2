# Security policy

This repository contains trading research and exchange-connected code. It is
not production-approved: see `docs/unified_roadmap.md` for the current
admission status.

## Reporting a vulnerability

Do **not** post vulnerability details in public issues, pull requests or logs.
Use [Report a vulnerability](https://github.com/user-nmmmmm/QuantTradingV2/security/advisories/new)
from this repository's [Security advisories](https://github.com/user-nmmmmm/QuantTradingV2/security/advisories)
page and sign in to GitHub if asked. Include affected versions/files, impact
and minimal reproduction steps in the **private** form. Remove credentials,
account identifiers and unrelated private data from attachments.

If the private form is unavailable, open a public contact request containing
only: "Please provide a private security reporting contact." Do not include
the vulnerability, affected versions, impact, reproduction or attachments;
wait for a confirmed private route before sending any details. No fallback
mailbox is currently published. A policy file does not enable GitHub's
reporting feature or prove that a maintainer received a report.

Maintainers: see the [channel checks and owner steps](docs/repository_governance.md)
for enablement, notifications and evidence that must remain private.

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
