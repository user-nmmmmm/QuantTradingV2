import logging
import os
import re
from typing import Optional, Union


_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_CONFIGURED = False


class SensitiveDataFilter(logging.Filter):
    """Remove credentials and common authentication headers from log records."""

    _patterns = (
        re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;}]+)"),
        re.compile(
            r"(?i)((?:api[_-]?key|secret|signature|x-mbx-apikey)\s*[:=]\s*)"
            r"([^\s,;}]+)"
        ),
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for name in ("EXCHANGE_API_KEY", "EXCHANGE_SECRET", "EXCHANGE_PASSWORD"):
            value = os.getenv(name)
            if value:
                message = message.replace(value, "[REDACTED]")
        for pattern in self._patterns:
            message = pattern.sub(r"\1[REDACTED]", message)
        record.msg = message
        record.args = ()
        return True


def _coerce_level(level: Union[int, str, None]) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str):
        normalized = level.upper()
        resolved = logging.getLevelName(normalized)
        if isinstance(resolved, int):
            return resolved
    return logging.INFO


def configure_logging(level: Union[int, str, None] = None) -> None:
    global _CONFIGURED

    effective_level = _coerce_level(level or os.getenv("QUANT_LOG_LEVEL"))
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(level=effective_level, format=_DEFAULT_FORMAT)
    else:
        root_logger.setLevel(effective_level)
        for handler in root_logger.handlers:
            if handler.formatter is None:
                handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))

    for handler in root_logger.handlers:
        if not any(isinstance(item, SensitiveDataFilter) for item in handler.filters):
            handler.addFilter(SensitiveDataFilter())
    _CONFIGURED = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
