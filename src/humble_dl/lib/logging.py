"""Structured JSON-lines logging with URL and gamekey redaction.

This module is named ``logging`` but lives in the ``humble_dl.lib`` package;
``import logging`` below resolves to the standard library (absolute imports).
"""

import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import IO, Any
from urllib.parse import urlsplit

SERVICE_NAME = "humble-dl"
LOG_LEVEL_ENV = "HUMBLE_DL_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "info"
GAMEKEY_VISIBLE_CHARS = 4
ELLIPSIS = "…"
FILE_NAME_KEY = "file_name"  # extra key; emitted as "filename" (LogRecord reserves it)
OUTPUT_FILENAME_KEY = "filename"

_LEVEL_LABELS = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}
# Attributes every LogRecord carries, plus those the logging machinery adds.
_RESERVED_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JsonFormatter(logging.Formatter):
    """Format records as one JSON object per line."""

    def __init__(self, clock: Clock = _utc_now) -> None:
        """Create a formatter.

        Args:
            clock: Returns the timestamp for each record; injectable for tests.
        """
        super().__init__()
        self._clock = clock

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as a JSON line (no trailing newline)."""
        payload: dict[str, Any] = {
            "level": _LEVEL_LABELS.get(record.levelno, record.levelname.lower()),
            "msg": record.getMessage(),
            "ts": self._clock().isoformat(),
            "service": SERVICE_NAME,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS:
                payload[OUTPUT_FILENAME_KEY if key == FILE_NAME_KEY else key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _StderrHandler(logging.StreamHandler[IO[str]]):
    """Stream handler that always writes to the *current* ``sys.stderr``."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    @property
    def stream(self) -> IO[str]:  # pyright: ignore[reportIncompatibleVariableOverride]
        return sys.stderr

    @stream.setter
    def stream(self, value: IO[str]) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Ignore assignment; the stream is always ``sys.stderr``."""


def _resolve_level(level: str | None) -> int:
    name = (level or os.environ.get(LOG_LEVEL_ENV) or DEFAULT_LOG_LEVEL).upper()
    resolved = logging.getLevelNamesMapping().get(name)
    return resolved if resolved is not None else logging.INFO


def get_logger(name: str, level: str | None = None, clock: Clock = _utc_now) -> logging.Logger:
    """Return a logger that writes JSON lines to stderr.

    Idempotent: repeated calls for the same name never attach a second handler.

    Args:
        name: Logger name.
        level: Level name; defaults to ``HUMBLE_DL_LOG_LEVEL`` then ``info``.
        clock: Timestamp source for the formatter (applied on first call only).

    Returns:
        The configured logger.
    """
    logger = logging.getLogger(name)
    if not any(isinstance(h, _StderrHandler) for h in logger.handlers):
        handler = _StderrHandler()
        handler.setFormatter(JsonFormatter(clock))
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(_resolve_level(level))
    return logger


def redact_url(url: str) -> str:
    """Return ``scheme://host[:port]/path`` with userinfo, query and fragment removed."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return f"{parts.scheme}://{host}{parts.path}"


def redact_gamekey(gamekey: str) -> str:
    """Return the first four characters of ``gamekey`` followed by an ellipsis.

    Keys of eight characters or fewer reveal at most half their characters.
    """
    visible = min(GAMEKEY_VISIBLE_CHARS, len(gamekey) // 2)
    return gamekey[:visible] + ELLIPSIS
