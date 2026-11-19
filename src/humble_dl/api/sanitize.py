"""Pure sanitization of captured traffic (security boundary; no I/O).

Captured output is committed as test fixtures, so everything here fails closed:
``assert_clean`` is the backstop that must run over every output name and text.
"""

import re
from collections.abc import Iterable, Iterator
from typing import Any, cast
from urllib.parse import quote, quote_plus, unquote, urlsplit

from humble_dl.api.rsc import (
    KIND_JSON,
    KIND_TEXT,
    FlightRow,
    compact_json,
    parse_flight,
    render_flight,
    try_json,
)
from humble_dl.lib.logging import redact_url

GAMEKEY_KEY = "gamekey"
FAKE_GAMEKEY_FORMAT = "GAMEKEY_{:04d}"
EMAIL_PLACEHOLDER = "user@example.invalid"
REDACTED = "REDACTED"
MIN_IDENTITY_CHARS = 3
MIN_HEURISTIC_KEY_CHARS = 6
NON_KEY_SEGMENTS = frozenset({"claim"})
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

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
GAMEKEY_JSON_RE = re.compile(r'\\?"gamekey\\?"\s*:\s*\\?"([^"\\]+)')
GIFT_PATH_RE = re.compile(r"/gift/([A-Za-z0-9_-]+)")
# Off-site (e.g. signed download) URLs may carry a token in the path, so only the
# host and a filename-like last segment survive.
SITE_HOSTS = frozenset({"humblebundle.com", "www.humblebundle.com"})
FILENAME_SEGMENT_RE = re.compile(r"[\w+-][\w.+-]{0,119}\.[A-Za-z0-9]{1,8}")
KEY_QUERY_RE = re.compile(r"[?&;](?:key|gamekey)=([^&\s\"'<>\\#]+)", re.IGNORECASE)


class CaptureLeakError(Exception):
    """Sanitized output still contains a captured secret; nothing was written."""


def assert_clean(text: str, secrets: Iterable[str]) -> None:
    """Raise if any secret appears in ``text`` (case-insensitive).

    Args:
        text: Output about to be written.
        secrets: Original gamekeys, emails, identity values and cookie values.

    Raises:
        CaptureLeakError: A secret is present. The message never contains it.
    """
    haystack = text.casefold()
    for secret in secrets:
        if secret and secret.casefold() in haystack:
            raise CaptureLeakError("Sanitized output still contains a captured secret")


def fake_gamekey(gamekey: str, gamekey_map: dict[str, str]) -> str:
    """Return the stable fake for ``gamekey``, adding it to ``gamekey_map`` if new."""
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


def redact_offsite_url(url: str) -> str:
    """Strip userinfo, query and fragment; off-site URLs also lose all but a filename.

    Site URLs keep their path. Off-site URLs become ``scheme://host/[REDACTED/]name``
    where ``name`` is kept only if it looks like a file name, else ``REDACTED``.
    """
    redacted = redact_url(url)
    parts = urlsplit(redacted)
    segments = [segment for segment in parts.path.split("/") if segment]
    if (parts.hostname or "").lower() in SITE_HOSTS or not segments:
        return redacted
    last = segments[-1]
    tail = last if FILENAME_SEGMENT_RE.fullmatch(last) else REDACTED
    prefix = f"{REDACTED}/" if len(segments) > 1 else ""
    return f"{parts.scheme}://{parts.netloc}/{prefix}{tail}"


def sanitize_string(text: str, gamekey_map: dict[str, str]) -> str:
    """Replace gamekeys and emails and redact URLs (``redact_offsite_url``) in ``text``."""
    text = _replace_gamekeys(text, gamekey_map)
    text = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return URL_RE.sub(lambda match: redact_offsite_url(match.group(0)), text)


def _sanitize_dict(obj: dict[str, Any], gamekey_map: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        folded = str(key).casefold()
        if folded == GAMEKEY_KEY and isinstance(value, str) and value:
            out[key] = fake_gamekey(value, gamekey_map)
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


def iter_gamekeys(obj: Any) -> Iterator[str]:
    """Yield every string value stored under a ``gamekey`` key, recursively."""
    if isinstance(obj, dict):
        mapping = cast("dict[str, Any]", obj)
        for key, value in mapping.items():
            if str(key).casefold() == GAMEKEY_KEY and isinstance(value, str) and value:
                yield value
            yield from iter_gamekeys(value)
    elif isinstance(obj, list):
        items = cast("list[Any]", obj)
        for item in items:
            yield from iter_gamekeys(item)


def iter_strings(obj: Any) -> Iterator[str]:
    """Yield every string (dict keys included) in a JSON-like value."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        mapping = cast("dict[str, Any]", obj)
        for key, value in mapping.items():
            yield str(key)
            yield from iter_strings(value)
    elif isinstance(obj, list):
        items = cast("list[Any]", obj)
        for item in items:
            yield from iter_strings(item)


def list_gamekeys(body: Any) -> list[str]:
    """Return gamekeys from a ``/user/order`` style body (strings or ``{gamekey}``)."""
    if not isinstance(body, list):
        return []
    items = cast("list[Any]", body)
    keys: list[str] = [item for item in items if isinstance(item, str) and item]
    keys.extend(iter_gamekeys([item for item in items if isinstance(item, dict)]))
    return keys


def text_gamekeys(text: str) -> list[str]:
    """Find gamekeys in free text: JSON ``"gamekey":"..."`` pairs, gift paths, key queries.

    Must run before URL queries are stripped. Path/query matches shorter than
    ``MIN_HEURISTIC_KEY_CHARS`` and non-key segments (``/gift/claim``) are ignored.
    """
    keys = list(GAMEKEY_JSON_RE.findall(text))
    heuristic = [
        *GIFT_PATH_RE.findall(text),
        *(unquote(value) for value in KEY_QUERY_RE.findall(text)),
    ]
    keys.extend(
        key
        for key in heuristic
        if len(key) >= MIN_HEURISTIC_KEY_CHARS and key.casefold() not in NON_KEY_SEGMENTS
    )
    return keys


def flight_gamekeys(text: str) -> list[str]:
    """Return every gamekey found in flight ``text`` (parsed rows plus regex fallback)."""
    keys = text_gamekeys(text)
    for row in parse_flight(text):
        keys.extend(iter_gamekeys(_row_value(row)))
    return keys


def _row_value(row: FlightRow) -> Any:
    """Parsed JSON of a row (``text`` rows are tried as JSON), else None."""
    if row.kind == KIND_JSON:
        return row.payload
    if row.kind == KIND_TEXT:
        return try_json(str(row.payload))[1]
    return None


# --- identity redaction ------------------------------------------------------


def identity_terms(values: Iterable[str]) -> list[str]:
    """Return redaction terms: stripped values of ``MIN_IDENTITY_CHARS``+ plus URL-encoded forms."""
    terms: dict[str, None] = {}
    for raw in values:
        value = raw.strip()
        if len(value) < MIN_IDENTITY_CHARS:
            continue
        for variant in (value, quote(value, safe=""), quote_plus(value)):
            terms[variant] = None
    return list(terms)


def identity_pattern(terms: Iterable[str]) -> re.Pattern[str] | None:
    """Compile a case-insensitive, longest-first alternation of ``terms`` (None if empty)."""
    ordered = sorted(set(terms), key=len, reverse=True)
    if not ordered:
        return None
    return re.compile("|".join(re.escape(term) for term in ordered), re.IGNORECASE)


def redact_text(text: str, pattern: re.Pattern[str] | None) -> str:
    """Replace every identity match in ``text`` with ``REDACTED``."""
    return text if pattern is None else pattern.sub(REDACTED, text)


def redact_value(obj: Any, pattern: re.Pattern[str] | None) -> Any:
    """Return ``obj`` with identity matches redacted in every string and dict key."""
    if pattern is None:
        return obj
    if isinstance(obj, dict):
        mapping = cast("dict[str, Any]", obj)
        return {redact_text(str(k), pattern): redact_value(v, pattern) for k, v in mapping.items()}
    if isinstance(obj, list):
        items = cast("list[Any]", obj)
        return [redact_value(item, pattern) for item in items]
    if isinstance(obj, str):
        return redact_text(obj, pattern)
    return obj


# --- flight sanitization -----------------------------------------------------


def _scrub_row(
    row: FlightRow, gamekey_map: dict[str, str], pattern: re.Pattern[str] | None
) -> FlightRow:
    if row.kind == KIND_JSON:
        value = redact_value(sanitize(row.payload, gamekey_map), pattern)
        return FlightRow(row.row_id, row.kind, value)
    text = str(row.payload)
    if row.kind == KIND_TEXT:
        ok, parsed = try_json(text)
        if ok:
            text = compact_json(sanitize(parsed, gamekey_map))
    clean = redact_text(sanitize_string(text, gamekey_map), pattern)
    return FlightRow(row.row_id, row.kind, clean)


def sanitize_flight(text: str, gamekey_map: dict[str, str], pattern: re.Pattern[str] | None) -> str:
    """Sanitize flight ``text`` row by row and re-serialize it.

    JSON rows (and ``T`` rows holding JSON) go through ``sanitize``; raw rows go
    through ``sanitize_string``. Identity matches are redacted everywhere.
    """
    rows = [_scrub_row(row, gamekey_map, pattern) for row in parse_flight(text)]
    return render_flight(rows)
