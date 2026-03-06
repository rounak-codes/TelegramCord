"""
utils/dedup.py
──────────────
A deduplication cache that persists seen Telegram message IDs to disk,
so the bot never re-posts a message it already forwarded — even after
a restart.

Uses a fixed-size collections.deque in memory, and a simple JSON file
on disk that is loaded on startup and updated after every successful post.
"""

import json
import os
from collections import deque
from utils.logger import logger

# Absolute path to the persistent cache file — always relative to project root
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_CACHE_FILE = os.path.join(_PROJECT_ROOT, "data", "seen_messages.json")


class DeduplicationCache:
    """
    Stores recently seen Telegram message IDs both in memory and on disk.
    On init, loads previously seen IDs from disk so restarts don't cause
    duplicate posts.
    """

    def __init__(self, max_size: int | None = None):
        _env_size = os.getenv("DEDUP_CACHE_SIZE")
        size = int(_env_size) if _env_size else (max_size or 500)
        self._cache: deque[int] = deque(maxlen=size)
        self._size = size
        self._cache_file = _CACHE_FILE

        # Ensure the data directory exists
        os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)

        # Load previously seen IDs from disk
        self._load()
        logger.info(
            "DeduplicationCache initialised — max_size=%d, loaded=%d IDs from disk (cache file: %s)",
            size, len(self._cache), self._cache_file,
        )

    def _load(self) -> None:
        """Load seen message IDs from the JSON file on disk."""
        if not os.path.exists(self._cache_file):
            return
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                ids: list[int] = json.load(f)
            for msg_id in ids:
                self._cache.append(msg_id)
            logger.info("Loaded %d seen message IDs from disk cache", len(ids))
        except Exception as exc:
            logger.warning("Could not load dedup cache from disk: %s", exc)

    def _save(self) -> None:
        """Persist current seen IDs to disk."""
        try:
            tmp_file = self._cache_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(list(self._cache), f)
            os.replace(tmp_file, self._cache_file)
            logger.debug("Saved %d IDs to disk cache: %s", len(self._cache), self._cache_file)
        except Exception as exc:
            logger.error("FAILED to save dedup cache to disk: %s — path: %s", exc, self._cache_file)

    def seen(self, message_id: int) -> bool:
        """Return True if this message_id was already processed."""
        return message_id in self._cache

    def add(self, message_id: int) -> None:
        """Mark a message_id as processed and persist to disk."""
        if message_id not in self._cache:
            self._cache.append(message_id)
            self._save()
            logger.info(
                "Dedup cache: saved message_id=%d (cache size: %d/%d)",
                message_id, len(self._cache), self._size,
            )

    def __len__(self) -> int:
        return len(self._cache)