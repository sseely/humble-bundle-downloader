"""Streaming downloader: executes planner actions against signed URLs.

This is the imperative shell for files. Each file is streamed into a ``.part``
file (always restarted from zero), verified, then placed atomically without
ever overwriting an existing file.
"""

import asyncio
import errno
import hashlib
import os
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from humble_dl.config import Settings
from humble_dl.lib.logging import FILE_NAME_KEY, get_logger, redact_url
from humble_dl.services.planner import Action, ArchiveThenFetch, Fetch, FileKey, Skip

LOGGER_NAME = "humble_dl.downloader"
PART_SUFFIX = ".part"
CHUNK_SIZE = 64 * 1024
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
BACKOFF_FACTOR = 2
STATUS_FORBIDDEN = 403
STATUS_GONE = 410
STATUS_SERVER_ERROR_MIN = 500
AUTH_EXPIRED_STATUSES = frozenset({STATUS_FORBIDDEN, STATUS_GONE})
# errno values meaning "hard links unavailable here" (exFAT, network volumes).
LINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV})
EVENT_DOWNLOADED = "file_downloaded"
EVENT_FAILED = "file_failed"
EVENT_ARCHIVED = "file_archived"
_URL_PATTERN = re.compile(r"https?://[^\s'\"]+")

type Refresh = Callable[[str], Awaitable[dict[FileKey, str]]]
type Sleep = Callable[[float], Awaitable[None]]
type Status = Literal["skipped", "downloaded", "archived_and_downloaded", "failed"]


@dataclass(frozen=True)
class Result:
    """Outcome of one action."""

    path: Path
    status: Status
    bytes: int
    md5: str | None
    error: str | None


class _Failure(Exception):
    """Expected per-file failure; the message is safe to surface."""


class _AuthExpired(Exception):
    """The signed URL was rejected (403/410)."""


class _Transient(Exception):
    """A retryable network or server error."""


@dataclass(frozen=True)
class _Written:
    size: int
    md5: str


def _scrub(text: str) -> str:
    """Strip query strings and userinfo from any URL embedded in ``text``."""
    return _URL_PATTERN.sub(lambda m: redact_url(m.group(0)), text)


def _part_path(path: Path) -> Path:
    return path.with_name(path.name + PART_SUFFIX)


def _place_no_clobber(src: Path, dst: Path) -> None:
    """Move ``src`` to ``dst`` atomically, refusing to replace an existing ``dst``.

    ``os.link`` fails with ``FileExistsError`` if ``dst`` exists, so there is no
    check-then-replace window. Where hard links are unsupported, falls back to an
    existence check plus ``os.replace`` (that fallback is not race-free).

    Raises:
        FileExistsError: ``dst`` already exists.
        OSError: any other filesystem failure.
    """
    try:
        os.link(src, dst)
    except OSError as exc:
        if exc.errno not in LINK_UNSUPPORTED_ERRNOS:
            raise
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "destination exists", str(dst)) from exc
        os.replace(src, dst)
        return
    os.unlink(src)


def _check_status(status: int) -> None:
    if status in AUTH_EXPIRED_STATUSES:
        raise _AuthExpired
    if status >= STATUS_SERVER_ERROR_MIN:
        raise _Transient(f"server error {status}")
    if not 200 <= status < 300:
        raise _Failure(f"HTTP {status}")


async def _write_body(response: httpx.Response, part: Path) -> _Written:
    digest = hashlib.md5(usedforsecurity=False)
    total = 0
    with part.open("wb") as handle:
        async for chunk in response.aiter_bytes(CHUNK_SIZE):
            handle.write(chunk)
            digest.update(chunk)
            total += len(chunk)
    return _Written(total, digest.hexdigest())


def _verify(fetch: Fetch, written: _Written) -> None:
    if fetch.size is not None and fetch.size != written.size:
        raise _Failure(f"size mismatch: expected {fetch.size}, got {written.size}")
    if fetch.md5 is not None and fetch.md5.casefold() != written.md5:
        raise _Failure(f"md5 mismatch: expected {fetch.md5.casefold()}, got {written.md5}")


def _log_fields(path: Path, url: str, size: int, event: str) -> dict[str, object]:
    return {
        "event": event,
        "bundle": path.parent.parent.name,
        "item": path.parent.name,
        FILE_NAME_KEY: path.name,
        "bytes": size,
        "url": redact_url(url),
    }


class _Runner:
    """Per-``execute`` state: semaphore, refresh memo and locks."""

    def __init__(
        self, client: httpx.AsyncClient, refresh: Refresh, settings: Settings, sleep: Sleep
    ) -> None:
        self._client = client
        self._refresh = refresh
        self._timeout = settings.http_timeout_s
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(settings.concurrency)
        # Both maps are touched only from the event loop thread; the per-gamekey
        # lock serialises the check-then-refresh sequence across awaits.
        self._locks: dict[str, asyncio.Lock] = {}
        self._refreshed: dict[str, dict[FileKey, str] | _Failure] = {}
        self._log = get_logger(LOGGER_NAME)

    async def run(self, action: Action) -> Result:
        if isinstance(action, Skip):
            return Result(action.path, "skipped", 0, None, None)
        fetch = action.fetch if isinstance(action, ArchiveThenFetch) else action
        async with self._semaphore:
            try:
                return await self._perform(action, fetch)
            except _Failure as exc:
                message = _scrub(str(exc))
            except OSError as exc:
                message = _scrub(f"{type(exc).__name__}: {exc}")
        self._log.error(
            message, extra=_log_fields(fetch.path, fetch.url, 0, EVENT_FAILED) | {"error": message}
        )
        return Result(fetch.path, "failed", 0, None, message)

    async def _perform(self, action: Fetch | ArchiveThenFetch, fetch: Fetch) -> Result:
        written = await self._download(fetch)
        part = _part_path(fetch.path)
        archived = isinstance(action, ArchiveThenFetch)
        try:
            if isinstance(action, ArchiveThenFetch):
                self._archive(action)
            _place_no_clobber(part, fetch.path)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise _Failure(f"could not place file: {exc}") from exc
        self._log.info(
            EVENT_DOWNLOADED,
            extra=_log_fields(fetch.path, fetch.url, written.size, EVENT_DOWNLOADED),
        )
        status: Status = "archived_and_downloaded" if archived else "downloaded"
        return Result(fetch.path, status, written.size, written.md5, None)

    def _archive(self, action: ArchiveThenFetch) -> None:
        size = action.existing.stat().st_size
        _place_no_clobber(action.existing, action.archive_to)
        self._log.info(
            EVENT_ARCHIVED,
            extra=_log_fields(action.archive_to, action.fetch.url, size, EVENT_ARCHIVED),
        )

    async def _download(self, fetch: Fetch) -> _Written:
        fetch.path.parent.mkdir(parents=True, exist_ok=True)
        part = _part_path(fetch.path)
        written = await self._stream_with_refresh(fetch, part)
        try:
            _verify(fetch, written)
        except _Failure:
            part.unlink(missing_ok=True)
            raise
        return written

    async def _stream_with_refresh(self, fetch: Fetch, part: Path) -> _Written:
        try:
            return await self._stream_with_retry(fetch.url, part)
        except _AuthExpired:
            url = await self._refreshed_url(fetch.key)
        try:
            return await self._stream_with_retry(url, part)
        except _AuthExpired:
            raise _Failure("download URL still rejected after refresh") from None

    async def _refreshed_url(self, key: FileKey) -> str:
        lock = self._locks.setdefault(key.gamekey, asyncio.Lock())
        async with lock:
            if key.gamekey not in self._refreshed:
                try:
                    self._refreshed[key.gamekey] = await self._refresh(key.gamekey)
                except Exception as exc:  # noqa: BLE001 - refresh is caller-supplied; fail the file only
                    self._refreshed[key.gamekey] = _Failure(f"refresh failed: {exc}")
            outcome = self._refreshed[key.gamekey]
        if isinstance(outcome, _Failure):
            raise outcome
        url = outcome.get(key)
        if url is None:
            raise _Failure("refreshed order no longer lists this file")
        return url

    async def _stream_with_retry(self, url: str, part: Path) -> _Written:
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                return await self._stream_once(url, part)
            except _Transient as exc:
                last = str(exc)
                if attempt < MAX_ATTEMPTS - 1:
                    await self._sleep(BACKOFF_BASE_S * BACKOFF_FACTOR**attempt)
        raise _Failure(f"download failed after {MAX_ATTEMPTS} attempts: {last}")

    async def _stream_once(self, url: str, part: Path) -> _Written:
        try:
            async with self._client.stream("GET", url, timeout=self._timeout) as response:
                _check_status(response.status_code)
                return await _write_body(response, part)
        except httpx.HTTPError as exc:
            raise _Transient(f"network error ({type(exc).__name__})") from exc


async def execute(
    actions: Sequence[Action],
    client: httpx.AsyncClient,
    refresh: Refresh,
    settings: Settings,
    *,
    sleep: Sleep = asyncio.sleep,
) -> list[Result]:
    """Execute planner actions with bounded concurrency.

    One failing action never aborts the others.

    Args:
        actions: Actions from the planner.
        client: HTTP client used for streaming downloads.
        refresh: Returns fresh signed URLs for a gamekey; called at most once per
            gamekey per call.
        settings: Supplies concurrency and the HTTP timeout.
        sleep: Backoff sleep; injectable so tests do not wait.

    Returns:
        One ``Result`` per action, in input order.
    """
    runner = _Runner(client, refresh, settings, sleep)
    return list(await asyncio.gather(*(runner.run(action) for action in actions)))
