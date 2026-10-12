"""Operations and governance: admission gates, gray release, health policy, preflight, incident records.

These modules sit above ``core`` and below ``config`` (see ``[tool.importlinter]`` in pyproject.toml): they may
import ``core`` but nothing in ``core`` imports them. Modules the trading kernel itself needs (``core.alerting``,
``core.live_safety``, ``core.runtime_identity``, ``core.redaction``, ``core.sqlite_backup``) stay in ``core``.
"""
