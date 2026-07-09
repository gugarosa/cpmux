# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import logging
import os
import sys
from logging import Formatter, Logger, StreamHandler

FORMATTER = Formatter("[cmux] [%(levelname)s] %(message)s")
LOG_LEVEL = getattr(logging, os.environ.get("CMUX_LOG_LEVEL", "INFO").upper(), logging.INFO)


def _console_handler() -> StreamHandler:
    handler = StreamHandler(stream=sys.stderr)
    handler.setFormatter(FORMATTER)

    return handler


def get_logger(logger_name: str) -> Logger:
    """Create a cmux logger that writes to stderr.

    Args:
        logger_name: Name of the logger.

    Returns:
        Configured logger with a single stderr handler.

    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(LOG_LEVEL)

    if not logger.handlers:
        logger.addHandler(_console_handler())
    logger.propagate = False

    return logger
