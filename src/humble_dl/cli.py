"""Command-line interface: thin shell wiring settings, session and capture.

Commands load settings, run a small async function with injectable
collaborators, and map expected failures to the documented exit codes.
"""

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Annotated, Protocol, cast

import httpx
import typer
from playwright.async_api import Cookie
from playwright.async_api import Error as PlaywrightError

from humble_dl.api.capture import (
    CaptureContext,
    CaptureError,
    CaptureLeakError,
    run_capture,
)
from humble_dl.api.client import LibraryApiError, LibraryClient, LibraryPage
from humble_dl.auth.session import (
    BotChallengeError,
    CookieJar,
    SessionCheckError,
    SessionContext,
    SessionExpiredError,
    assert_logged_in,
    interactive_login,
    open_context,
    persist_session_cookies,
)
from humble_dl.config import ConfigError, Settings, load_settings
from humble_dl.lib.hashcache import HashCache
from humble_dl.lib.logging import get_logger
from humble_dl.models.library import SchemaDriftError
from humble_dl.services.books import (
    DEFAULT_PAUSE_S,
    BooksDeps,
    BooksError,
    BooksFailedError,
    BooksOptions,
    BooksReport,
    import_books,
)
from humble_dl.services.books_library import library_duplicates_report
from humble_dl.services.planner import ArchiveThenFetch
from humble_dl.services.sync import (
    InsufficientSpaceError,
    LibraryLike,
    Summary,
    SyncDeps,
    SyncOptions,
    fetch_of,
    run_sync,
)
from humble_dl.services.titles import BundleSource, fetch_titles, save_titles

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
MSG_BROWSER_ERROR = "Browser error. Close other browser windows and retry."
MSG_SCHEMA_DRIFT = (
    "Humble Bundle's data no longer matches the expected format; "
    "the raw response was saved to the debug directory."
)
MSG_SYNC_SUMMARY = (
    "planned {planned}, skipped {skipped}, downloaded {downloaded}, failed {failed}, "
    "archived {archived}, {bytes} bytes in {duration_s:.1f}s"
)
MSG_DRY_LINE = "{verb} {relative} ({size})"
MSG_DRY_TOTAL = "Dry run: {count} files to fetch, {bytes} known bytes."
MSG_FAILED_FILES = "{count} file(s) failed:"
MSG_FAILED_BUNDLES = "{count} bundle(s) could not be loaded:"
MSG_UNSIGNED_FILES = "{count} file(s) had no download link:"
MSG_BOOKS_SUMMARY = (
    "selected {selected}, already imported {already}, imported {imported}, "
    "changed {changed}, failed {failed}, duplicate in another bundle {duplicates}, "
    "already in Books {in_books}"
)
MSG_BOOKS_FAILED = "{count} file(s) failed to import into Books:"
MSG_TITLES_SAVED = "Saved {count} title(s) to {path}."
UNKNOWN_SIZE = "size unknown"
VERB_FETCH = "fetch"
VERB_REPLACE = "archive+fetch"

_log = get_logger(__name__)


class BrowserLike(CaptureContext, CookieJar, Protocol):
    """A context usable by both commands (a real ``BrowserContext`` satisfies it)."""

    async def cookies(self) -> list[Cookie]: ...  # pyright: ignore[reportIncompatibleMethodOverride]


class ContextOpener(Protocol):
    """Opens a browser context on the persistent profile."""

    def __call__(
        self, settings: Settings, *, headed: bool
    ) -> AbstractAsyncContextManager[BrowserLike]: ...


class SyncFailedError(Exception):
    """One or more files failed to download; the message lists them."""


PersistFn = Callable[[CookieJar, Settings], Awaitable[int]]
LoginFn = Callable[[SessionContext, Settings], Awaitable[None]]
CaptureFn = Callable[[CaptureContext, Settings, Path], Awaitable[list[Path]]]
SyncFn = Callable[[Settings, SyncOptions, SyncDeps], Awaitable[Summary]]
AssertLoggedInFn = Callable[[SessionContext, Settings], Awaitable[None]]
LibraryFactory = Callable[[BrowserLike, Settings], Awaitable[LibraryLike]]
BundleSourceFactory = Callable[[BrowserLike, Settings], Awaitable[BundleSource]]
FetchTitlesFn = Callable[[BundleSource, str | None], Awaitable[tuple[dict[str, str], list[str]]]]


async def _make_library(ctx: BrowserLike, settings: Settings) -> LibraryClient:
    page = await ctx.new_page()
    return LibraryClient(cast("LibraryPage", page), settings)


# Module-level defaults; tests replace these so no browser is launched.
OPEN_CONTEXT: ContextOpener = open_context
INTERACTIVE_LOGIN: LoginFn = interactive_login
RUN_CAPTURE: CaptureFn = run_capture
PERSIST_COOKIES: PersistFn = persist_session_cookies
RUN_SYNC: SyncFn = run_sync
ASSERT_LOGGED_IN: AssertLoggedInFn = assert_logged_in
MAKE_LIBRARY: LibraryFactory = _make_library
MAKE_BUNDLES: BundleSourceFactory = _make_library
FETCH_TITLES: FetchTitlesFn = fetch_titles
BOOKS_DEPS: Callable[[], BooksDeps] = BooksDeps

# Most specific first: BotChallengeError subclasses SessionExpiredError.
# A ``None`` message means "use the exception's own message".
_ERROR_MAP: tuple[tuple[type[Exception], int, str | None], ...] = (
    (BotChallengeError, EXIT_SESSION, MSG_BOT_CHALLENGE),
    (SessionExpiredError, EXIT_SESSION, MSG_SESSION_EXPIRED),
    (SchemaDriftError, EXIT_SCHEMA, MSG_SCHEMA_DRIFT),
    (SyncFailedError, EXIT_FILES, None),
    (BooksFailedError, EXIT_FILES, None),
    (BooksError, EXIT_CONFIG, None),
    (InsufficientSpaceError, EXIT_CONFIG, None),
    (LibraryApiError, EXIT_CONFIG, None),
    (PlaywrightError, EXIT_CONFIG, MSG_BROWSER_ERROR),
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


def _dry_run_lines(summary: Summary, output_dir: Path) -> list[str]:
    lines: list[str] = []
    for action in summary.actions:
        fetch = fetch_of(action)
        if fetch is None:
            continue
        verb = VERB_REPLACE if isinstance(action, ArchiveThenFetch) else VERB_FETCH
        size = UNKNOWN_SIZE if fetch.size is None else f"{fetch.size} bytes"
        relative = fetch.path.relative_to(output_dir).as_posix()
        lines.append(MSG_DRY_LINE.format(verb=verb, relative=relative, size=size))
    return lines


def _dry_run_total(summary: Summary) -> str:
    fetches = [f for f in map(fetch_of, summary.actions) if f is not None]
    return MSG_DRY_TOTAL.format(count=len(fetches), bytes=sum(f.size or 0 for f in fetches))


def _report_sync(summary: Summary, settings: Settings, opts: SyncOptions) -> None:
    if opts.dry_run:
        for line in _dry_run_lines(summary, settings.output_dir):
            typer.echo(line)
        typer.echo(_dry_run_total(summary))
    typer.echo(MSG_SYNC_SUMMARY.format(**vars(summary)))
    problems = _failure_sections(summary)
    if problems:
        raise SyncFailedError("\n".join(problems))


def _failure_sections(summary: Summary) -> list[str]:
    sections: list[str] = []
    if summary.failures:
        listing = "\n".join(f"  {path}" for path in summary.failures)
        sections.append(f"{MSG_FAILED_FILES.format(count=summary.failed)}\n{listing}")
    if summary.failed_bundles:
        listing = "\n".join(f"  {title}" for title in summary.failed_bundles)
        count = len(summary.failed_bundles)
        sections.append(f"{MSG_FAILED_BUNDLES.format(count=count)}\n{listing}")
    if summary.unsigned_files:
        listing = "\n".join(f"  {name}" for name in summary.unsigned_files)
        count = len(summary.unsigned_files)
        sections.append(f"{MSG_UNSIGNED_FILES.format(count=count)}\n{listing}")
    return sections


async def _sync(settings: Settings, opts: SyncOptions) -> Summary:
    # Headed: Cloudflare blocks server-action POSTs (signed URLs) from headless Chromium.
    async with OPEN_CONTEXT(settings, headed=True) as ctx:
        await ASSERT_LOGGED_IN(ctx, settings)
        library = await MAKE_LIBRARY(ctx, settings)
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_s, follow_redirects=True
        ) as client:
            deps = SyncDeps(
                library=library,
                http=client,
                cache=HashCache.load(settings.hash_cache_path),
                free_bytes=lambda path: shutil.disk_usage(path).free,
            )
            return await RUN_SYNC(settings, opts, deps)


@app.command()
def sync(
    output: Annotated[
        Path | None, typer.Option("--output", help="Output directory (default ~/HumbleBundle).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be downloaded; write nothing.")
    ] = False,
    only: Annotated[
        str | None, typer.Option("--only", help="Only bundles whose title contains TEXT.")
    ] = None,
) -> None:
    """Download every ebook file from the library."""

    def body() -> None:
        settings = load_settings(os.environ, Path.home(), output_override=output)
        opts = SyncOptions(dry_run=dry_run, only=only)
        summary = asyncio.run(_sync(settings, opts))
        _report_sync(summary, settings, opts)

    _run("sync", body)


async def _titles(settings: Settings, only: str | None) -> tuple[dict[str, str], list[str]]:
    # Headed, like sync: Cloudflare is less likely to challenge a visible browser.
    async with OPEN_CONTEXT(settings, headed=True) as ctx:
        await ASSERT_LOGGED_IN(ctx, settings)
        return await FETCH_TITLES(await MAKE_BUNDLES(ctx, settings), only)


@app.command()
def titles(
    only: Annotated[
        str | None, typer.Option("--only", help="Only bundles whose title contains TEXT.")
    ] = None,
) -> None:
    """Save Humble's exact item titles (for relabelling books in Apple Books)."""

    def body() -> None:
        settings = load_settings(os.environ, Path.home())
        found, failed = asyncio.run(_titles(settings, only))
        save_titles(settings.titles_path, found)
        typer.echo(MSG_TITLES_SAVED.format(count=len(found), path=settings.titles_path))
        if failed:
            listing = "\n".join(f"  {title}" for title in failed)
            raise SyncFailedError(f"{MSG_FAILED_BUNDLES.format(count=len(failed))}\n{listing}")

    _run("titles", body)


def _report_books(report: BooksReport, dry_run: bool) -> None:
    for line in report.lines:
        typer.echo(line)
    if not dry_run:
        typer.echo(
            MSG_BOOKS_SUMMARY.format(
                selected=report.selected,
                already=report.already,
                imported=report.imported,
                changed=report.changed,
                failed=len(report.failed),
                duplicates=report.duplicates,
                in_books=report.in_books,
            )
        )
    if report.failed:
        listing = "\n".join(f"  {path}" for path in report.failed)
        raise BooksFailedError(f"{MSG_BOOKS_FAILED.format(count=len(report.failed))}\n{listing}")


def _duplicates_body() -> None:
    for line in library_duplicates_report(BOOKS_DEPS().library_dir):
        typer.echo(line)


@app.command("books-duplicates")
def books_duplicates() -> None:
    """Report books held more than once in Apple Books (read-only)."""
    _run("books-duplicates", _duplicates_body)


@app.command()
def books(
    output: Annotated[
        Path | None, typer.Option("--output", help="Output directory (default ~/HumbleBundle).")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be imported; change nothing.")
    ] = False,
    only: Annotated[
        str | None, typer.Option("--only", help="Only bundles whose folder name contains TEXT.")
    ] = None,
    pause: Annotated[
        float, typer.Option("--pause", help="Seconds between files (0-60).")
    ] = DEFAULT_PAUSE_S,
) -> None:
    """Import one file per downloaded ebook into Apple Books (macOS)."""

    def body() -> None:
        settings = load_settings(os.environ, Path.home(), output_override=output)
        opts = BooksOptions(dry_run=dry_run, only=only, pause=pause)
        _report_books(import_books(settings, opts, BOOKS_DEPS()), dry_run)

    _run("books", body)
