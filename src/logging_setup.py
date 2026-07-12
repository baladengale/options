"""
Shared logging — used by screener, OIE engine, and portfolio check.
Writes to logs/ directory. Each module gets its own logger.
"""
import logging
import os
import sys
from src.paths import LOG_DIR, LOG_PATH

# Root logger — file gets everything, console gets INFO+
_file_handler = logging.FileHandler(LOG_PATH)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(name)s] %(levelname)s %(message)s'))

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(name)s] %(message)s'))

_root = logging.getLogger('options')
_root.setLevel(logging.DEBUG)
_root.addHandler(_file_handler)
_root.addHandler(_console_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a module. Name is used as prefix in output."""
    return _root.getChild(name)
