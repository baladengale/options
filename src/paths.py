"""
Project paths — configurable via OPTIONS_HOME env var.
All modules import from here so the project can be relocated.

Set OPTIONS_HOME to the project root directory.
Default: auto-detected from this file's location (../.. from src/).
"""
import os

_HOME = os.environ.get('OPTIONS_HOME')
if _HOME:
    PROJECT_ROOT = os.path.abspath(_HOME)
else:
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

DB_DIR = os.path.join(PROJECT_ROOT, 'db')
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs')
CONFIG_DIR = os.path.join(PROJECT_ROOT, 'config')

OIE_DB_PATH = os.path.join(DB_DIR, 'oie_paper.db')
RULES_PATH = os.path.join(CONFIG_DIR, 'rules.yaml')
LOG_PATH = os.path.join(LOG_DIR, 'options.log')

# Ensure directories exist
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)
