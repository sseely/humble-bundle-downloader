"""Readable titles and writers for Apple Books entries named after their files.

Books titles a PDF with no metadata after its file (``rickandmortypresents_vol5``)
and shows "Unknown Author". This module decides the Humble title for each such
entry and matches writer credits looked up online (Comic Vine first, Open
Library as a fallback). Everything here is pure; the HTTP clients live in
``api/`` and the Books UI automation in ``scripts/books_collections/``.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from humble_dl.services.books_collections import (
    LABEL_SEPARATOR,
    NOT_A_BOOK,
    Owner,
    is_file_named,
    strip_progress,
)
from humble_dl.services.books_library import norm

UNKNOWN_AUTHOR = "Unknown Author"
MAX_AUTHORS = 3
WRITER_ROLE = "writer"
ROLE_SEPARATOR = ","
FOLDER_SEPARATOR = "/"
AMBIGUOUS_JOIN = " | "
SKIP_NO_ITEM = "no matching Humble item"
SKIP_NO_TITLE = "no saved Humble title; run `humble-dl titles`"
SKIP_AMBIGUOUS = "matches several items: "
SOURCE_COMICVINE = "comicvine"
SOURCE_OPENLIBRARY = "openlibrary"
SOURCE_NONE = "none"
SOURCE_OVERRIDE = "override"
OVERRIDES_NOTE = "title_overrides.json"
OVERRIDE_DUPLICATE = "duplicate_of"  # override marking a copy to remove, naming the kept one
NOTE_OPEN_LIBRARY = "first listed contributor"
NOTE_NO_MATCH = "no match"
FIRST_ISSUE = 1
MAX_VOLUME_TRIES = 3  # candidate series checked for a "Volume N" issue

_PUBLISHER_SPLIT = re.compile(r"\s+by\s+", re.IGNORECASE)
_YEAR = re.compile(r"\s*\((\d{4})\)")
_ISSUE = re.compile(r"\s*(?:Issue\s*)?#\s*(\d+)", re.IGNORECASE)
_VOLUME = re.compile(r",?\s*Vol(?:ume)?\.?\s*(\d+)", re.IGNORECASE)
_TRAILING_NUMBER = re.compile(r"\s+(\d+)$")
_VOLUME_NAME = re.compile(r"^\s*vol(?:ume|\.)?\b", re.IGNORECASE)
_SERIES_TRIM = " ,:-–"


@dataclass(frozen=True)
class Rename:
    """A Books entry to retitle or give an author; ``author`` is what Books shows now.

    ``owner`` is the Humble item the entry came from, or None for a book that
    matches no bundle (its title is then the one Books shows).
    """

    label: str
    owner: Owner | None
    title: str
    author: str


@dataclass(frozen=True)
class SeriesQuery:
    """A title split into a series name, an issue/volume number and a year."""

    series: str
    number: int | None
    year: int | None
    collected: bool = False  # "Vol. N" (a collected edition), not issue "#N"


@dataclass(frozen=True)
class CvVolume:
    """A Comic Vine volume (series) as returned by its search."""

    id: int
    name: str
    publisher: str | None
    start_year: int | None
    issues: int


@dataclass(frozen=True)
class CvCredit:
    """A person credited on a Comic Vine issue; ``role`` may list several roles."""

    name: str
    role: str


@dataclass(frozen=True)
class CvIssue:
    """A Comic Vine issue; collected editions are named like "Volume Five"."""

    id: int
    name: str | None


@dataclass(frozen=True)
class OlDoc:
    """An Open Library search result."""

    title: str
    authors: list[str]
    publishers: list[str]


# --- labels and progress ------------------------------------------------------


def label_parts(label: str) -> tuple[str, str]:
    """``(title, author)`` of a label; file-named titles never contain ``", "``."""
    title, _, author = strip_progress(label).partition(LABEL_SEPARATOR)
    return title, author


def new_key(title: str, author: str) -> str:
    """The progress key (label without reading status) Books shows after a rename."""
    return f"{title}{LABEL_SEPARATOR}{author}"


def move_progress(done: Mapping[str, Sequence[str]], old: str, new: str) -> dict[str, list[str]]:
    """Collections progress with ``old``'s record merged into ``new``."""
    moved = {key: list(names) for key, names in done.items() if key != old}
    if old in done:
        kept = moved.setdefault(new, [])
        kept.extend(n for n in done[old] if n not in kept)
    return moved


# --- rename plan --------------------------------------------------------------


def _titled(owners: Iterable[Owner], titles: Mapping[str, str]) -> dict[Owner, str]:
    found = {o: titles.get(f"{o[0]}{FOLDER_SEPARATOR}{o[1]}") for o in owners}
    return {o: " ".join(t.split()) for o, t in found.items() if t is not None}


def _narrow(
    titled: dict[Owner, str], file_title: str, stems: Mapping[Owner, set[str]]
) -> dict[Owner, str]:
    """Several distinct titles: keep the items holding a file named like the entry."""
    if len(set(titled.values())) == 1:
        return titled
    holding = {o: t for o, t in titled.items() if file_title in stems.get(o, set())}
    return holding or titled


def _decide(
    label: str,
    owners: set[Owner],
    titles: Mapping[str, str],
    stems: Mapping[Owner, set[str]],
) -> Rename | str:
    """A rename for ``label``, or the reason it is skipped."""
    file_title, author = label_parts(label)
    titled = _titled(owners, titles)
    if not titled:
        return SKIP_NO_TITLE
    titled = _narrow(titled, file_title, stems)
    distinct = sorted(set(titled.values()))
    if len(distinct) > 1:
        return SKIP_AMBIGUOUS + AMBIGUOUS_JOIN.join(distinct)
    owner = min(titled)
    return Rename(label, owner, titled[owner], author)


def _author_only(label: str, owners: set[Owner] | None) -> Rename | None:
    """A titled entry showing "Unknown Author" keeps its title and gets an author."""
    title, author = label_parts(label)
    if label == NOT_A_BOOK or author != UNKNOWN_AUTHOR:
        return None
    return Rename(label, min(owners) if owners else None, title, author)


def rename_plan(
    labels: Iterable[str],
    assigned: Mapping[str, set[Owner]],
    titles: Mapping[str, str],
    duplicates: set[str],
    stems: Mapping[Owner, set[str]],
) -> tuple[list[Rename], dict[str, str]]:
    """Entries to retitle or give an author, and skipped label -> reason.

    File-named labels get their Humble title (duplicates left out); titled
    labels showing "Unknown Author" keep their title. ``stems`` maps an item to
    the file stems in its folder; it settles a label matching several items.
    """
    renames: list[Rename] = []
    skipped: dict[str, str] = {}
    for label in labels:
        owners = assigned.get(label)
        if label in duplicates:
            continue
        if not is_file_named(label):
            found = _author_only(label, owners)
            renames.extend([found] if found else [])
            continue
        decision = _decide(label, owners, titles, stems) if owners else SKIP_NO_ITEM
        if isinstance(decision, Rename):
            renames.append(decision)
        else:
            skipped[label] = decision
    return renames, skipped


def item_stems(output_dir: Path, owners: Iterable[Owner]) -> dict[Owner, set[str]]:
    """File stems in each ``<bundle>/<item>`` folder (hidden files left out)."""
    stems: dict[Owner, set[str]] = {}
    for bundle, item in owners:
        folder = output_dir / bundle / item
        files = folder.iterdir() if folder.is_dir() else iter(())
        stems[(bundle, item)] = {f.stem for f in files if not f.name.startswith(".")}
    return stems


# --- publisher and series -----------------------------------------------------


def bundle_publisher(bundle: str) -> str | None:
    """The publisher named after the last "by" in a bundle name, if any."""
    parts = _PUBLISHER_SPLIT.split(bundle)
    return parts[-1].strip() if len(parts) > 1 else None


def _clean(series: str) -> str:
    return series.strip(_SERIES_TRIM)


def series_query(title: str) -> SeriesQuery:
    """Split ``"Series Vol. 5: Subtitle"`` / ``"Series #2"`` into series and number."""
    year_match = _YEAR.search(title)
    year = int(year_match.group(1)) if year_match else None
    text = _YEAR.sub("", title, count=1)
    issue = _ISSUE.search(text)
    if issue:
        series = _VOLUME.sub("", text[: issue.start()])
        return SeriesQuery(_clean(series), int(issue.group(1)), year)
    for pattern in (_VOLUME, _TRAILING_NUMBER):
        found = pattern.search(text)
        if found:
            series, number = _clean(text[: found.start()]), int(found.group(1))
            return SeriesQuery(series, number, year, collected=pattern is _VOLUME)
    return SeriesQuery(_clean(text), None, year)


# --- Comic Vine ---------------------------------------------------------------


def _publisher_ok(found: str | None, wanted: str | None) -> bool:
    if wanted is None:
        return True
    return found is not None and norm(wanted) in norm(found)


def _fits(volume: CvVolume, query: SeriesQuery, publisher: str | None) -> bool:
    """Same series name and publisher, and enough issues for the wanted number."""
    if norm(volume.name) != norm(query.series):
        return False
    if not _publisher_ok(volume.publisher, publisher):
        return False
    return query.number is None or volume.issues >= query.number


def _start_year(volume: CvVolume) -> int:
    return volume.start_year or 0


def _started_by(volume: CvVolume, ref_year: int | None) -> bool:
    return ref_year is not None and 0 < _start_year(volume) <= ref_year


def _ordered(volumes: list[CvVolume], collected: bool, latest_first: bool) -> list[CvVolume]:
    if collected:
        return sorted(volumes, key=lambda v: (v.issues, _start_year(v)))
    return sorted(volumes, key=_start_year, reverse=latest_first)


def rank_volumes(
    candidates: Iterable[CvVolume],
    query: SeriesQuery,
    publisher: str | None,
    ref_year: int | None,
) -> list[CvVolume]:
    """Volumes fitting ``query`` (same name and publisher, enough issues), best first.

    A volume started in the title's own year comes first. Then come volumes
    started by the title's year (else ``ref_year``, the file's creation year),
    then the rest: shortest first for a collected "Vol. N" (collected editions
    form short series beside the monthly one), else latest started first, with
    the rest earliest first.
    """
    matches = [v for v in candidates if _fits(v, query, publisher)]
    exact, others = _split(matches, lambda v: v.start_year == query.year)
    bound = query.year if query.year is not None else ref_year
    started, rest = _split(others, lambda v: _started_by(v, bound))
    ordered = _ordered(started, query.collected, latest_first=True)
    return exact + ordered + _ordered(rest, query.collected, latest_first=False)


def _split(
    volumes: list[CvVolume], keep: Callable[[CvVolume], bool]
) -> tuple[list[CvVolume], list[CvVolume]]:
    """``(volumes where keep is true, the others)``, each in input order."""
    kept = [v for v in volumes if keep(v)]
    return kept, [v for v in volumes if v not in kept]


def choose_volume(
    candidates: Iterable[CvVolume],
    query: SeriesQuery,
    publisher: str | None,
    ref_year: int | None,
) -> CvVolume | None:
    """The best-ranked volume for ``query`` (see ``rank_volumes``), if any."""
    ranked = rank_volumes(candidates, query, publisher, ref_year)
    return ranked[0] if ranked else None


def writers(credits: Iterable[CvCredit]) -> list[str]:
    """Names credited as writer, in credit order, without repeats."""
    found: list[str] = []
    for credit in credits:
        roles = {role.strip() for role in credit.role.split(ROLE_SEPARATOR)}
        if WRITER_ROLE in roles and credit.name not in found:
            found.append(credit.name)
    return found


def author_text(names: Sequence[str]) -> str | None:
    """``"A"``, ``"A & B"`` or ``"A, B & C"`` (at most ``MAX_AUTHORS``); None if empty."""
    kept = list(names[:MAX_AUTHORS])
    if not kept:
        return None
    if len(kept) == 1:
        return kept[0]
    return f"{', '.join(kept[:-1])} & {kept[-1]}"


# --- Open Library -------------------------------------------------------------


def pick_open_library(docs: Iterable[OlDoc], title: str, publisher: str | None) -> list[str]:
    """The first listed author of the first exact-title match from ``publisher``.

    Open Library lists every contributor (artists, colorists) with no roles; the
    first is usually the writer, so only that one is kept.
    """
    for doc in docs:
        if norm(doc.title) != norm(title) or not doc.authors:
            continue
        if publisher is None or any(_publisher_ok(p, publisher) for p in doc.publishers):
            return doc.authors[:1]
    return []


# --- writer lookup ------------------------------------------------------------


class CvSource(Protocol):
    """Comic Vine lookups (``api.bookmeta.ComicVine``)."""

    def volumes(self, series: str) -> list[CvVolume]: ...
    def issue(self, volume_id: int, number: int) -> CvIssue | None: ...
    def credits(self, issue_id: int) -> list[CvCredit]: ...


class OlSource(Protocol):
    """Open Library lookups (``api.bookmeta.OpenLibrary``)."""

    def search(self, title: str) -> list[OlDoc]: ...


@dataclass(frozen=True)
class Found:
    """A lookup result: the author text (None if none), where it came from, and why."""

    author: str | None
    source: str
    note: str


def _pick_issue(
    cv: CvSource, ranked: list[CvVolume], number: int, collected: bool
) -> tuple[CvVolume, CvIssue] | None:
    """The first ranked volume holding issue ``number``; for a collected "Vol. N",
    the first whose issue is named like a volume ("Volume Five", not "Book Five")."""
    fallback: tuple[CvVolume, CvIssue] | None = None
    for volume in ranked[:MAX_VOLUME_TRIES]:
        issue = cv.issue(volume.id, number)
        if issue is None:
            continue
        if not collected or _VOLUME_NAME.match(issue.name or ""):
            return volume, issue
        fallback = fallback or (volume, issue)
    return fallback


def _comic_vine(
    title: str, publisher: str | None, ref_year: int | None, cv: CvSource
) -> Found | None:
    query = series_query(title)
    ranked = rank_volumes(cv.volumes(query.series), query, publisher, ref_year)
    number = query.number or FIRST_ISSUE
    picked = _pick_issue(cv, ranked, number, query.collected)
    if picked is None:
        return None
    volume, issue = picked
    author = author_text(writers(cv.credits(issue.id)))
    if author is None:
        return None
    return Found(author, SOURCE_COMICVINE, f"{volume.name} ({volume.start_year}) #{number}")


def find_writer(
    title: str,
    publisher: str | None,
    ref_year: int | None,
    cv: CvSource | None,
    ol: OlSource | None,
) -> Found:
    """Writer credits from Comic Vine, else Open Library's first contributor."""
    found = _comic_vine(title, publisher, ref_year, cv) if cv is not None else None
    if found is not None:
        return found
    # Without a bundle publisher to check, any same-titled book would match.
    use_ol = ol is not None and publisher is not None
    names = pick_open_library(ol.search(title), title, publisher) if use_ol and ol else []
    author = author_text(names)
    if author is None:
        return Found(None, SOURCE_NONE, NOTE_NO_MATCH)
    return Found(author, SOURCE_OPENLIBRARY, NOTE_OPEN_LIBRARY)


# --- review records -----------------------------------------------------------


def book_entry(rename: Rename, found: Found) -> dict[str, str | None] | None:
    """The reviewable ``book_info.json`` record for an entry; None if nothing changes."""
    title, _ = label_parts(rename.label)
    if rename.title == title and found.author is None:
        return None
    item = f"{rename.owner[0]}{FOLDER_SEPARATOR}{rename.owner[1]}" if rename.owner else None
    return {
        "title": rename.title,
        "author": found.author,
        "source": found.source,
        "note": found.note,
        "item": item,
    }


def rename_targets(
    entries: Mapping[str, Mapping[str, str | None]], renamed: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """Old key (label without status) -> (title, author) for entries not renamed yet.

    An entry with no author found keeps the author Books shows now.
    """
    targets: dict[str, tuple[str, str]] = {}
    for label, entry in entries.items():
        old = strip_progress(label)
        if old in renamed:
            continue
        current_title, current_author = label_parts(label)
        title = entry.get("title") or current_title
        targets[old] = (title, entry.get("author") or current_author)
    return targets


# --- overrides ----------------------------------------------------------------

Entry = dict[str, str | None]


def _override_entry(label: str, override: Mapping[str, str | None], planned: Entry | None) -> Entry:
    """Override values win; a missing title keeps the planned (else current) one."""
    title = (planned or {}).get("title") or label_parts(label)[0]
    return {
        "title": override.get("title") or title,
        "author": override.get("author"),
        "source": SOURCE_OVERRIDE,
        "note": OVERRIDES_NOTE,
        "item": None,
    }


def apply_overrides(
    entries: Mapping[str, Entry],
    skipped: Mapping[str, str],
    labels: Iterable[str],
    overrides: Mapping[str, Mapping[str, str | None]],
) -> tuple[dict[str, Entry], dict[str, str], dict[str, str]]:
    """Merge hand-written ``title_overrides.json`` (keyed by label without status).

    Returns ``(entries, skipped, duplicates)``: an override with a title or
    author replaces the looked-up entry; one with ``duplicate_of`` takes the
    label out and lists it as a copy to remove (label -> the copy kept).
    """
    merged = {label: dict(entry) for label, entry in entries.items()}
    left = dict(skipped)
    duplicates: dict[str, str] = {}
    for label in labels:
        override = overrides.get(strip_progress(label))
        if override is None:
            continue
        left.pop(label, None)
        kept = override.get(OVERRIDE_DUPLICATE)
        if kept:
            merged.pop(label, None)
            duplicates[label] = kept
        else:
            merged[label] = _override_entry(label, override, merged.get(label))
    return merged, left, duplicates


def removal_targets(remove: Mapping[str, str], removed: Iterable[str]) -> dict[str, str]:
    """Key (label without status) -> kept copy, for duplicates not removed yet."""
    done = set(removed)
    keyed = {strip_progress(label): kept for label, kept in remove.items()}
    return {key: kept for key, kept in keyed.items() if key not in done}
