"""Apple Books library scanning: book keys, duplicate detection, library report.

Everything here reads entry *names* only; nothing in the Books library is ever
opened or written. Key derivation and grouping are pure.
"""

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path, PurePath

from humble_dl.lib.epub import read_epub_title
from humble_dl.lib.logging import get_logger

PREFERRED_SUFFIXES = (".epub", ".pdf")
EPUB_SUFFIX = ".epub"
HIDDEN_PREFIX = "."
LIBRARY_RELATIVE = ("Library", "Mobile Documents", "iCloud~com~apple~iBooks", "Documents")
MSG_DUP_LINE = " | "
MSG_DUP_TOTAL = "{count} book(s) with more than one copy in Books."

_COLLISION_SUFFIX = re.compile(r"_\d{9,}$")
_KEPUB_SUFFIX = re.compile(r"\.kepub$", re.IGNORECASE)
# Store/format/quality tags that do not change which book a file is (the user
# treats every edition as the same book): "_EPUB", " - for Kindle", "-high"...
_VARIANT_TAGS = re.compile(
    r"(?:[ _-]+(?:for[ _-]?kindle|for[ _-]?kobo|kepub|epub|mobi|hq|lq|high|low))+$",
    re.IGNORECASE,
)
_SUBTITLE_SEPARATOR = " - "
_DIGIT = re.compile(r"\d")
_GLUED_QUALITY = re.compile(r"(?<=\d)(?:hq|lq)$", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9]")
_COPY_NUMBER = re.compile(r"^(.*\S) (\d+)$")

_log = get_logger(__name__)


def norm(text: str) -> str:
    """Lowercase ``text`` and drop every character outside ``[a-z0-9]``."""
    return _NON_ALNUM.sub("", text.lower())


def default_library_dir() -> Path:
    """Apple Books' iCloud library folder, resolved from the current home directory."""
    return Path.home().joinpath(*LIBRARY_RELATIVE)


def is_book_entry(name: str) -> bool:
    """True for a visible ``.epub``/``.pdf`` library entry name."""
    return (
        not name.startswith(HIDDEN_PREFIX) and PurePath(name).suffix.lower() in PREFERRED_SUFFIXES
    )


def read_library_names(library_dir: Path) -> list[str]:
    """List top-level entry names of the Books library (names only, never contents).

    A missing directory yields an empty list and a ``books_library_missing`` warning.
    """
    try:
        return sorted(entry.name for entry in library_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        _log.warning("books_library_missing", extra={"path": str(library_dir)})
        return []


def filename_key(name: str) -> str:
    """Book key of a file name, applied identically to downloads and library entries.

    Drops the extension (and ``.kepub``), a ``_<9+ digits>`` collision suffix,
    trailing variant tags (``_EPUB``, ``for Kindle``, ``-high``, ``HQ``...), then
    normalizes.
    """
    stem = _KEPUB_SUFFIX.sub("", PurePath(name).stem)
    stem = _COLLISION_SUFFIX.sub("", stem)
    stem = _VARIANT_TAGS.sub("", stem)
    return norm(_GLUED_QUALITY.sub("", stem))


def entry_key(name: str) -> str:
    """Key of a library entry (see ``filename_key``)."""
    return filename_key(name)


def library_keys(names: Iterable[str]) -> frozenset[str]:
    """Non-empty keys of every visible ``.epub``/``.pdf`` library entry."""
    return frozenset(k for n in names if is_book_entry(n) and (k := entry_key(n)))


def book_keys(path: Path, *, include_item: bool) -> frozenset[str]:
    """Identity keys of a downloaded file.

    Filename key, the EPUB title for readable EPUBs, and, when ``include_item``,
    the item folder name (Humble's title, e.g. ``Athena #1`` for
    ``athena_issue1.pdf``). Only pass ``include_item`` for an item contributing a
    single file, or every file of a multi-file item would collapse into one book.
    """
    keys = {filename_key(path.name)}
    if include_item:
        keys |= item_title_keys(path.parent.name)
    if path.suffix.lower() == EPUB_SUFFIX:
        title = read_epub_title(path)
        keys.add(norm(title) if title else "")
    keys.discard("")
    return frozenset(keys)


def item_title_keys(item: str) -> set[str]:
    """Keys from a Humble item title (the item folder name).

    The full title, plus the part before the first ``" - "`` (a sanitized ``:``)
    when that part contains a digit: ``The Boys Vol. 2 - Get Some`` also yields
    ``theboysvol2``, matching Books' ``The Boys Vol 2``. Number-less prefixes such
    as ``Red Sonja`` are too generic and are not used.
    """
    keys = {norm(item)}
    prefix, sep, _ = item.partition(_SUBTITLE_SEPARATOR)
    if sep and _DIGIT.search(prefix):
        keys.add(norm(prefix))
    return keys


def keys_for_selection(selected: Sequence[Path]) -> dict[Path, frozenset[str]]:
    """``book_keys`` for each selected file; item titles only for single-file items."""
    per_item = Counter(path.parent for path in selected)
    return {path: book_keys(path, include_item=per_item[path.parent] == 1) for path in selected}


def dedupe(
    paths: Sequence[Path], keys: Mapping[Path, frozenset[str]]
) -> tuple[list[Path], list[Path]]:
    """Keep one file per book across bundles; books share identity via ANY common key.

    EPUBs are considered before PDFs, then in sorted path order; the first wins.

    Returns:
        ``(kept, dropped)``, each sorted.
    """
    seen: set[str] = set()
    kept: list[Path] = []
    dropped: list[Path] = []
    for path in sorted(paths, key=lambda p: (p.suffix.lower() != EPUB_SUFFIX, p)):
        (dropped if keys[path] & seen else kept).append(path)
        seen |= keys[path]
    return sorted(kept), sorted(dropped)


def partition(
    selected: Sequence[Path],
    keys: Mapping[Path, frozenset[str]],
    in_library: Collection[str],
) -> tuple[list[Path], list[Path], list[Path]]:
    """Split ``selected`` into ``(to_consider, duplicates, already_in_books)``."""
    kept, duplicates = dedupe(selected, keys)
    in_books = [p for p in kept if keys[p] & set(in_library)]
    skipped = set(in_books)
    return [p for p in kept if p not in skipped], duplicates, in_books


def _group_key(name: str, known: Collection[str]) -> str:
    """Group key of an entry; ``<base> N`` joins ``<base>`` only if ``<base>`` exists."""
    stem = _COLLISION_SUFFIX.sub("", PurePath(name).stem)
    match = _COPY_NUMBER.match(stem)
    if match and norm(match.group(1)) in known:
        return norm(match.group(1))
    return norm(stem)


def duplicate_groups(names: Sequence[str]) -> dict[str, list[str]]:
    """Library entries grouped by book; only groups with more than one entry."""
    entries = [n for n in sorted(names) if is_book_entry(n)]
    known = library_keys(entries)
    groups: dict[str, list[str]] = {}
    for name in entries:
        groups.setdefault(_group_key(name, known), []).append(name)
    return {key: found for key, found in sorted(groups.items()) if len(found) > 1}


def library_duplicates_report(library_dir: Path | None = None) -> tuple[str, ...]:
    """Read-only report: one line per duplicated book, then a count line."""
    names = read_library_names(library_dir or default_library_dir())
    groups = duplicate_groups(names)
    lines = [MSG_DUP_LINE.join(found) for found in groups.values()]
    lines.append(MSG_DUP_TOTAL.format(count=len(groups)))
    return tuple(lines)
