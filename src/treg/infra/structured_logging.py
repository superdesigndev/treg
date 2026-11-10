"""Plain event output scoped to named loggers, without configuring the application root."""
from __future__ import annotations

import logging


def get_logger(name: str) -> logging.Logger:
    """Keep JSON on one physical line while preserving inherited levels and explicit sinks.

    The default sink bypasses wrapping root formatters. Operators can configure the named
    logger's handlers and filters explicitly; an existing named handler is left untouched.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
        logger.propagate = False
    return logger
