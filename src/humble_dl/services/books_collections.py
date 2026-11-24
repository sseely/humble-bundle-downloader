"""Plan Apple Books collections: one per Humble bundle folder.

Books exposes each library entry only as an accessibility label such as
``"Redshirts, John Scalzi, 3% finished"``. This module maps those labels to the
``<Bundle>/<Item>`` folders they came from and decides which collections each
book belongs in. Everything here is pure apart from reading the download folder;
the Books UI automation lives in ``scripts/books_collections/``.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from humble_dl.services.books import PREFERRED_SUFFIXES, is_current, item_files, subdirs
from humble_dl.services.books_library import book_keys, filename_key, item_title_keys, norm

Owner = tuple[str, str]  # (bundle folder name, item folder name)

UNFILED_COLLECTION = "My Books"  # books that match no bundle
NOT_A_BOOK = "Uploaded to iCloud"  # a status row Books lists among the books
LABEL_SEPARATOR = ", "
PDF_SUFFIX = ".pdf"
CLOUD_ONLY_SUFFIX = ", In iCloud"  # not downloaded: Books hides Rename for these

# Reading status Books appends to a label; it changes as the book is read.
_PROGRESS = re.compile(r", (?:\d+% finished|New|Finished book|Finished|Sample)(?:, In iCloud)?$")


@dataclass(frozen=True)
class CollectionsPlan:
    """``collections``: label -> collection names; ``duplicates``: labels left out."""

    collections: dict[str, list[str]]
    duplicates: list[str]

    def counts(self) -> list[tuple[str, int]]:
        """Books per collection, largest first."""
        totals: dict[str, int] = defaultdict(int)
        for names in self.collections.values():
            for name in names:
                totals[name] += 1
        return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


def strip_progress(label: str) -> str:
    """The label without its reading status: a stable identity for a library entry."""
    return _PROGRESS.sub("", label)


def is_cloud_only(label: str) -> bool:
    """True when the book is in iCloud only; Books offers no Rename until it is downloaded."""
    return label.endswith(CLOUD_ONLY_SUFFIX)


def is_file_named(label: str) -> bool:
    """True when Books titled the entry after its file (``rickandmorty_vol1``)."""
    title = strip_progress(label).split(LABEL_SEPARATOR)[0]
    return "_" in title or (" " not in title and title == title.lower())


def rekey_progress(done: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Merge progress records whose labels differ only in reading status."""
    merged: dict[str, list[str]] = {}
    for label, names in done.items():
        kept = merged.setdefault(strip_progress(label), [])
        kept.extend(n for n in names if n not in kept)
    return merged


def _item_keys(item: Path) -> frozenset[str]:
    files = [f for f in item.iterdir() if is_current(f) and f.suffix.lower() in PREFERRED_SUFFIXES]
    keys: set[str] = set()
    for path in files:
        keys |= book_keys(path, include_item=False)
    if files and len(item_files(item)) == 1:
        keys |= item_title_keys(item.name)
    return frozenset(keys)


def owner_index(output_dir: Path) -> dict[str, set[Owner]]:
    """Book key -> the ``(bundle, item)`` folders holding a book with that key."""
    index: dict[str, set[Owner]] = defaultdict(set)
    if not output_dir.is_dir():
        return {}
    for bundle in subdirs(output_dir):
        for item in subdirs(bundle):
            for key in _item_keys(item):
                index[key].add((bundle.name, item.name))
    return dict(index)


def _candidate_keys(label: str) -> list[set[str]]:
    """Key sets for each leading run of label segments, longest first."""
    segments = strip_progress(label).split(LABEL_SEPARATOR)
    candidates: list[set[str]] = []
    for n in range(len(segments), 0, -1):
        title = LABEL_SEPARATOR.join(segments[:n])
        candidates.append({norm(title), filename_key(title + PDF_SUFFIX)} | item_title_keys(title))
    return candidates


def _owners(label: str, index: Mapping[str, set[Owner]]) -> set[Owner]:
    for keys in _candidate_keys(label):
        owners = set[Owner]().union(*(index.get(k, set[Owner]()) for k in keys))
        if owners:
            return owners
    return set()


def assign_labels(
    labels: Iterable[str], index: Mapping[str, set[Owner]]
) -> tuple[dict[str, set[Owner]], list[str]]:
    """Match each label to its owning folders; returns (assigned, unmatched labels)."""
    assigned: dict[str, set[Owner]] = {}
    unmatched: list[str] = []
    for label in labels:
        owners = _owners(label, index)
        if owners:
            assigned[label] = owners
        else:
            unmatched.append(label)
    return assigned, unmatched


def duplicate_keepers(assigned: Mapping[str, set[Owner]]) -> dict[str, str]:
    """File-named label -> the title-named label of the same item, which is kept."""
    by_item: dict[Owner, list[str]] = defaultdict(list)
    for label, owners in assigned.items():
        for owner in owners:
            by_item[owner].append(label)
    keepers: dict[str, str] = {}
    for labels in by_item.values():
        titled = sorted(label for label in labels if not is_file_named(label))
        if titled:
            keepers.update({label: titled[0] for label in labels if is_file_named(label)})
    return keepers


def _duplicates(assigned: Mapping[str, set[Owner]]) -> set[str]:
    """File-named labels of items that also have a title-named label."""
    return set(duplicate_keepers(assigned))


def build_plan(labels: Iterable[str], assigned: Mapping[str, set[Owner]]) -> CollectionsPlan:
    """Collections for every library label; unmatched books go to ``UNFILED_COLLECTION``."""
    dups = _duplicates(assigned)
    collections: dict[str, list[str]] = {}
    for label in labels:
        if label == NOT_A_BOOK or label in dups:
            continue
        owners = assigned.get(label)
        collections[label] = sorted({b for b, _ in owners}) if owners else [UNFILED_COLLECTION]
    return CollectionsPlan(collections=collections, duplicates=sorted(dups))


def pending(
    collections: Mapping[str, Sequence[str]], done: Mapping[str, Sequence[str]]
) -> dict[str, list[str]]:
    """Collections each label still needs; ``done`` is keyed by ``strip_progress`` label."""
    todo: dict[str, list[str]] = {}
    for label, names in collections.items():
        finished = done.get(strip_progress(label), ())
        left = [n for n in names if n not in finished]
        if left:
            todo[label] = left
    return todo
