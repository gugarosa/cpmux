# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import logging
import sys

from cmux.logging import get_logger


def test_get_logger_returns_logger_instance():
    logger = get_logger("cmux.test.instance")
    assert isinstance(logger, logging.Logger)


def test_get_logger_disables_propagation():
    logger = get_logger("cmux.test.propagate")
    assert logger.propagate is False


def test_get_logger_streams_to_stderr():
    logger = get_logger("cmux.test.stderr")
    assert logger.handlers
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_get_logger_is_idempotent_when_called_twice():
    first = get_logger("cmux.test.idempotent")
    count = len(first.handlers)
    second = get_logger("cmux.test.idempotent")
    assert first is second
    assert len(second.handlers) == count
