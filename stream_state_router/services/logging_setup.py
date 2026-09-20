from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .paths import logs_dir


def _configure_dependency_logging() -> None:
    """Silence dependency tracebacks for errors SSR already normalizes.

    obsws-python currently logs connection/request failures with
    logger.exception() before re-raising them. SSR catches those exceptions,
    exposes a concise OBS status in the UI, and records the state transition in
    its own rotating log. Letting the dependency record propagate to Python's
    last-resort handler therefore floods the console during normal OBS startup
    or reconnect periods.
    """
    dependency_logger = logging.getLogger("obsws_python")
    if not any(isinstance(handler, logging.NullHandler) for handler in dependency_logger.handlers):
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
