## Summary
<!-- What changes and why, in a few lines. -->

## Scope
- [ ] Docs only
- [ ] Tooling / CI / repo hygiene
- [ ] Behavior change in `core/`, `backtest/`, `live_trading/` or `strategies/`

## Safety checklist (tick or write N/A)
- [ ] No change to order, fill, fee, risk or protective-stop semantics (or the change is described above with a rollback plan)
- [ ] No secrets, API keys or account identifiers in code, docs, fixtures or logs
- [ ] Frozen artifacts untouched (`docs/file_retention_manifest.json`, `docs/archive/2026-0[89]*`, `reports/` receipts), or the change is explained
- [ ] Source/config identity of frozen research candidates considered (comment-only edits also change source hashes)

## Test plan
- [ ] `python -m pytest -q`
- [ ] `python -m ruff check .`
- [ ] `python scripts/check_repository_hygiene.py`
- [ ] Other (describe):
