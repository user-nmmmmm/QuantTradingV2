import logging
import os
from typing import Optional, Union


_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_CONFIGURED = False


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

    _CONFIGURED = True


def get_logger(name: Optional[str] = None) -> logging.Logger:
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(name)
