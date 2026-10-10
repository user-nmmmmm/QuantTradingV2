# Contributing

## Setup
```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements-dev.txt
# optional ML research dependencies
python -m pip install -r requirements-ml.txt
```
CI uses Python 3.11; the repository pin in `.python-version` is for local use.

## Before opening a PR
```bash
python -m ruff check .
python -m mypy core/domain.py core/runtime.py live_trading/execution_adapter.py
python -m pytest -q
python scripts/check_repository_hygiene.py
python scripts/verify_roadmap_completion.py --structure-only
```

## Rules of thumb
- One PR, one kind of change: moving files, behavior changes and doc edits are
  reviewed separately.
- Do not change order, fee, risk or protective-stop behavior without a written
  contract update and a regression against the frozen baselines.
- Do not edit files listed in `docs/file_retention_manifest.json` or the dated
  snapshots under `docs/archive/2026-08*` and `docs/archive/2026-09*`.
- Keep dependency changes in their own PR and regenerate
  `requirements.lock.txt` / `requirements.lock.sha256` with it
  (`docs/dependency_management.md`).
- Docs: one topic, one file; append dated sections instead of creating
  `*_YYYYMMDD.md` copies. See `docs/README.md`.
