"""
utils/logger.py
───────────────
Centralised logging configuration.
All modules import `logger` from here for consistent formatting.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# ── Read log level from environment (default: INFO) ──────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
NUMERIC_LEVEL = getattr(logging, LOG_LEVEL, logging.INFO)

# ── Formatter ─────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

# ── Root logger ───────────────────────────────────────────────────────────────
logger = logging.getLogger("tg_discord_bot")
logger.setLevel(NUMERIC_LEVEL)

# Console handler — always on
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(formatter)
logger.addHandler(_console_handler)

# File handler — rotates at 5 MB, keeps 3 backups
_log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
os.makedirs(_log_dir, exist_ok=True)
_file_handler = RotatingFileHandler(
    filename=os.path.join(_log_dir, "bot.log"),
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(formatter)
logger.addHandler(_file_handler)
