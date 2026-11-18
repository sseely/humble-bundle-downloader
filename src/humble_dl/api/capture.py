"""Record library traffic and write sanitized fixtures.

Sanitization is a security boundary: captured output is committed as test
fixtures. Everything is sanitized in memory (pure functions), verified with
``assert_clean``, and only then written to disk. Response headers are never
persisted and non-JSON bodies are never written.
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, unquote, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from humble_dl.auth.session import HUMBLE_BASE, MS_PER_S, SessionContext, assert_logged_in
from humble_dl.config import Settings
from humble_dl.lib.logging import get_logger, redact_gamekey, redact_url

GAMEKEY_KEY = "gamekey"
FAKE_GAMEKEY_FORMAT = "GAMEKEY_{:04d}"
EMAIL_PLACEHOLDER = "user@example.invalid"
SECRET_KEYS = frozenset(
    {
        "gamekey",
        "email",
        "token",
        "csrf",
        "cookie",
        "session",
        "password",
        "secret",
        "auth",
        "authorization",
        "uid",
        "user_id",
        "userid",
        "address",
        "phone",
        "ip",
        "ip_address",
        "payment",
        "card",
        "billing",
        "api_key",
        "username",
        "full_name",
        "first_name",
        "last_name",
        "display_name",
    }
)
SECRET_KEY_FRAGMENTS = ("token", "csrf", "secret", "password", "cookie", "session", "email")

HUMBLE_DOMAIN = "humblebundle.com"
LIBRARY_PATH = "/home/library"
USER_ORDER_PATH = "/api/v1/user/order"
ORDER_PATH_PREFIX = "/api/v1/order"
ORDER_QUERY = "all_tpkds=true"
HTTP_GET = "GET"

MAX_PAGE_RESPONSES = 200
MAX_ORDERS = 5
SCROLL_STEPS = 3
SCROLL_DELTA_PX = 2000

CONTENT_TYPE_HEADER = "content-type"
JSON_CONTENT_TYPE = "json"
INDEX_NAME = "INDEX.md"
JSON_INDENT = 2
SLUG_MAX_CHARS = 80
EMPTY_SLUG = "root"
INDEX_HEADER = (
    "# Captured library traffic\n\n"
    "| NN | Method | Status | URL | File |\n"
    "|----|--------|--------|-----|------|\n"
)
NO_FILE = "-"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
ORDER_PATH_RE = re.compile(rf"^{re.escape(ORDER_PATH_PREFIX)}/([^/]+)")
SLUG_RE = re.compile(r"[^a-z0-9]+")

_log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class CaptureError(Exception):
    """Capture could not complete (browser or request failure); nothing was written."""


class CaptureLeakError(Exception):
    """Sanitized output still contains a captured secret; nothing was written."""


class _BodyLike(Protocol):
    @property
    def status(self) -> int: ...
    @property
    def headers(self) -> dict[str, str]: ...
    async def text(self) -> str: ...


class _RequestLike(Protocol):
    @property
    def method(self) -> str: ...


class PageResponse(_BodyLike, Protocol):
    """The slice of Playwright ``Response`` this module uses."""

    @property
    def url(self) -> str: ...
    @property
    def request(self) -> _RequestLike: ...


class _MouseLike(Protocol):
    async def wheel(self, delta_x: float, delta_y: float) -> None: ...


class CapturePage(Protocol):
    """The slice of Playwright ``Page`` this module uses."""

    @property
    def mouse(self) -> _MouseLike: ...
    def on(self, event: Literal["response"], f: Callable[[PageResponse], None]) -> None: ...
    async def goto(self, url: str, *, timeout: float | None = ...) -> object: ...
    async def wait_for_load_state(
        self, state: Literal["networkidle"] | None = ..., *, timeout: float | None = ...
    ) -> None: ...


class CaptureContext(SessionContext, Protocol):
    """The slice of ``BrowserContext`` ``run_capture`` uses (real contexts satisfy it)."""

    async def new_page(self) -> CapturePage: ...  # pyright: ignore[reportIncompatibleMethodOverride]


@dataclass(frozen=True)
class Record:
    """One recorded response; ``body`` is parsed JSON when ``has_json``."""

    url: str
    method: str
    status: int
    has_json: bool
    body: Any


# --- pure core ---------------------------------------------------------------


def assert_clean(text: str, secrets: Iterable[str]) -> None:
    """Raise if any secret appears in ``text`` (case-insensitive).

    Args:
        text: Output about to be written.
        secrets: Original gamekeys and emails captured from the traffic.

    Raises:
        CaptureLeakError: A secret is present. The message never contains it.
    """
    haystack = text.casefold()
    for secret in secrets:
        if secret and secret.casefold() in haystack:
            raise CaptureLeakError("Sanitized output still contains a captured secret")


def is_humble_host(url: str) -> bool:
    """Return True if ``url`` is on humblebundle.com or a subdomain."""
    host = (urlsplit(url).hostname or "").lower()
    return host == HUMBLE_DOMAIN or host.endswith(f".{HUMBLE_DOMAIN}")


def _fake_gamekey(gamekey: str, gamekey_map: dict[str, str]) -> str:
    if gamekey not in gamekey_map:
        gamekey_map[gamekey] = FAKE_GAMEKEY_FORMAT.format(len(gamekey_map) + 1)
    return gamekey_map[gamekey]


def _is_secret_key(folded: str) -> bool:
    return folded in SECRET_KEYS or any(frag in folded for frag in SECRET_KEY_FRAGMENTS)


def _replace_gamekeys(text: str, gamekey_map: dict[str, str]) -> str:
    """Case-insensitively replace known gamekeys, longest first (prefix-safe)."""
    if not gamekey_map:
        return text
    fakes = {gamekey.lower(): fake for gamekey, fake in gamekey_map.items()}
    keys = sorted(gamekey_map, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(key) for key in keys), re.IGNORECASE)
    return pattern.sub(lambda match: fakes[match.group(0).lower()], text)


def sanitize_string(text: str, gamekey_map: dict[str, str]) -> str:
    """Replace gamekeys and emails and strip URL queries/fragments in ``text``."""
    text = _replace_gamekeys(text, gamekey_map)
    text = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return URL_RE.sub(lambda match: redact_url(match.group(0)), text)


def _sanitize_dict(obj: dict[str, Any], gamekey_map: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        folded = str(key).casefold()
        if folded == GAMEKEY_KEY and isinstance(value, str) and value:
            out[key] = _fake_gamekey(value, gamekey_map)
        elif not _is_secret_key(folded):
            out[sanitize_string(str(key), gamekey_map)] = sanitize(value, gamekey_map)
    return out


def sanitize(obj: Any, gamekey_map: dict[str, str]) -> Any:
    """Return a sanitized deep copy of a JSON-like ``obj`` (pure apart from the map).

    Gamekeys become stable fakes (``GAMEKEY_0001`` ...) via ``gamekey_map``;
    emails become a placeholder; URL queries are stripped; secret keys are dropped.
    Gamekeys not yet in the map are added only when seen under a ``gamekey`` key,
    so pre-populate the map (see ``build_outputs``) to cover every occurrence.

    Args:
        obj: Parsed JSON value.
        gamekey_map: Real gamekey -> fake gamekey; mutated with new entries.

    Returns:
        The sanitized value.
    """
    if isinstance(obj, dict):
        mapping = cast("dict[str, Any]", obj)
        return _sanitize_dict(mapping, gamekey_map)
    if isinstance(obj, list):
        items = cast("list[Any]", obj)
        return [sanitize(item, gamekey_map) for item in items]
    if isinstance(obj, str):
        return sanitize_string(obj, gamekey_map)
    return obj


def _iter_gamekeys(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        mapping = cast("dict[str, Any]", obj)
        for key, value in mapping.items():
            if str(key).casefold() == GAMEKEY_KEY and isinstance(value, str) and value:
                yield value
            yield from _iter_gamekeys(value)
    elif isinstance(obj, list):
        items = cast("list[Any]", obj)
        for item in items:
            yield from _iter_gamekeys(item)


def list_gamekeys(body: Any) -> list[str]:
    """Return gamekeys from a ``/user/order`` style body (strings or ``{gamekey}``)."""
    if not isinstance(body, list):
        return []
    items = cast("list[Any]", body)
    keys: list[str] = [item for item in items if isinstance(item, str) and item]
    keys.extend(_iter_gamekeys([item for item in items if isinstance(item, dict)]))
    return keys


def _record_gamekeys(record: Record) -> list[str]:
    path = urlsplit(record.url).path
    keys: list[str] = []
    match = ORDER_PATH_RE.match(path)
    if match:
        keys.append(unquote(match.group(1)))
    if record.has_json:
        if path.rstrip("/") == USER_ORDER_PATH:
            keys.extend(list_gamekeys(record.body))
        keys.extend(_iter_gamekeys(record.body))
    return keys


def _iter_strings(obj: Any) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        mapping = cast("dict[str, Any]", obj)
        for key, value in mapping.items():
            yield str(key)
            yield from _iter_strings(value)
    elif isinstance(obj, list):
        items = cast("list[Any]", obj)
        for item in items:
            yield from _iter_strings(item)


def _record_emails(record: Record) -> list[str]:
    strings = [record.url, *_iter_strings(record.body)]
    found: list[str] = []
    for text in strings:
        found.extend(EMAIL_RE.findall(text))
        found.extend(EMAIL_RE.findall(unquote(text)))
    return [email for email in found if email.casefold() != EMAIL_PLACEHOLDER]


def _slug(path: str) -> str:
    slug = SLUG_RE.sub("-", path.lower()).strip("-")[:SLUG_MAX_CHARS].strip("-")
    return slug or EMPTY_SLUG


def _index_row(number: int, record: Record, safe_url: str, file_name: str | None) -> str:
    return (
        f"| {number:02d} | {record.method} | {record.status} "
        f"| `{redact_url(safe_url)}` | {file_name or NO_FILE} |\n"
    )


def _dump(value: Any) -> str:
    return json.dumps(value, indent=JSON_INDENT, ensure_ascii=False) + "\n"


def build_outputs(records: list[Record]) -> dict[str, str]:
    """Sanitize ``records`` into ``{file name: text}``; INDEX.md is last (pure).

    Pass 1 collects every gamekey (bodies, order lists, URL paths) so pass 2
    replaces them everywhere, including file slugs and the index. Every output
    (names and texts) is then checked with ``assert_clean``.

    Raises:
        CaptureLeakError: Any original gamekey or captured email survived.
    """
    gamekey_map: dict[str, str] = {}
    emails: dict[str, None] = {}
    for record in records:
        for gamekey in _record_gamekeys(record):
            _fake_gamekey(gamekey, gamekey_map)
        emails.update(dict.fromkeys(_record_emails(record)))
    files: dict[str, str] = {}
    index = INDEX_HEADER
    for number, record in enumerate(records, start=1):
        safe_url = sanitize_string(record.url, gamekey_map)
        file_name = None
        if record.has_json:
            file_name = f"{number:02d}-{_slug(urlsplit(safe_url).path)}.json"
            files[file_name] = _dump(sanitize(record.body, gamekey_map))
        index += _index_row(number, record, safe_url, file_name)
    files[INDEX_NAME] = index
    secrets = [*gamekey_map, *emails]
    for name, text in files.items():
        assert_clean(name, secrets)
        assert_clean(text, secrets)
    return files


# --- imperative shell --------------------------------------------------------


def _loggable_path(url: str) -> str:
    path = urlsplit(url).path
    return ORDER_PATH_RE.sub(lambda m: f"{ORDER_PATH_PREFIX}/{redact_gamekey(m.group(1))}", path)


async def _read_json(response: _BodyLike) -> tuple[bool, Any]:
    if JSON_CONTENT_TYPE not in response.headers.get(CONTENT_TYPE_HEADER, "").lower():
        return False, None
    try:
        return True, json.loads(await response.text())
    except (PlaywrightError, ValueError):
        return False, None


def _log_record(record: Record) -> None:
    _log.info(
        "capture_response",
        extra={"path": _loggable_path(record.url), "status": record.status},
    )


class _ResponseCollector:
    """Collects humblebundle.com responses from ``page.on("response")``, capped."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self.responses: list[PageResponse] = []

    def add(self, response: PageResponse) -> None:
        if len(self.responses) < self._limit and is_humble_host(response.url):
            self.responses.append(response)


async def _page_record(response: PageResponse) -> Record:
    has_json, body = await _read_json(response)
    record = Record(response.url, response.request.method, response.status, has_json, body)
    _log_record(record)
    return record


async def _load_library(page: CapturePage, settings: Settings) -> None:
    timeout_ms = settings.http_timeout_s * MS_PER_S
    await page.goto(HUMBLE_BASE + LIBRARY_PATH, timeout=timeout_ms)
    for _ in range(SCROLL_STEPS):
        await page.mouse.wheel(0, SCROLL_DELTA_PX)
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            _log.warning("capture_network_not_idle")


async def _record_library_page(ctx: CaptureContext, settings: Settings) -> list[Record]:
    collector = _ResponseCollector(MAX_PAGE_RESPONSES)
    try:
        page = await ctx.new_page()
        page.on("response", collector.add)
        await _load_library(page, settings)
        return [await _page_record(response) for response in collector.responses]
    except PlaywrightError as exc:
        raise CaptureError("Library page capture failed: browser error") from exc


async def _legacy_get(ctx: CaptureContext, settings: Settings, path: str) -> Record:
    url = HUMBLE_BASE + path
    try:
        response = await ctx.request.get(url, timeout=settings.api_timeout_s * MS_PER_S)
        has_json, body = await _read_json(response)
    except PlaywrightError as exc:
        raise CaptureError("Legacy API call failed: browser request error") from exc
    record = Record(url, HTTP_GET, response.status, has_json, body)
    _log_record(record)
    return record


async def _record_legacy(ctx: CaptureContext, settings: Settings, sleep: Sleep) -> list[Record]:
    records = [await _legacy_get(ctx, settings, USER_ORDER_PATH)]
    first = records[0]
    gamekeys = list_gamekeys(first.body) if first.has_json else []
    interval_s = 1 / settings.api_rps
    for gamekey in gamekeys[:MAX_ORDERS]:
        await sleep(interval_s)
        path = f"{ORDER_PATH_PREFIX}/{quote(gamekey, safe='')}?{ORDER_QUERY}"
        records.append(await _legacy_get(ctx, settings, path))
    return records


def _write_outputs(out_dir: Path, outputs: dict[str, str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in outputs.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


async def run_capture(
    ctx: CaptureContext,
    settings: Settings,
    out_dir: Path,
    *,
    sleep: Sleep = asyncio.sleep,
) -> list[Path]:
    """Record library traffic and write sanitized fixtures plus ``INDEX.md``.

    Args:
        ctx: Logged-in browser context (or structural equivalent).
        settings: Provides timeouts.
        out_dir: Destination directory (created on success only).
        sleep: Async sleep used to rate-limit legacy calls (injectable).

    Returns:
        Paths written: one ``NN-<slug>.json`` per JSON response, then ``INDEX.md``.

    Raises:
        SessionExpiredError: Session is not logged in (propagated unchanged).
        BotChallengeError: Probe hit a bot challenge (propagated unchanged).
        CaptureError: Browser or request failure while recording.
        CaptureLeakError: Sanitization verification failed; nothing written.
    """
    await assert_logged_in(ctx, settings)
    records = [
        *await _record_library_page(ctx, settings),
        *await _record_legacy(ctx, settings, sleep),
    ]
    written = _write_outputs(out_dir, build_outputs(records))
    _log.info("capture_complete", extra={"count": len(records), "out_dir": str(out_dir)})
    return written
