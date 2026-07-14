# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import logging
import os
import sys
from logging import Formatter, Logger, StreamHandler

FORMATTER = Formatter("[cpmux] [%(levelname)s] %(message)s")
LOG_LEVEL = getattr(logging, os.environ.get("CPMUX_LOG_LEVEL", "INFO").upper(), logging.INFO)


def _console_handler() -> StreamHandler:
    handler = StreamHandler(stream=sys.stderr)
    handler.setFormatter(FORMATTER)

    return handler


def get_logger(logger_name: str) -> Logger:
    """Return a cpmux stderr logger.

    Args:
        logger_name: Logger name.

    Returns:
        Configured stderr logger.

    """

    logger = logging.getLogger(logger_name)
    logger.setLevel(LOG_LEVEL)

    if not logger.handlers:
        logger.addHandler(_console_handler())
    logger.propagate = False

    return logger
