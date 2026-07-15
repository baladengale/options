"""
Shared logging — used by screener, OIE engine, and portfolio check.
Writes to logs/ directory. Each module gets its own logger.
"""
import logging
from src.paths import LOG_DIR, LOG_PATH

# Root logger — file gets everything, console gets INFO+
_file_handler = logging.FileHandler(LOG_PATH)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(name)s] %(levelname)s %(message)s'))

_root = logging.getLogger('options')
_root.setLevel(logging.DEBUG)
_root.addHandler(_file_handler)
_root.propagate = False  # Block moomoo/third-party root handlers from echoing to console


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a module. Name is used as prefix in output."""
    return _root.getChild(name)
