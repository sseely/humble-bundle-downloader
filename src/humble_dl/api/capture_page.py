"""Browser-page driving for ``capture``: load the library, open a bundle, request links.

Every step here is read-only on the account except the format-button click,
which asks the site for signed download URLs (and fires its ``recordDownload``
call). ``window.open`` is disabled first, so no download tab or file transfer
starts; only the server's signed-URL response is observed.
"""

import asyncio
from collections.abc import Callable
from typing import Any, Literal, Protocol, cast

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from humble_dl.api.rsc import LIBRARY_PATH, is_humble_host, library_detail_path, select_bundle_id
from humble_dl.auth.session import HUMBLE_BASE
from humble_dl.lib.logging import get_logger

SCROLL_STEPS = 3
SCROLL_DELTA_PX = 2000
SCRIPT_SELECTOR = "script"
SCRIPT_TEXT_JS = "els => els.map(e => e.textContent)"
# Format buttons' text reads like "EPUB(2.9 MB)". Plain substring matching: a
# :text-matches() regex selector matched nothing on the live page.
FORMAT_BUTTON_SELECTOR = 'button:has-text(" KB)"), button:has-text(" MB)"), button:has-text(" GB)")'
CLICK_EVENT = "click"
RESPONSE_EVENT = "response"
HTTP_POST = "POST"
DISABLE_WINDOW_OPEN_JS = "() => { window.open = () => null; }"

_log = get_logger(__name__)


class BodyLike(Protocol):
    """Response status, headers and body (Playwright ``Response``/``APIResponse``)."""

    @property
    def status(self) -> int: ...
    @property
    def headers(self) -> dict[str, str]: ...
    async def text(self) -> str: ...


class _RequestLike(Protocol):
    @property
    def method(self) -> str: ...


class PageResponse(BodyLike, Protocol):
    """The slice of Playwright ``Response`` capture uses."""

    @property
    def url(self) -> str: ...
    @property
    def request(self) -> _RequestLike: ...


class _MouseLike(Protocol):
    async def wheel(self, delta_x: float, delta_y: float) -> None: ...


class CapturePage(Protocol):
    """The slice of Playwright ``Page`` capture uses."""

    @property
    def mouse(self) -> _MouseLike: ...
    def on(self, event: Literal["response"], f: Callable[[PageResponse], None]) -> None: ...
    async def goto(self, url: str, *, timeout: float | None = ...) -> object: ...
    async def wait_for_load_state(
        self, state: Literal["networkidle"] | None = ..., *, timeout: float | None = ...
    ) -> None: ...
    async def eval_on_selector_all(self, selector: str, expression: str) -> Any: ...
    async def evaluate(self, expression: str) -> Any: ...
    async def dispatch_event(
        self, selector: str, type: str, *, timeout: float | None = ...
    ) -> None: ...
    async def wait_for_event(
        self,
        event: str,
        predicate: Callable[[Any], bool] | None = ...,
        *,
        timeout: float | None = ...,
    ) -> Any: ...


class ResponseCollector:
    """Collects humblebundle.com responses from ``page.on("response")``, capped.

    ``drain`` hands over what arrived since the last drain, so bodies can be read
    before the page navigates away (Playwright drops them after navigation).
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._seen = 0
        self._pending: list[PageResponse] = []

    def add(self, response: PageResponse) -> None:
        if self._seen < self._limit and is_humble_host(response.url):
            self._seen += 1
            self._pending.append(response)

    def drain(self) -> list[PageResponse]:
        drained, self._pending = self._pending, []
        return drained


async def wait_idle(page: CapturePage, timeout_ms: float) -> None:
    """Wait for network idle; a timeout is logged, not raised."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        _log.warning("capture_network_not_idle")


async def load_library(page: CapturePage, timeout_ms: float) -> None:
    """Open the library list and scroll so lazily loaded rows arrive."""
    await page.goto(HUMBLE_BASE + LIBRARY_PATH, timeout=timeout_ms)
    for _ in range(SCROLL_STEPS):
        await page.mouse.wheel(0, SCROLL_DELTA_PX)
        await wait_idle(page, timeout_ms)


async def first_bundle_id(page: CapturePage) -> str | None:
    """Read the first bundle id from the list page's inline flight scripts."""
    raw = cast("list[Any]", await page.eval_on_selector_all(SCRIPT_SELECTOR, SCRIPT_TEXT_JS))
    bundle_id = select_bundle_id(item for item in raw if isinstance(item, str))
    if bundle_id is None:
        _log.info("capture_no_detail_link")
    return bundle_id


async def open_bundle(page: CapturePage, bundle_id: str, timeout_ms: float) -> bool:
    """Best-effort GET of a bundle's detail page; returns whether it loaded."""
    try:
        await page.goto(HUMBLE_BASE + library_detail_path(bundle_id), timeout=timeout_ms)
    except PlaywrightError:
        _log.warning("capture_detail_navigation_failed")
        return False
    await wait_idle(page, timeout_ms)
    return True


def _is_post(response: PageResponse) -> bool:
    return response.request.method == HTTP_POST


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


async def request_signed_urls(page: CapturePage, timeout_ms: float) -> None:
    """Best-effort: click one format button and wait for the server-action response.

    ``window.open`` is disabled first. The click is dispatched to the element, so
    a consent banner cannot intercept it and no consent choice is made. The wait
    targets the POST itself: ``networkidle`` resolves at once on a settled page.
    """
    waiter = asyncio.ensure_future(
        page.wait_for_event(RESPONSE_EVENT, predicate=_is_post, timeout=timeout_ms)
    )
    try:
        await page.evaluate(DISABLE_WINDOW_OPEN_JS)
        await page.dispatch_event(FORMAT_BUTTON_SELECTOR, CLICK_EVENT, timeout=timeout_ms)
    except PlaywrightError as exc:  # includes "button not found" timeouts
        waiter.cancel()
        _log.warning("capture_format_click_failed", extra={"reason": _first_line(exc)})
        return
    try:
        await waiter
    except PlaywrightError as exc:
        _log.warning("capture_signed_url_response_missing", extra={"reason": _first_line(exc)})
