"""Record library traffic and write sanitized fixtures.

Sanitization is a security boundary: captured output is committed as test
fixtures. Everything is sanitized in memory (pure functions in ``sanitize`` and
``rsc``), verified with ``assert_clean``, and only then written to disk.
Response headers are never persisted; only JSON bodies, RSC flight rows and the
inline flight data of the library document are written (the HTML never is).
"""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit

from playwright.async_api import Error as PlaywrightError

from humble_dl.api.capture_page import (
    BodyLike,
    CapturePage,
    PageResponse,
    ResponseCollector,
    first_bundle_id,
    load_library,
    open_bundle,
    request_signed_urls,
    server_action_id,
)
from humble_dl.api.rsc import (
    KIND_FLIGHT,
    KIND_JSON,
    KIND_RSC,
    LIBRARY_PATH,
    ResponseKind,
    classify_response,
    extract_flight_from_html,
    is_humble_host,
)
from humble_dl.api.sanitize import (
    EMAIL_PLACEHOLDER,
    EMAIL_RE,
    GIFT_PATH_RE,
    SECRET_KEYS,
    CaptureLeakError,
    assert_clean,
    fake_gamekey,
    flight_gamekeys,
    identity_pattern,
    identity_terms,
    iter_gamekeys,
    iter_strings,
    list_gamekeys,
    redact_text,
    redact_value,
    sanitize,
    sanitize_flight,
    sanitize_string,
    text_gamekeys,
)
from humble_dl.auth.session import (
    HUMBLE_BASE,
    MS_PER_S,
    SessionContext,
    assert_logged_in,
    fetch_identity_secrets,
)
from humble_dl.config import Settings
from humble_dl.lib.logging import get_logger, redact_gamekey, redact_url

__all__ = [
    "EMAIL_PLACEHOLDER",
    "SECRET_KEYS",
    "CaptureContext",
    "CaptureError",
    "CaptureLeakError",
    "Record",
    "assert_clean",
    "build_outputs",
    "is_humble_host",
    "list_gamekeys",
    "run_capture",
    "sanitize",
]

USER_ORDER_PATH = "/api/v1/user/order"
ORDER_PATH_PREFIX = "/api/v1/order"
ORDER_QUERY = "all_tpkds=true"
HTTP_GET = "GET"

MAX_PAGE_RESPONSES = 200
MAX_BODY_CHARS = 20 * 1024 * 1024
MAX_ORDERS = 5
MIN_COOKIE_CHARS = 8

CONTENT_TYPE_HEADER = "content-type"
KIND_INDEX_ONLY = "index-only"
TOO_LARGE_NOTE = "too large"
INDEX_NAME = "INDEX.md"
JSON_INDENT = 2
SLUG_MAX_CHARS = 80
EMPTY_SLUG = "root"
FLIGHT_SLUG = "library-flight"
DETAIL_FLIGHT_SLUG = "library-detail-flight"
INDEX_HEADER = (
    "# Captured library traffic\n\n"
    "| NN | Method | Status | URL | File | Kind |\n"
    "|----|--------|--------|-----|------|------|\n"
)
NO_FILE = "-"


ORDER_PATH_RE = re.compile(rf"^{re.escape(ORDER_PATH_PREFIX)}/([^/]+)")
SLUG_RE = re.compile(r"[^a-z0-9]+")

_log = get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]
IdentityFetcher = Callable[[SessionContext, Settings], Awaitable[list[str]]]


class CaptureError(Exception):
    """Capture could not complete (browser or request failure); nothing was written."""


class CaptureContext(SessionContext, Protocol):
    """The slice of ``BrowserContext`` ``run_capture`` uses (real contexts satisfy it)."""

    async def new_page(self) -> CapturePage: ...  # pyright: ignore[reportIncompatibleMethodOverride]
    async def cookies(self) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True)
class Record:
    """One recorded response.

    ``body`` is parsed JSON when ``has_json``; ``flight`` is raw flight text for
    ``rsc``/``flight`` records (``flight_kind`` says which); ``note`` explains skips.
    """

    url: str
    method: str
    status: int
    has_json: bool
    body: Any
    flight: str | None = None
    flight_kind: str = KIND_RSC
    note: str = ""

    @property
    def kind(self) -> str:
        """The index "kind" column: json, rsc, flight or index-only."""
        if self.has_json:
            return KIND_JSON
        return self.flight_kind if self.flight is not None else KIND_INDEX_ONLY


# --- pure core ---------------------------------------------------------------


def _record_gamekeys(record: Record) -> list[str]:
    path = urlsplit(record.url).path
    keys = text_gamekeys(record.url)
    match = ORDER_PATH_RE.match(path)
    if match:
        keys.append(unquote(match.group(1)))
    if record.has_json:
        if path.rstrip("/") == USER_ORDER_PATH:
            keys.extend(list_gamekeys(record.body))
        keys.extend(iter_gamekeys(record.body))
        for text in iter_strings(record.body):
            keys.extend(text_gamekeys(text))
    if record.flight is not None:
        keys.extend(flight_gamekeys(record.flight))
    return keys


def _record_emails(record: Record) -> list[str]:
    strings = [record.url, *iter_strings(record.body)]
    if record.flight is not None:
        strings.append(record.flight)
    found: list[str] = []
    for text in strings:
        found.extend(EMAIL_RE.findall(text))
        found.extend(EMAIL_RE.findall(unquote(text)))
    return [email for email in found if email.casefold() != EMAIL_PLACEHOLDER]


def _slug(path: str) -> str:
    slug = SLUG_RE.sub("-", path.lower()).strip("-")[:SLUG_MAX_CHARS].strip("-")
    return slug or EMPTY_SLUG


def _index_row(number: int, record: Record, safe_url: str, file_name: str | None) -> str:
    shown = file_name or (f"{NO_FILE} ({record.note})" if record.note else NO_FILE)
    return (
        f"| {number:02d} | {record.method} | {record.status} "
        f"| `{redact_url(safe_url)}` | {shown} | {record.kind} |\n"
    )


def _dump(value: Any) -> str:
    return json.dumps(value, indent=JSON_INDENT, ensure_ascii=False) + "\n"


def _render(
    number: int,
    record: Record,
    safe_url: str,
    gamekey_map: dict[str, str],
    pattern: re.Pattern[str] | None,
) -> tuple[str, str] | None:
    """Return ``(file name, sanitized text)`` for a record, or None if index-only."""
    if record.has_json:
        body = redact_value(sanitize(record.body, gamekey_map), pattern)
        return f"{number:02d}-{_slug(urlsplit(safe_url).path)}.json", _dump(body)
    if record.flight is None:
        return None
    path = urlsplit(safe_url).path
    if record.flight_kind != KIND_FLIGHT:
        slug = _slug(path)
    else:
        slug = FLIGHT_SLUG if path.rstrip("/") == LIBRARY_PATH else DETAIL_FLIGHT_SLUG
    return f"{number:02d}-{slug}.rsc.txt", sanitize_flight(record.flight, gamekey_map, pattern)


def build_outputs(
    records: list[Record],
    identity: Sequence[str] = (),
    extra_secrets: Sequence[str] = (),
) -> dict[str, str]:
    """Sanitize ``records`` into ``{file name: text}``; INDEX.md is last (pure).

    Pass 1 collects every gamekey (bodies, flight rows, order lists, URL paths and
    ``key=`` queries) so pass 2 replaces them everywhere, including file slugs and
    the index. Identity values are redacted everywhere. Every output (names and
    texts) is then checked with ``assert_clean``.

    Args:
        records: Recorded responses.
        identity: Account identity strings to redact (values under 3 chars ignored).
        extra_secrets: Further values that must not appear (e.g. cookie values).

    Raises:
        CaptureLeakError: Any secret survived sanitization.
    """
    terms = identity_terms(identity)
    pattern = identity_pattern(terms)
    gamekey_map: dict[str, str] = {}
    emails: dict[str, None] = {}
    for record in records:
        for gamekey in _record_gamekeys(record):
            fake_gamekey(gamekey, gamekey_map)
        emails.update(dict.fromkeys(_record_emails(record)))
    files: dict[str, str] = {}
    index = INDEX_HEADER
    for number, record in enumerate(records, start=1):
        safe_url = redact_text(sanitize_string(record.url, gamekey_map), pattern)
        rendered = _render(number, record, safe_url, gamekey_map, pattern)
        if rendered is not None:
            files[rendered[0]] = rendered[1]
        index += _index_row(number, record, safe_url, rendered[0] if rendered else None)
    files[INDEX_NAME] = index
    secrets = [*gamekey_map, *emails, *terms, *extra_secrets]
    for name, text in files.items():
        assert_clean(name, secrets)
        assert_clean(text, secrets)
    return files


# --- imperative shell --------------------------------------------------------


def _loggable_path(url: str) -> str:
    path = urlsplit(url).path
    path = ORDER_PATH_RE.sub(lambda m: f"{ORDER_PATH_PREFIX}/{redact_gamekey(m.group(1))}", path)
    return GIFT_PATH_RE.sub(lambda m: f"/gift/{redact_gamekey(m.group(1))}", path)


async def _read_text(response: BodyLike) -> str | None:
    try:
        return await response.text()
    except PlaywrightError:
        return None


def _parse_body(base: Record, kind: ResponseKind, text: str) -> Record:
    if kind == KIND_JSON:
        try:
            return replace(base, has_json=True, body=json.loads(text))
        except (ValueError, RecursionError):
            return base
    flight = text if kind == KIND_RSC else extract_flight_from_html(text)
    return replace(base, flight=flight, flight_kind=kind) if flight else base


async def _response_record(response: BodyLike, url: str, method: str) -> Record:
    base = Record(url, method, response.status, False, None)
    kind = classify_response(url, response.headers.get(CONTENT_TYPE_HEADER, ""))
    text = await _read_text(response) if kind else None
    if kind is None or text is None:
        return base
    if len(text) > MAX_BODY_CHARS:
        return replace(base, note=TOO_LARGE_NOTE)
    return _parse_body(base, kind, text)


def _log_record(record: Record, action: str | None = None) -> None:
    extra: dict[str, object] = {
        "path": _loggable_path(record.url),
        "method": record.method,
        "status": record.status,
        "kind": record.kind,
    }
    if action is not None:
        extra["action"] = action
    _log.info("capture_response", extra=extra)


async def _page_record(response: PageResponse) -> Record:
    record = await _response_record(response, response.url, response.request.method)
    _log_record(record, server_action_id(response))
    return record


async def _drain_records(collector: ResponseCollector) -> list[Record]:
    return [await _page_record(response) for response in collector.drain()]


async def _record_library_page(ctx: CaptureContext, settings: Settings) -> list[Record]:
    """List page, then one bundle's detail page and one signed-URL request.

    Bodies are read after each page settles and before navigating away.
    """
    collector = ResponseCollector(MAX_PAGE_RESPONSES)
    timeout_ms = settings.http_timeout_s * MS_PER_S
    try:
        page = await ctx.new_page()
        page.on("response", collector.add)
        await load_library(page, timeout_ms)
        bundle_id = await first_bundle_id(page)
        records = await _drain_records(collector)
        if bundle_id is not None and await open_bundle(page, bundle_id, timeout_ms):
            await request_signed_urls(page, timeout_ms)
        return records + await _drain_records(collector)
    except PlaywrightError as exc:
        raise CaptureError("Library page capture failed: browser error") from exc


async def _legacy_get(ctx: CaptureContext, settings: Settings, path: str) -> Record:
    url = HUMBLE_BASE + path
    try:
        response = await ctx.request.get(url, timeout=settings.api_timeout_s * MS_PER_S)
        record = await _response_record(response, url, HTTP_GET)
    except PlaywrightError as exc:
        raise CaptureError("Legacy API call failed: browser request error") from exc
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


async def _cookie_secrets(ctx: CaptureContext) -> list[str]:
    """Cookie values worth guarding (never written or logged); raw and decoded forms."""
    try:
        cookies = await ctx.cookies()
    except PlaywrightError as exc:
        raise CaptureError("Could not read browser cookies") from exc
    secrets: dict[str, None] = {}
    for cookie in cookies:
        value = cookie.get("value")
        if isinstance(value, str) and len(value) >= MIN_COOKIE_CHARS:
            secrets[value] = None
            secrets[unquote(value)] = None
    return [value for value in secrets if len(value) >= MIN_COOKIE_CHARS]


def _write_outputs(out_dir: Path, outputs: dict[str, str]) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in outputs.items():
        path = out_dir / name
        path.write_text(text, encoding="utf-8", errors="replace")
        written.append(path)
    return written


async def run_capture(
    ctx: CaptureContext,
    settings: Settings,
    out_dir: Path,
    *,
    sleep: Sleep = asyncio.sleep,
    fetch_identity: IdentityFetcher = fetch_identity_secrets,
) -> list[Path]:
    """Record library traffic and write sanitized fixtures plus ``INDEX.md``.

    Args:
        ctx: Logged-in browser context (or structural equivalent).
        settings: Provides timeouts.
        out_dir: Destination directory (created on success only).
        sleep: Async sleep used to rate-limit legacy calls (injectable).
        fetch_identity: Returns account identity strings to redact (injectable).

    Returns:
        Paths written: one file per JSON/RSC/flight response, then ``INDEX.md``.

    Raises:
        SessionExpiredError: Session is not logged in (propagated unchanged).
        BotChallengeError: Probe hit a bot challenge (propagated unchanged).
        CaptureError: Browser or request failure while recording.
        CaptureLeakError: Sanitization verification failed; nothing written.
    """
    await assert_logged_in(ctx, settings)
    identity = await fetch_identity(ctx, settings)
    cookie_secrets = await _cookie_secrets(ctx)
    records = [
        *await _record_library_page(ctx, settings),
        *await _record_legacy(ctx, settings, sleep),
    ]
    written = _write_outputs(out_dir, build_outputs(records, identity, cookie_secrets))
    _log.info("capture_complete", extra={"count": len(records), "out_dir": str(out_dir)})
    return written
