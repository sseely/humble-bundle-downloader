"""Pure, filesystem-safe naming helpers that define the on-disk layout."""

import re
import unicodedata
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_COMPONENT_BYTES = 255
EMPTY_COMPONENT = "_"
MD5_PREFIX_LENGTH = 8
MAX_SUFFIX_BYTES = 100
FIRST_COUNTER = 1

_SEPARATOR_REPLACEMENT = " - "
_SEPARATOR_PATTERN = re.compile(r"[/:]")
_WHITESPACE_RUN = re.compile(r"\s+")
_CONTROL_CATEGORY = "Cc"
_TRAILING_TRIM_CHARS = ". "
_URL_PATH_SEPARATOR = "/"


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes without splitting a code point."""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")


def _trim(text: str) -> str:
    """Strip surrounding whitespace and trailing dots/spaces."""
    return text.strip().rstrip(_TRAILING_TRIM_CHARS)


def sanitize_component(name: str) -> str:
    """Make ``name`` safe to use as a single path component.

    Applies NFC normalization, replaces ``/`` and ``:`` with `` - ``, collapses
    whitespace, strips control characters, trims leading/trailing whitespace and
    trailing dots, and truncates to ``MAX_COMPONENT_BYTES`` UTF-8 bytes.

    Args:
        name: Arbitrary human-readable name.

    Returns:
        A non-empty, filesystem-safe component (``"_"`` if nothing remains).
    """
    text = unicodedata.normalize("NFC", name)
    text = _SEPARATOR_PATTERN.sub(_SEPARATOR_REPLACEMENT, text)
    text = _WHITESPACE_RUN.sub(" ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != _CONTROL_CATEGORY)
    text = _trim(text)
    text = _trim(_truncate_utf8(text, MAX_COMPONENT_BYTES))
    return text or EMPTY_COMPONENT


def _disambiguate(name: str, machine_name: str, taken: set[str]) -> str:
    """Return ``name`` suffixed with ``-<machine_name>`` (and ``-2``, ``-3``, ...) until unique.

    Uniqueness is judged on ``casefold()`` against ``taken``; the result is always
    within ``MAX_COMPONENT_BYTES`` because the base is shortened to fit the suffix.
    """
    machine_part = _truncate_utf8(f"-{machine_name}", MAX_SUFFIX_BYTES)
    counter = FIRST_COUNTER
    candidate = ""
    while not candidate or candidate.casefold() in taken:
        counter_part = "" if counter == FIRST_COUNTER else f"-{counter}"
        tail = f"{machine_part}{counter_part}"
        base = _truncate_utf8(name, MAX_COMPONENT_BYTES - len(tail.encode("utf-8")))
        candidate = sanitize_component(f"{base}{tail}")
        counter += 1
    return candidate


def item_dir_names(items: list[tuple[str, str]]) -> dict[str, str]:
    """Map each item's machine name to a unique, sanitized directory name.

    Uniqueness is case-insensitive (``casefold()``) because default macOS volumes are.
    The first occurrence of a sanitized name keeps it; later ones become
    ``<name>-<machine_name>``, with ``-2``, ``-3``, ... appended if that also collides.
    Output names keep their original casing.

    Args:
        items: ``(human_name, machine_name)`` pairs in API order.

    Returns:
        Mapping of ``machine_name`` to directory name.
    """
    taken: set[str] = set()
    result: dict[str, str] = {}
    for human_name, machine_name in items:
        name = sanitize_component(human_name)
        if name.casefold() in taken:
            name = _disambiguate(name, machine_name, taken)
        taken.add(name.casefold())
        result[machine_name] = name
    return result


def filename_from_url(url: str) -> str:
    """Return the sanitized, percent-decoded basename of a URL's path.

    Args:
        url: Absolute URL; query and fragment are ignored.

    Returns:
        A filesystem-safe filename.
    """
    path = urlsplit(url).path
    basename = path.rsplit(_URL_PATH_SEPARATOR, 1)[-1]
    return sanitize_component(unquote(basename))


def versioned_name(filename: str, md5: str) -> str:
    """Insert the first 8 characters of ``md5`` before the file suffix.

    Args:
        filename: Original filename, e.g. ``"book.pdf"``.
        md5: Hex digest of the local file being archived.

    Returns:
        ``"book.abcdef12.pdf"`` style name (``"README.abcdef12"`` without suffix).
    """
    path = PurePosixPath(filename)
    return f"{path.stem}.{md5[:MD5_PREFIX_LENGTH]}{path.suffix}"
