from __future__ import annotations

import logging
import unittest

from stream_state_router.services.logging_setup import _configure_dependency_logging


class DependencyLoggingTests(unittest.TestCase):
    def test_obsws_python_records_do_not_propagate_to_console_handlers(self):
        logger = logging.getLogger("obsws_python")
        previous_handlers = list(logger.handlers)
        previous_propagate = logger.propagate
        try:
            logger.handlers.clear()
            logger.propagate = True

            _configure_dependency_logging()

            self.assertFalse(logger.propagate)
            self.assertTrue(
                any(isinstance(handler, logging.NullHandler) for handler in logger.handlers)
            )
        finally:
            logger.handlers[:] = previous_handlers
            logger.propagate = previous_propagate

    def test_dependency_logging_configuration_is_idempotent(self):
        logger = logging.getLogger("obsws_python")
        previous_handlers = list(logger.handlers)
        previous_propagate = logger.propagate
        try:
            logger.handlers.clear()
            logger.propagate = True

            _configure_dependency_logging()
            _configure_dependency_logging()

            null_handlers = [
                handler
                for handler in logger.handlers
                if isinstance(handler, logging.NullHandler)
            ]
            self.assertEqual(len(null_handlers), 1)
        finally:
            logger.handlers[:] = previous_handlers
            logger.propagate = previous_propagate


if __name__ == "__main__":
    unittest.main()
