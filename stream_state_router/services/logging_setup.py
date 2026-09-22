from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import logs_dir


def _configure_dependency_logging() -> None:
    """Silence dependency tracebacks for failures SSR already normalizes."""

    dependency_logger = logging.getLogger("obsws_python")
    if not any(
        isinstance(handler, logging.NullHandler)
        for handler in dependency_logger.handlers
    ):
        dependency_logger.addHandler(logging.NullHandler())
    dependency_logger.propagate = False


def configure_logging() -> logging.Logger:
    _configure_dependency_logging()
    logger = logging.getLogger("stream_state_router")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    handler = RotatingFileHandler(
        logs_dir() / "stream-state-router.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
