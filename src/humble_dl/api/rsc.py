"""Pure helpers for React Server Components "flight" data (Next.js App Router).

Flight data arrives inline in the HTML document as
``self.__next_f.push([1,"<JS string literal>"])`` chunks, or as the body of a
``text/x-component`` response. Rows look like ``<hex id>:<payload>``; ``T`` rows
(``<id>:T<hexlen>,<text>``) carry ``hexlen`` UTF-8 bytes of raw text. Nothing
here raises on malformed input: unparseable material is kept as raw rows.
"""

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import urlsplit

PUSH_PREFIX: Final = "self.__next_f.push("
CHUNK_TYPE_TEXT: Final = 1
CHUNK_MIN_ITEMS: Final = 2
KIND_JSON: Final = "json"
KIND_RAW: Final = "raw"
KIND_TEXT: Final = "text"
KIND_RSC: Final = "rsc"
KIND_FLIGHT: Final = "flight"
HUMBLE_DOMAIN: Final = "humblebundle.com"
LIBRARY_PATH: Final = "/home/library"
JSON_CONTENT_TYPE: Final = "json"
RSC_CONTENT_TYPE: Final = "text/x-component"
HTML_CONTENT_TYPE: Final = "text/html"
DETAIL_MARKERS: Final = ("/downloads", "key=", "/gift/")
DETAIL_BLOCKED: Final = ("checkout", "purchase", "buy", "redeem", "claim", "cart")
HEX_BASE: Final = 16
COMPACT_SEPARATORS: Final = (",", ":")
ROW_START_RE = re.compile(r"[0-9a-fA-F]+:")
TEXT_HEADER_RE = re.compile(r"T([0-9a-fA-F]+),")
WHITESPACE_RE = re.compile(r"\s*")

RowKind = Literal["json", "raw", "text"]
ResponseKind = Literal["json", "rsc", "flight"]


@dataclass(frozen=True)
class FlightRow:
    """One flight row; ``payload`` is parsed JSON for ``json`` rows, else text."""

    row_id: str
    kind: RowKind
    payload: Any


def _chunk_text(value: Any) -> str | None:
    """Return the string of a ``[1, "<text>"]`` push argument, else None."""
    if not isinstance(value, list) or len(value) < CHUNK_MIN_ITEMS:  # pyright: ignore[reportUnknownArgumentType]
        return None
    kind, text = value[0], value[1]  # pyright: ignore[reportUnknownVariableType]
    if kind == CHUNK_TYPE_TEXT and isinstance(text, str):
        return text
    return None


def extract_flight_from_html(html: str) -> str:
    """Concatenate the decoded ``self.__next_f.push([1,"..."])`` chunks of ``html``.

    Pushes of other types (``[0]``, ``[2, ...]``) and undecodable pushes are skipped.

    Args:
        html: Full HTML document text.

    Returns:
        The flight text, or an empty string when there are no type-1 chunks.
    """
    decoder = json.JSONDecoder()
    chunks: list[str] = []
    pos = 0
    while (index := html.find(PUSH_PREFIX, pos)) != -1:
        start = index + len(PUSH_PREFIX)
        pos = start
        start = WHITESPACE_RE.match(html, start).end()  # pyright: ignore[reportOptionalMemberAccess]
        try:
            value, end = decoder.raw_decode(html, start)
        except (ValueError, RecursionError):
            continue
        pos = end
        text = _chunk_text(value)
        if text is not None:
            chunks.append(text)
    return "".join(chunks)


def try_json(text: str) -> tuple[bool, Any]:
    """Parse ``text`` as JSON; return ``(False, None)`` instead of raising."""
    try:
        return True, json.loads(text)
    except (ValueError, RecursionError):
        return False, None


def _take_bytes(text: str, start: int, size: int) -> str:
    """Return the characters of ``text[start:]`` spanning ``size`` UTF-8 bytes."""
    used = 0
    end = start
    while end < len(text) and used < size:
        used += len(text[end].encode("utf-8", errors="replace"))
        end += 1
    return text[start:end]


def _line_end(text: str, pos: int) -> int:
    end = text.find("\n", pos)
    return len(text) if end == -1 else end


def _next_row(text: str, pos: int) -> tuple[FlightRow, int]:
    """Parse the row starting at ``pos``; return it and the next position."""
    end = _line_end(text, pos)
    start = ROW_START_RE.match(text, pos)
    if start is None or start.end() > end:
        return FlightRow("", KIND_RAW, text[pos:end]), end + 1
    row_id = text[pos : start.end() - 1]
    header = TEXT_HEADER_RE.match(text, start.end())
    if header is not None:
        body = _take_bytes(text, header.end(), int(header.group(1), HEX_BASE))
        return FlightRow(row_id, KIND_TEXT, body), header.end() + len(body)
    payload = text[start.end() : end]
    ok, value = try_json(payload)
    if ok:
        return FlightRow(row_id, KIND_JSON, value), end + 1
    return FlightRow(row_id, KIND_RAW, payload), end + 1


def parse_flight(text: str) -> list[FlightRow]:
    """Split flight ``text`` into rows (never raises; malformed lines stay raw).

    Args:
        text: Concatenated flight data.

    Returns:
        Rows in order. JSON payloads are parsed, ``T`` rows keep their raw text.
    """
    rows: list[FlightRow] = []
    pos = 0
    while pos < len(text):
        if text[pos] == "\n":
            pos += 1
            continue
        row, pos = _next_row(text, pos)
        rows.append(row)
    return rows


def compact_json(value: Any) -> str:
    """Serialize ``value`` as compact JSON, keeping non-ASCII characters."""
    return json.dumps(value, ensure_ascii=False, separators=COMPACT_SEPARATORS)


def _render_row(row: FlightRow) -> str:
    prefix = f"{row.row_id}:" if row.row_id else ""
    if row.kind == KIND_JSON:
        return prefix + compact_json(row.payload)
    if row.kind == KIND_TEXT:
        size = len(str(row.payload).encode("utf-8", errors="replace"))
        return f"{prefix}T{size:x},{row.payload}"
    return prefix + str(row.payload)


def render_flight(rows: Iterable[FlightRow]) -> str:
    """Serialize rows as ``<id>:<payload>`` lines; ``T`` lengths are recomputed."""
    return "".join(f"{_render_row(row)}\n" for row in rows)


# --- response classification and detail-link choice (pure) -------------------


def is_humble_host(url: str) -> bool:
    """Return True if ``url`` is on humblebundle.com or a subdomain."""
    host = (urlsplit(url).hostname or "").lower()
    return host == HUMBLE_DOMAIN or host.endswith(f".{HUMBLE_DOMAIN}")


def classify_response(url: str, content_type: str) -> ResponseKind | None:
    """Decide how a response is recorded from its URL path and content type."""
    lowered = content_type.lower()
    if JSON_CONTENT_TYPE in lowered:
        return KIND_JSON
    if RSC_CONTENT_TYPE in lowered:
        return KIND_RSC
    if HTML_CONTENT_TYPE in lowered and urlsplit(url).path.rstrip("/") == LIBRARY_PATH:
        return KIND_FLIGHT
    return None


def _is_site_href(href: str) -> bool:
    if href.startswith("/"):
        return not href.startswith("//")
    return urlsplit(href).scheme in ("http", "https") and is_humble_host(href)


def select_detail_href(hrefs: Iterable[str]) -> str | None:
    """Pick the first library-item link, never one that could buy/gift/redeem/claim.

    Only same-site hrefs containing ``/downloads``, ``key=`` or ``/gift/`` qualify;
    any href containing a blocked word (checkout, purchase, buy, redeem, claim,
    cart) is skipped.
    """
    for href in hrefs:
        lowered = href.lower()
        if not _is_site_href(href) or any(word in lowered for word in DETAIL_BLOCKED):
            continue
        if any(marker in lowered for marker in DETAIL_MARKERS):
            return href
    return None
