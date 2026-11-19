"""Compatibility entrypoint for operational incident records."""

from ops.operations.incident_journal import main, record_incident

__all__ = ["main", "record_incident"]


if __name__ == "__main__":
    raise SystemExit(main())
