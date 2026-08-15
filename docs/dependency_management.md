# Dependency locking

Python 3.11 is the supported development and CI baseline.

- `requirements.txt` is the exact runtime manifest.
- `requirements.lock.txt` is the exact transitive runtime lock.
- `requirements-dev.txt` adds pinned test, lint, type-check, and coverage tools.
- `requirements.lock.sha256` detects silent lock-file changes.

Install runtime dependencies with `python -m pip install -r requirements.lock.txt`. Install contributor tooling with `python -m pip install -r requirements-dev.txt`. Run `python scripts/check_environment.py --strict-lock` to compare the active environment with the lock, and `python scripts/verify_lock.py` to validate exact pins and the committed SHA-256.

The runtime lock is platform-neutral where wheels are available. Record any required platform markers directly in the lock and regenerate the checksum in the same change.
