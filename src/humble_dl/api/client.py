"""Library client: bundle list, bundle detail and signed URLs via a headed page.

All requests run inside a Playwright page on the persistent profile (Cloudflare
blocks server-action POSTs from outside a real browser). Signed URLs come from
the Next.js server action ``getSignedUrlsForEntitlements``.
"""

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

from playwright.async_api import Page

from humble_dl.api.rsc import extract_flight_from_html, library_detail_path
from humble_dl.auth.session import (
    AUTH_BASE,
    HUMBLE_BASE,
    MS_PER_S,
    BotChallengeError,
    SessionExpiredError,
    is_challenge_body,
    is_login_redirect,
)
from humble_dl.config import Settings
from humble_dl.lib.logging import get_logger, redact_url
from humble_dl.models.library import (
    EBOOK_PLATFORM,
    BundleRef,
    Order,
    SchemaDriftError,
    SignedUrlsError,
    apply_signed_urls,
    ebook_items,
    parse_bundle,
    parse_library,
    parse_signed_urls,
)

LIBRARY_PATH: Final = "/home/library"
SIGNED_URL_BATCH = 25
MAX_ATTEMPTS: Final = 5
BACKOFF_BASE_S: Final = 1.0
BACKOFF_FACTOR: Final = 2
HTTP_OK: Final = 200
HTTP_FORBIDDEN: Final = 403
HTTP_TOO_MANY_REQUESTS: Final = 429
HTTP_SERVER_ERROR_MIN: Final = 500
HTTP_SERVICE_UNAVAILABLE: Final = 503
HTTP_SUCCESS_MAX: Final = 299
NAV_CHALLENGE_STATUSES: Final = (HTTP_FORBIDDEN, HTTP_SERVICE_UNAVAILABLE)
CLOUDFLARE_MARKER: Final = "Attention Required! | Cloudflare"
ACTION_NAME: Final = "getSignedUrlsForEntitlements"
ACTION_ID_RE: Final = re.compile(
    r'createServerReference\)\("([0-9a-f]{40,})",[^)]*?"' + ACTION_NAME + r'"\)'
)
ACTION_NOT_FOUND_MSG: Final = "signed-URL action not found; Humble may have changed its site"
CHUNK_PATH_FRAGMENT: Final = "/_next/static/chunks/"
JS_LIST_CHUNKS: Final = (
    "() => Array.from(document.scripts).map(s => s.src)"
    f".filter(s => s.includes('{CHUNK_PATH_FRAGMENT}'))"
)
JS_FETCH_TEXT: Final = "async url => (await fetch(url)).text()"
JS_CALL_ACTION: Final = """async ([url, id, args]) => {
  const r = await fetch(url, {method: 'POST', body: JSON.stringify(args), headers: {
    'Next-Action': id, 'Content-Type': 'text/plain;charset=UTF-8',
    'Accept': 'text/x-component'}});
  return [r.status, await r.text()];
}"""
KIND_LIBRARY: Final = "library"
KIND_BUNDLE: Final = "bundle"
KIND_SIGNED: Final = "signed-urls"
DEBUG_FILE_MODE: Final = 0o600
MISSING_LOG_LIMIT: Final = 20
DEBUG_TS_FORMAT: Final = "%Y%m%dT%H%M%S%fZ"

_log = get_logger(__name__)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class LibraryApiError(Exception):
    """A library request failed; the message never contains query strings."""


class _NavResponse(Protocol):
    @property
    def status(self) -> int: ...


class LibraryPage(Protocol):
    """The slice of Playwright ``Page`` the library client uses."""

    @property
    def url(self) -> str: ...
    async def goto(self, url: str, *, timeout: float | None = ...) -> _NavResponse | None: ...
    async def content(self) -> str: ...
    async def evaluate(self, expression: str, arg: Any = ...) -> Any: ...


def real_page_is_library_page(page: Page) -> LibraryPage:
    """Static check: pyright proves a real ``Page`` satisfies ``LibraryPage``."""
    return page


class ActionVerdict(Enum):
    """Classification of a server-action response status."""

    OK = "ok"
    RETRY = "retry"
    CHALLENGE = "challenge"
    FAIL = "fail"


def find_action_id(chunk_text: str) -> str | None:
    """Return the signed-URL action id found in a script chunk, if any."""
    match = ACTION_ID_RE.search(chunk_text)
    return match.group(1) if match else None


def batched(ids: Sequence[str], size: int) -> Iterator[list[str]]:
    """Yield consecutive slices of ``ids`` holding at most ``size`` items."""
    for start in range(0, len(ids), size):
        yield list(ids[start : start + size])


def is_blocked_body(body: str) -> bool:
    """Return True for a bot-challenge or Cloudflare block page body."""
    return is_challenge_body(body) or CLOUDFLARE_MARKER in body


def classify_action(status: int, body: str) -> ActionVerdict:
    """Classify a server-action response by status (and body for 403)."""
    if status == HTTP_OK:
        return ActionVerdict.OK
    if status == HTTP_FORBIDDEN and is_blocked_body(body):
        return ActionVerdict.CHALLENGE
    if status == HTTP_TOO_MANY_REQUESTS or status >= HTTP_SERVER_ERROR_MIN:
        return ActionVerdict.RETRY
    return ActionVerdict.FAIL


def backoff_delay(attempt: int) -> float:
    """Delay in seconds after failed ``attempt`` (1-based): 1, 2, 4, 8."""
    return BACKOFF_BASE_S * BACKOFF_FACTOR ** (attempt - 1)


def describe_missing(
    order: Order, signed: Mapping[str, Mapping[str, str]]
) -> list[dict[str, object]]:
    """Explain each ebook file with no signed URL (names only, never URLs).

    Returns:
        One ``{"key", "file", "available"}`` entry per unmatched file, where
        ``available`` lists the filenames Humble returned for that key.
    """
    missing: list[dict[str, object]] = []
    for sub in order.subproducts:
        for download in sub.downloads:
            if download.platform != EBOOK_PLATFORM:
                continue
            returned = signed.get(download.key or "", {})
            for file in download.files:
                if file.url not in returned:
                    missing.append(
                        {"key": download.key, "file": file.url, "available": sorted(returned)}
                    )
    return missing


def is_auth_host(url: str) -> bool:
    """Return True if ``url`` is on the Humble auth host."""
    return urlsplit(url).netloc == urlsplit(AUTH_BASE).netloc


def write_debug_dump(settings: Settings, kind: str, text: str, now: datetime) -> str:
    """Write ``text`` to the debug dir (mode 0600); return the file name."""
    settings.debug_dir.mkdir(parents=True, exist_ok=True)
    name = f"{kind}-{now.strftime(DEBUG_TS_FORMAT)}.txt"
    fd = os.open(settings.debug_dir / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, DEBUG_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return name


class _Throttle:
    """Minimum-interval rate limiter on an injected clock and sleep."""

    def __init__(self, rps: float, clock: Clock, sleep: Sleep) -> None:
        self._interval = 1.0 / rps
        self._clock = clock
        self._sleep = sleep
        self._next = float("-inf")

    async def wait(self) -> None:
        now = self._clock()
        delay = self._next - now
        if delay > 0:
            await self._sleep(delay)
            now += delay
        self._next = now + self._interval


class LibraryClient:
    """Reads the library and signed URLs through a logged-in page."""

    def __init__(
        self,
        page: LibraryPage,
        settings: Settings,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        """Create a client.

        Args:
            page: Page on a logged-in persistent profile.
            settings: Provides ``api_rps``, ``api_timeout_s`` and ``debug_dir``.
            clock: Monotonic clock (injectable for tests).
            sleep: Async sleep (injectable for tests).
        """
        self._page = page
        self._settings = settings
        self._sleep = sleep
        self._throttle = _Throttle(settings.api_rps, clock, sleep)
        self._action_id: str | None = None

    async def list_bundles(self) -> list[BundleRef]:
        """Return the user's bundles.

        Raises:
            SessionExpiredError: Not logged in.
            BotChallengeError: Cloudflare challenged the request.
            LibraryApiError: Navigation failed.
            SchemaDriftError: The page no longer matches the expected shape.
        """
        flight = await self._flight(HUMBLE_BASE + LIBRARY_PATH)
        return self._parse(KIND_LIBRARY, flight, parse_library)

    async def get_order(self, bundle_id: str) -> tuple[Order, list[str]]:
        """Return a bundle's order with signed URLs and the unsigned filenames.

        Args:
            bundle_id: Bundle id from ``list_bundles``.

        Returns:
            ``(order, missing)``; ``missing`` lists filenames with no signed URL.

        Raises:
            SessionExpiredError: Not logged in.
            BotChallengeError: Cloudflare challenged the request.
            LibraryApiError: A request failed or the action was not found.
            SchemaDriftError: A payload no longer matches the expected shape.
        """
        page_url = HUMBLE_BASE + library_detail_path(bundle_id)
        order = await self.get_bundle(bundle_id)
        ids = [s.entitlement_id for s, _ in ebook_items(order) if s.entitlement_id]
        signed: dict[str, dict[str, str]] = {}
        for batch in batched(ids, SIGNED_URL_BATCH):
            signed.update(await self._signed_batch(page_url, batch))
        details = describe_missing(order, signed)
        order, missing = apply_signed_urls(order, signed)
        if missing:
            _log.warning(
                "signed_urls_missing",
                extra={
                    "bundle": bundle_id,
                    "count": len(missing),
                    "files": details[:MISSING_LOG_LIMIT],
                },
            )
        return order, missing

    async def get_bundle(self, bundle_id: str) -> Order:
        """Return a bundle's order as listed on its page, without signed URLs.

        Raises:
            SessionExpiredError: Not logged in.
            BotChallengeError: Cloudflare challenged the request.
            LibraryApiError: Navigation failed.
            SchemaDriftError: The page no longer matches the expected shape.
        """
        flight = await self._flight(HUMBLE_BASE + library_detail_path(bundle_id))
        return self._parse(KIND_BUNDLE, flight, lambda text: parse_bundle(text, bundle_id))

    async def refresh(self, bundle_id: str) -> Order:
        """Re-fetch a bundle so its signed URLs are fresh."""
        return (await self.get_order(bundle_id))[0]

    async def _flight(self, url: str) -> str:
        await self._throttle.wait()
        response = await self._page.goto(url, timeout=self._settings.api_timeout_s * MS_PER_S)
        status = response.status if response is not None else HTTP_OK
        _log.info("api_request", extra={"path": redact_url(url), "status": status, "attempt": 1})
        final = self._page.url
        if is_auth_host(final) or is_login_redirect(final):
            raise SessionExpiredError("Humble session expired; run the login command again.")
        html = await self._page.content()
        if status in NAV_CHALLENGE_STATUSES and is_blocked_body(html):
            raise BotChallengeError("Humble served a bot challenge; retry later or log in again.")
        if not HTTP_OK <= status <= HTTP_SUCCESS_MAX:
            raise LibraryApiError(f"GET {redact_url(url)} failed with status {status}")
        return extract_flight_from_html(html)

    def _parse[T](self, kind: str, text: str, parser: Callable[[str], T]) -> T:
        try:
            return parser(text)
        except SchemaDriftError as exc:
            name = write_debug_dump(self._settings, kind, text, datetime.now(UTC))
            _log.error("schema_drift", extra={"path": exc.path, "file_name": name})
            raise

    async def _signed_batch(self, page_url: str, ids: list[str]) -> dict[str, dict[str, str]]:
        """Signed URLs for ``ids``; app-level ``ok:false`` errors marked
        retryable by Humble are retried with the same backoff as 429/5xx."""
        action_id = await self._ensure_action_id()
        attempt = 1
        while True:
            body = await self._call_action(page_url, action_id, [ids])
            try:
                return self._parse(KIND_SIGNED, body, parse_signed_urls)
            except SignedUrlsError as exc:
                if not exc.retryable or attempt >= MAX_ATTEMPTS:
                    path = redact_url(page_url)
                    raise LibraryApiError(
                        f"signed-URL request for {path} failed: {exc.code}"
                    ) from exc
                delay = backoff_delay(attempt)
                _log.warning(
                    "api_backoff", extra={"delay": delay, "attempt": attempt, "code": exc.code}
                )
                await self._sleep(delay)
                attempt += 1

    async def _call_action(self, page_url: str, action_id: str, args: list[list[str]]) -> str:
        path = redact_url(page_url)
        status = 0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._throttle.wait()
            status, body = await self._page.evaluate(JS_CALL_ACTION, [page_url, action_id, args])
            _log.info("api_request", extra={"path": path, "status": status, "attempt": attempt})
            verdict = classify_action(status, body)
            if verdict is ActionVerdict.OK:
                return str(body)
            if verdict is ActionVerdict.CHALLENGE:
                raise BotChallengeError("Humble served a bot challenge; retry later.")
            if verdict is ActionVerdict.FAIL:
                break
            if attempt < MAX_ATTEMPTS:
                delay = backoff_delay(attempt)
                _log.warning("api_backoff", extra={"delay": delay, "attempt": attempt})
                await self._sleep(delay)
        raise LibraryApiError(f"signed-URL request for {path} failed with status {status}")

    async def _ensure_action_id(self) -> str:
        if self._action_id is None:
            self._action_id = await self._discover_action_id()
        return self._action_id

    async def _discover_action_id(self) -> str:
        sources: list[str] = await self._page.evaluate(JS_LIST_CHUNKS)
        for src in sources:
            found = find_action_id(await self._page.evaluate(JS_FETCH_TEXT, src))
            if found is not None:
                return found
        raise LibraryApiError(ACTION_NOT_FOUND_MSG)
