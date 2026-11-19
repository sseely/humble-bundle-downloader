"""Persistent Playwright session: profile prep, session probe, interactive login."""

import asyncio
import sys
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError

from humble_dl.config import Settings
from humble_dl.lib.logging import get_logger

HUMBLE_BASE = "https://www.humblebundle.com"
AUTH_BASE = "https://auth.humblebundle.com"
# Ory Kratos whoami: 200 = active session, 401 = none, 403 = needs higher assurance.
SESSION_PROBE_URL = AUTH_BASE + "/sessions/whoami"
# Anonymous users are redirected to the Kratos login flow and returned here afterwards.
LOGIN_PATH = "/home/library"
LOGIN_REDIRECT_PATHS = ("/login", "/self-service/login")
IDENTITY_KEY = "identity"
TRAITS_KEY = "traits"
ID_KEY = "id"
PROFILE_DIR_MODE = 0o700
MS_PER_S = 1000
DEFAULT_POLL_S = 2.0
BODY_SNIPPET_CHARS = 4096
LOCATION_HEADER = "location"

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_SERVICE_UNAVAILABLE = 503
HTTP_REDIRECT_MIN = 300
HTTP_REDIRECT_MAX = 399
CHALLENGE_STATUSES = frozenset({HTTP_FORBIDDEN, HTTP_SERVICE_UNAVAILABLE})
CHALLENGE_MARKERS = ("captcha", "cf-chl", "challenge-platform", "just a moment")

LOGIN_INSTRUCTION = (
    "Log in to Humble Bundle in the browser window that just opened; "
    "this command continues automatically once you are signed in."
)

_log = get_logger(__name__)


class SessionExpiredError(Exception):
    """The Humble Bundle session is missing, expired, or login timed out."""


class BotChallengeError(SessionExpiredError):
    """The probe was answered with a bot/captcha challenge instead of data."""


class SessionCheckError(Exception):
    """The session probe failed in an unexpected way (status or Playwright error)."""


class ProbeVerdict(Enum):
    """Classification of a session probe response."""

    OK = "ok"
    EXPIRED = "expired"
    CHALLENGE = "challenge"
    UNEXPECTED = "unexpected"


class _ResponseLike(Protocol):
    @property
    def status(self) -> int: ...
    @property
    def headers(self) -> dict[str, str]: ...
    async def text(self) -> str: ...
    async def json(self) -> Any: ...


class _RequesterLike(Protocol):
    async def get(
        self, url: str, *, timeout: float | None = ..., max_redirects: int | None = ...
    ) -> _ResponseLike: ...


class _PageLike(Protocol):
    async def goto(self, url: str, *, timeout: float | None = ...) -> object: ...


class SessionContext(Protocol):
    """The slice of ``BrowserContext`` this module uses (real contexts satisfy it)."""

    @property
    def request(self) -> _RequesterLike: ...
    async def new_page(self) -> _PageLike: ...


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


def is_challenge_body(body: str) -> bool:
    """Return True if ``body`` contains a typical bot-challenge marker."""
    lowered = body.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def is_login_redirect(location: str | None) -> bool:
    """Return True if ``location`` points at the login page."""
    if not location:
        return False
    return urlsplit(location).path.startswith(LOGIN_REDIRECT_PATHS)


def classify_probe(status: int, location: str | None, body_snippet: str) -> ProbeVerdict:
    """Classify a session probe response (pure).

    Args:
        status: HTTP status code.
        location: Value of the ``Location`` header, if any.
        body_snippet: Leading part of the body (only consulted for 403/503).

    Returns:
        The verdict for the probe.
    """
    if status == HTTP_OK:
        return ProbeVerdict.OK
    if status in CHALLENGE_STATUSES and is_challenge_body(body_snippet):
        return ProbeVerdict.CHALLENGE
    if status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
        return ProbeVerdict.EXPIRED
    if HTTP_REDIRECT_MIN <= status <= HTTP_REDIRECT_MAX and is_login_redirect(location):
        return ProbeVerdict.EXPIRED
    return ProbeVerdict.UNEXPECTED


def prepare_profile_dir(path: Path) -> None:
    """Create ``path`` (and parents) and force mode 0700, even if it existed looser."""
    path.mkdir(mode=PROFILE_DIR_MODE, parents=True, exist_ok=True)
    path.chmod(PROFILE_DIR_MODE)


@asynccontextmanager
async def open_context(settings: Settings, *, headed: bool) -> AsyncGenerator[BrowserContext]:
    """Launch Chromium on the persistent profile and yield its context.

    Args:
        settings: Provides ``profile_dir``.
        headed: Show the browser window when True.

    Yields:
        The persistent browser context; closed on exit.
    """
    prepare_profile_dir(settings.profile_dir)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(settings.profile_dir), headless=not headed
        )
        try:
            yield context
        finally:
            await context.close()


async def _probe(ctx: SessionContext, settings: Settings) -> ProbeVerdict:
    """Issue the probe request and classify it; wraps Playwright errors."""
    try:
        response = await ctx.request.get(
            SESSION_PROBE_URL,
            timeout=settings.api_timeout_s * MS_PER_S,
            max_redirects=0,
        )
        status = response.status
        _log.info("session_check", extra={"status": status})
        body = ""  # whoami 200 carries identity traits: only challenge bodies are read
        if status in CHALLENGE_STATUSES:
            body = (await response.text())[:BODY_SNIPPET_CHARS]
        return classify_probe(status, response.headers.get(LOCATION_HEADER), body)
    except PlaywrightError as exc:
        raise SessionCheckError("Session probe failed: browser request error") from exc


async def assert_logged_in(ctx: SessionContext, settings: Settings) -> None:
    """Verify the session by probing the API.

    Args:
        ctx: Browser context (or structural equivalent).
        settings: Provides ``api_timeout_s``.

    Raises:
        BotChallengeError: The probe was met with a bot challenge.
        SessionExpiredError: The session is not logged in.
        SessionCheckError: Unexpected status or Playwright failure.
    """
    verdict = await _probe(ctx, settings)
    if verdict is ProbeVerdict.OK:
        return
    if verdict is ProbeVerdict.CHALLENGE:
        raise BotChallengeError("Humble Bundle returned a bot challenge; retry later")
    if verdict is ProbeVerdict.EXPIRED:
        raise SessionExpiredError("Session expired; run `humble-dl login`")
    raise SessionCheckError("Session probe returned an unexpected response")


def _collect_strings(value: object) -> list[str]:
    """Return every non-empty string found recursively in ``value`` (pure)."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, dict):
        found: list[str] = []
        for item in cast("dict[str, object]", value).values():
            found.extend(_collect_strings(item))
        return found
    if isinstance(value, list):
        return [text for item in cast("list[object]", value) for text in _collect_strings(item)]
    return []


def extract_identity_secrets(body: object) -> list[str]:
    """Pull identity traits, identity id and session id out of a whoami body (pure).

    Returns:
        Deduplicated, order-stable non-empty strings.
    """
    if not isinstance(body, dict):
        return []
    top = cast("dict[str, object]", body)
    identity = top.get(IDENTITY_KEY)
    secrets: list[str] = []
    if isinstance(identity, dict):
        ident = cast("dict[str, object]", identity)
        secrets.extend(_collect_strings(ident.get(TRAITS_KEY)))
        secrets.extend(_collect_strings(ident.get(ID_KEY)))
    secrets.extend(_collect_strings(top.get(ID_KEY)))
    return list(dict.fromkeys(secrets))


async def fetch_identity_secrets(ctx: SessionContext, settings: Settings) -> list[str]:
    """Return the account holder's identifying strings for fixture redaction.

    Values are never logged.

    Args:
        ctx: Browser context (or structural equivalent).
        settings: Provides ``api_timeout_s``.

    Returns:
        Traits strings, identity id and session id; empty unless whoami is 200.

    Raises:
        SessionCheckError: Playwright failure while requesting or parsing.
    """
    try:
        response = await ctx.request.get(
            SESSION_PROBE_URL, timeout=settings.api_timeout_s * MS_PER_S, max_redirects=0
        )
        if response.status != HTTP_OK:
            return []
        return extract_identity_secrets(await response.json())
    except PlaywrightError as exc:
        raise SessionCheckError("Identity lookup failed: browser request error") from exc


async def _open_login_page(ctx: SessionContext, settings: Settings) -> None:
    try:
        page = await ctx.new_page()
        await page.goto(HUMBLE_BASE + LOGIN_PATH, timeout=settings.http_timeout_s * MS_PER_S)
    except PlaywrightError as exc:
        raise SessionCheckError("Could not open the login page") from exc


async def interactive_login(
    ctx: SessionContext,
    settings: Settings,
    *,
    poll_s: float = DEFAULT_POLL_S,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.monotonic,
) -> None:
    """Open the login page and poll until the human has signed in.

    Args:
        ctx: Browser context (headed).
        settings: Provides ``login_timeout_s`` and probe timeouts.
        poll_s: Seconds between probes.
        sleep: Async sleep (injectable).
        clock: Monotonic clock (injectable).

    Raises:
        SessionExpiredError: Login did not complete within ``login_timeout_s``.
        BotChallengeError: The probe was met with a bot challenge.
        SessionCheckError: The login page could not be opened. Probe failures
            while polling are logged and retried until the timeout.
    """
    deadline = clock() + settings.login_timeout_s
    await _open_login_page(ctx, settings)
    print(LOGIN_INSTRUCTION, file=sys.stderr)
    _log.info("login_started")
    while True:
        try:
            await assert_logged_in(ctx, settings)
        except BotChallengeError:
            raise
        except SessionExpiredError:
            pass
        except SessionCheckError:
            _log.warning("session_check_unexpected")
        else:
            _log.info("login_succeeded")
            return
        if clock() >= deadline:
            _log.warning("login_timed_out")
            raise SessionExpiredError("Login timed out; run `humble-dl login` again")
        await sleep(poll_s)
