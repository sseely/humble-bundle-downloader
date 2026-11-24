# pyright: basic
"""Decide new titles and authors for Books entries and write book_info.json.

File-named entries (``rickandmortypresents_vol5``) get Humble's title from
``humble-dl titles``; every entry showing "Unknown Author" gets writer credits
from Comic Vine (when HUMBLE_DL_COMICVINE_API_KEY is set) or Open Library.
Hand-written title_overrides.json entries win. Duplicate copies (file-named
copies of a titled book, and overrides marked ``duplicate_of``) go to
remove.json. Nothing in Books changes here: review book_info.json and
remove.json, then run rename_pass.py and remove_pass.py.
Responses are cached in lookup_cache.json, so a re-run makes no new requests.
"""

import os
from collections import Counter
from pathlib import Path

import httpx
import store

from humble_dl.api.bookmeta import ComicVine, JsonFetcher, LookupFailedError, OpenLibrary
from humble_dl.config import load_settings
from humble_dl.lib.pdfmeta import pdf_creation_year
from humble_dl.services import book_info as bi
from humble_dl.services import books_collections as bc
from humble_dl.services.titles import load_titles

ENV_COMICVINE_KEY = "HUMBLE_DL_COMICVINE_API_KEY"
SOURCE_ERROR = "error"
SAVE_EVERY = 10  # lookups between cache saves


def ref_year(output_dir: Path, rename: bi.Rename) -> int | None:
    if rename.owner is None:
        return None
    pdf = next((output_dir / rename.owner[0] / rename.owner[1]).glob("*.pdf"), None)
    return pdf_creation_year(pdf) if pdf else None


def plan(labels: list[str], output_dir: Path, titles_path: Path):
    """``(renames, skipped, duplicate keepers)`` for the scanned labels."""
    assigned, _ = bc.assign_labels(labels, bc.owner_index(output_dir))
    keepers = bc.duplicate_keepers(assigned)
    owners = {o for found in assigned.values() for o in found}
    stems = bi.item_stems(output_dir, owners)
    renames, skipped = bi.rename_plan(
        labels, assigned, load_titles(titles_path), set(keepers), stems
    )
    return renames, skipped, keepers


def lookup(rename: bi.Rename, output_dir: Path, cv, ol) -> bi.Found:
    if rename.author != bi.UNKNOWN_AUTHOR:
        return bi.Found(None, bi.SOURCE_NONE, "author already set")
    publisher = bi.bundle_publisher(rename.owner[0]) if rename.owner else None
    try:
        return bi.find_writer(rename.title, publisher, ref_year(output_dir, rename), cv, ol)
    except LookupFailedError as err:
        return bi.Found(None, SOURCE_ERROR, str(err))


def run_lookups(renames: list[bi.Rename], output_dir: Path, cache: dict, key: str | None):
    """Book entries to change, and how many lookups each source answered."""
    entries: dict[str, dict[str, str | None]] = {}
    sources: Counter[str] = Counter()
    with httpx.Client() as client:
        fetcher = JsonFetcher(client, cache)
        cv = ComicVine(fetcher, key) if key else None
        ol = OpenLibrary(fetcher)
        for count, rename in enumerate(renames, 1):
            found = lookup(rename, output_dir, cv, ol)
            sources[found.source] += 1
            entry = bi.book_entry(rename, found)
            if entry is not None:
                entries[rename.label] = entry
            print(f"{count:3}/{len(renames)} {found.source:11} {rename.title} | {found.author}")
            if count % SAVE_EVERY == 0:
                store.save(store.LOOKUP_CACHE_FILE, cache)
    return entries, sources


def report(entries: dict, sources: Counter[str], skipped: dict[str, str]) -> None:
    retitles = sum(1 for label, e in entries.items() if bi.label_parts(label)[0] != e["title"])
    print(f"entries to change: {len(entries)} (new titles: {retitles})")
    print("lookups by source: " + ", ".join(f"{s} {n}" for s, n in sources.most_common()))
    print(f"file-named entries left as they are: {len(skipped)} (see {store.INFO_SKIPPED_FILE})")
    print(f"Review {store.STATE_DIR / store.BOOK_INFO_FILE}, then run rename_pass.py")


def main() -> int:
    labels: list[str] | None = store.load(store.LABELS_FILE)
    if labels is None:
        print(f"No {store.LABELS_FILE} in {store.STATE_DIR}; run scan.py first")
        return 1
    settings = load_settings(os.environ, Path.home())
    renames, skipped, keepers = plan(labels, settings.output_dir, settings.titles_path)
    cache = store.load(store.LOOKUP_CACHE_FILE, {})
    key = os.environ.get(ENV_COMICVINE_KEY)
    print(f"{ENV_COMICVINE_KEY}: {'set' if key else 'not set (Open Library only)'}")
    entries, sources = run_lookups(renames, settings.output_dir, cache, key)
    store.save(store.LOOKUP_CACHE_FILE, cache)
    overrides = store.load(store.OVERRIDES_FILE, {})
    entries, skipped, extra = bi.apply_overrides(entries, skipped, labels, overrides)
    remove = {**keepers, **extra}
    store.save(store.BOOK_INFO_FILE, entries)
    store.save(store.INFO_SKIPPED_FILE, skipped)
    store.save(store.REMOVE_FILE, remove)
    report(entries, sources, skipped)
    print(f"duplicate copies to remove: {len(remove)} (review {store.REMOVE_FILE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
