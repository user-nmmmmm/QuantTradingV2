# Contributing

## Setup
```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.lock.txt -r requirements-dev.txt   # runtime versions from the lock, as CI does
python -m pip install -e . --no-deps      # makes `import core` etc. work from any directory
# optional ML research dependencies
python -m pip install -r requirements-ml.txt
```
The editable install is the supported mode: many modules resolve `config/`,
`data/` and `reports/` relative to the checkout, so wheels are not supported
yet. Do not add `sys.path` tweaks to scripts or tests; import the packages
directly. Console entry points: `quant-backtest`, `quant-live`,
`quant-resolve-order`, `quant-dashboard`, `quant-dev`.
CI uses Python 3.11; the repository pin in `.python-version` is for local use.

## Before opening a PR
One command runs what CI's `quality` job runs, in the same order, and prints a summary:
```bash
python scripts/dev.py check              # or `quant-dev check`; add --no-tests to skip the long run
python scripts/dev.py list               # all tasks (lint, types, imports, ratchet, links, fast, ...)
```
The individual commands:
```bash
python -m ruff check .
python scripts/quality_ratchet.py check
lint-imports
python -m mypy core/foundation/domain.py core/foundation/runtime.py live_trading/execution_adapter.py
python -m pytest -q -n 4          # add -m "not slow" for a quick loop
python scripts/check_repository_hygiene.py
python scripts/verify_roadmap_completion.py --structure-only
python scripts/check_markdown_links.py
python scripts/check_environment.py --strict-lock   # the environment matches requirements.lock.txt
python scripts/regenerate_lock.py --check           # lock is canonical and covers requirements.txt
python scripts/data_inputs.py verify                # tracked market data matches data/binance/1d/_frozen_inputs.json
```

Optional: `python -m pip install pre-commit && pre-commit install` runs the same checks
before each commit. How the quality gates and the ratchet work: `docs/quality.md`.

Decisions that shaped these rules are recorded in `docs/adr/`. A dev container
(`.devcontainer/`) is provided for a ready-made Linux/Python 3.11 environment; the `devcontainer` workflow builds it and runs `dev.py check --no-tests` inside it whenever its inputs change and weekly.

## Paying down the quality backlog
The ratchet freezes the existing ruff/mypy findings; it does not excuse them (`docs/quality.md`). The convention:
- A PR that touches a file also reduces that file's findings where the fix is mechanical and cannot change behavior
  (unused imports and variables, `raise ... from`, explicit loop-variable binding). Run
  `python scripts/quality_ratchet.py update` and commit the lower baseline so the improvement cannot be lost.
- Do not fix a finding in code you are not otherwise changing if the fix could alter trading behavior; write a
  characterization test first, or leave it and note it in the PR.
- When a rule reaches zero, promote it to the hard gate (`[tool.ruff.lint] select`) in its own PR: first
  `F401/F811/F841/E4/E7`, then `B`.
- Once a quarter, compare the baseline totals with the previous quarter's and record the numbers in
  `docs/quality.md`; a baseline that has not moved in a quarter needs a reason or a plan.

## Rules of thumb
- One PR, one kind of change: moving files, behavior changes and doc edits are
  reviewed separately.
- Do not change order, fee, risk or protective-stop behavior without a written
  contract update and a regression against the frozen baselines.
- Do not edit files listed in `docs/file_retention_manifest.json` or the dated
  snapshots under `docs/archive/2026-08*` and `docs/archive/2026-09*`.
- Keep dependency changes in their own PR and regenerate
  `requirements.lock.txt` / `requirements.lock.sha256` with it
  (`python scripts/regenerate_lock.py`; `docs/dependency_management.md`). Never edit the lock by hand.
- Adding or replacing a tracked market-data CSV: `git add` it, run `python scripts/data_inputs.py build`, and review
  the manifest diff in the same PR (`docs/data_identity.md`).
- Moving, merging or archiving a tracked document: append an entry to `docs/doc_index.json`
  (old path -> new path); the link check and the roadmap verifiers resolve moves through it.
- Changes under `config/frozen_scope.json` paths: CI prints the frozen-scope identity impact in the job
  summary; comment/layout-only edits do not change it (`docs/identity.md`).
- Releases: user-visible or frozen-scope changes get a line under `## [Unreleased]` in `CHANGELOG.md`; versions,
  tags and the tag-bound identity record are described in `docs/releasing.md` (tags are created by the maintainer).
- Docs: one topic, one file; append dated sections instead of creating
  `*_YYYYMMDD.md` copies. See `docs/README.md`.
