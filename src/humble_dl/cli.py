"""Command-line interface: thin shell wiring settings, session and capture.

Commands load settings, run a small async function with injectable
collaborators, and map expected failures to the documented exit codes.
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Annotated, Protocol

import typer
from playwright.async_api import Cookie

from humble_dl.api.capture import (
    CaptureContext,
    CaptureError,
    CaptureLeakError,
    run_capture,
)
from humble_dl.auth.session import (
    BotChallengeError,
    CookieJar,
    SessionCheckError,
    SessionContext,
    SessionExpiredError,
    interactive_login,
    open_context,
    persist_session_cookies,
)
from humble_dl.config import ConfigError, Settings, load_settings
from humble_dl.lib.logging import get_logger

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SESSION = 2
EXIT_SCHEMA = 3
EXIT_FILES = 4

DEFAULT_CAPTURE_DIR = Path("tests/helpers/fixtures/captured")

MSG_SESSION_EXPIRED = "Session expired or not logged in. Run `humble-dl login`."
MSG_BOT_CHALLENGE = (
    "Humble Bundle served a bot/captcha challenge. "
    "Run `humble-dl login` and complete it in the browser."
)
MSG_LEAK = "Sanitization safety check failed; nothing was written."
MSG_CHECK_FAILED = "The session check failed unexpectedly. See the log output for details."
MSG_CAPTURE_FAILED = "Capture failed. See the log output for details."
MSG_LOGIN_DONE = "Login complete; the session is saved."
MSG_CAPTURE_DONE = "Wrote {count} files to {out_dir}."
MSG_REVIEW = "Review the captured files before committing them."

_log = get_logger(__name__)


class BrowserLike(CaptureContext, CookieJar, Protocol):
    """A context usable by both commands (a real ``BrowserContext`` satisfies it)."""

    async def cookies(self) -> list[Cookie]: ...  # pyright: ignore[reportIncompatibleMethodOverride]


class ContextOpener(Protocol):
    """Opens a browser context on the persistent profile."""

    def __call__(
        self, settings: Settings, *, headed: bool
    ) -> AbstractAsyncContextManager[BrowserLike]: ...


PersistFn = Callable[[CookieJar, Settings], Awaitable[int]]
LoginFn = Callable[[SessionContext, Settings], Awaitable[None]]
CaptureFn = Callable[[CaptureContext, Settings, Path], Awaitable[list[Path]]]

# Module-level defaults; tests replace these so no browser is launched.
OPEN_CONTEXT: ContextOpener = open_context
INTERACTIVE_LOGIN: LoginFn = interactive_login
RUN_CAPTURE: CaptureFn = run_capture
PERSIST_COOKIES: PersistFn = persist_session_cookies

# Most specific first: BotChallengeError subclasses SessionExpiredError.
# A ``None`` message means "use the exception's own message".
_ERROR_MAP: tuple[tuple[type[Exception], int, str | None], ...] = (
    (BotChallengeError, EXIT_SESSION, MSG_BOT_CHALLENGE),
    (SessionExpiredError, EXIT_SESSION, MSG_SESSION_EXPIRED),
    (ConfigError, EXIT_CONFIG, None),
    (CaptureLeakError, EXIT_CONFIG, MSG_LEAK),
    (SessionCheckError, EXIT_CONFIG, MSG_CHECK_FAILED),
    (CaptureError, EXIT_CONFIG, MSG_CAPTURE_FAILED),
)

app = typer.Typer(no_args_is_help=True, add_completion=False)


def map_error(exc: Exception) -> tuple[int, str] | None:
    """Map an expected failure to ``(exit code, stderr message)`` (pure).

    Returns:
        The code and message, or None when ``exc`` is not an expected failure.
    """
    for exc_type, code, message in _ERROR_MAP:
        if isinstance(exc, exc_type):
            return code, message if message is not None else str(exc)
    return None


def _run(command: str, body: Callable[[], None]) -> None:
    """Run ``body`` with start/finish logging and error-to-exit-code mapping."""
    _log.info("command_started", extra={"command": command})
    code = EXIT_OK
    try:
        body()
    except Exception as exc:
        mapped = map_error(exc)
        if mapped is None:
            _log.error("command_finished", extra={"command": command, "exit_code": None})
            raise
        code, message = mapped
        typer.echo(message, err=True)
    _log.info("command_finished", extra={"command": command, "exit_code": code})
    raise typer.Exit(code)


async def _login(
    settings: Settings, opener: ContextOpener, login_fn: LoginFn, persist_fn: PersistFn
) -> None:
    async with opener(settings, headed=True) as ctx:
        await login_fn(ctx, settings)
        await persist_fn(ctx, settings)
    typer.echo(MSG_LOGIN_DONE, err=True)


async def _capture(
    settings: Settings, out: Path, opener: ContextOpener, capture_fn: CaptureFn
) -> None:
    # Headed: Cloudflare blocks server-action POSTs (signed URLs) from headless Chromium.
    async with opener(settings, headed=True) as ctx:
        written = await capture_fn(ctx, settings, out)
    typer.echo(MSG_CAPTURE_DONE.format(count=len(written), out_dir=out), err=True)
    typer.echo(MSG_REVIEW, err=True)


def _login_body() -> None:
    settings = load_settings(os.environ, Path.home())
    asyncio.run(_login(settings, OPEN_CONTEXT, INTERACTIVE_LOGIN, PERSIST_COOKIES))


@app.command()
def login() -> None:
    """Open a browser and sign in to Humble Bundle; the session is saved."""
    _run("login", _login_body)


@app.command()
def capture(
    out: Annotated[Path, typer.Option("--out", help="Directory for sanitized fixtures.")] = (
        DEFAULT_CAPTURE_DIR
    ),
) -> None:
    """Record and sanitize library API traffic as test fixtures."""

    def body() -> None:
        settings = load_settings(os.environ, Path.home())
        asyncio.run(_capture(settings, out, OPEN_CONTEXT, RUN_CAPTURE))

    _run("capture", body)
