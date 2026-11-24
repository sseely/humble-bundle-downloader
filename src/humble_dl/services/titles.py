"""Humble's own item titles, keyed by the ``<Bundle>/<Item>`` folders sync writes.

Folder names lose ``:`` and ``/`` (see ``lib/naming.py``), so tools that relabel
books (the Apple Books rename pass) read the exact titles from here instead.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from humble_dl.api.client import LibraryApiError
from humble_dl.lib.logging import get_logger
from humble_dl.lib.naming import item_dir_names, sanitize_component
from humble_dl.models.library import BundleRef, Order, ebook_items
from humble_dl.services.sync import filter_bundles

FOLDER_SEPARATOR = "/"
TMP_SUFFIX = ".tmp"
EVENT_BUNDLE_FAILED = "titles_bundle_failed"

_log = get_logger(__name__)


class BundleSource(Protocol):
    """The slice of ``LibraryClient`` that reads bundle pages without signing URLs."""

    async def list_bundles(self) -> list[BundleRef]: ...
    async def get_bundle(self, bundle_id: str) -> Order: ...


def item_titles(order: Order) -> dict[str, str]:
    """``"<bundle dir>/<item dir>"`` -> Humble's item title, for each ebook item."""
    items = ebook_items(order)
    names = item_dir_names([(sub.human_name, sub.machine_name) for sub, _ in items])
    bundle_dir = sanitize_component(order.human_name)
    return {
        f"{bundle_dir}{FOLDER_SEPARATOR}{names[sub.machine_name]}": " ".join(sub.human_name.split())
        for sub, _ in items
    }


async def fetch_titles(source: BundleSource, only: str | None) -> tuple[dict[str, str], list[str]]:
    """Titles from every bundle (or those matching ``only``) and the bundles that failed."""
    titles: dict[str, str] = {}
    failed: list[str] = []
    for bundle in filter_bundles(await source.list_bundles(), only):
        try:
            order = await source.get_bundle(bundle.id)
        except LibraryApiError as exc:
            _log.error(EVENT_BUNDLE_FAILED, extra={"bundle": bundle.title, "error": str(exc)})
            failed.append(bundle.title)
            continue
        titles.update(item_titles(order))
    return titles, failed


def load_titles(path: Path) -> dict[str, str]:
    """The saved titles, or an empty mapping when none were saved yet."""
    if not path.exists():
        return {}
    data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    return data


def save_titles(path: Path, titles: Mapping[str, str]) -> None:
    """Merge ``titles`` into the saved file (atomic write, keys sorted)."""
    merged = {**load_titles(path), **titles}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    text = json.dumps(dict(sorted(merged.items())), indent=1, ensure_ascii=False)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
