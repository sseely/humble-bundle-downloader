"""Tests for humble_dl.services.titles: Humble's own item titles by folder."""

import json
from pathlib import Path

from humble_dl.api.client import LibraryApiError
from humble_dl.models.library import BundleRef, Download, FileVersion, Order, Subproduct
from humble_dl.services.titles import fetch_titles, item_titles, load_titles, save_titles

BUNDLE = "Humble Comics Bundle: Example Comics by Oni Press"
BUNDLE_DIR = "Humble Comics Bundle - Example Comics by Oni Press"
WORLDS = "Rick and Morty: Worlds Apart"
WORLDS_DIR = "Rick and Morty - Worlds Apart"
HACK = "Hack/Slash/Eva #1"
HACK_DIR = "Hack - Slash - Eva #1"
EBOOK = "ebook"


def sub(machine: str, human: str, platform: str = EBOOK) -> Subproduct:
    files = [FileVersion(label="PDF", url="u", md5=None, file_size=None)]
    return Subproduct(
        machine_name=machine, human_name=human, downloads=[Download(platform=platform, files=files)]
    )


def order(subs: list[Subproduct], human: str = BUNDLE, gamekey: str = "gk") -> Order:
    return Order(gamekey=gamekey, human_name=human, subproducts=subs)


def test_item_titles_keys_by_sanitized_folders() -> None:
    titles = item_titles(order([sub("worlds", WORLDS), sub("hack", HACK)]))
    assert titles == {f"{BUNDLE_DIR}/{WORLDS_DIR}": WORLDS, f"{BUNDLE_DIR}/{HACK_DIR}": HACK}


def test_item_titles_skips_non_ebooks_and_disambiguates_like_sync() -> None:
    subs = [sub("a", WORLDS), sub("b", WORLDS), sub("game", "A Game", platform="windows")]
    titles = item_titles(order(subs))
    assert titles == {
        f"{BUNDLE_DIR}/{WORLDS_DIR}": WORLDS,
        f"{BUNDLE_DIR}/{WORLDS_DIR}-b": WORLDS,
    }


class FakeSource:
    def __init__(self, orders: dict[str, Order], failing: set[str] | None = None) -> None:
        self.orders = orders
        self.failing = failing or set()
        self.requested: list[str] = []

    async def list_bundles(self) -> list[BundleRef]:
        return [BundleRef(id=key, title=o.human_name) for key, o in self.orders.items()]

    async def get_bundle(self, bundle_id: str) -> Order:
        self.requested.append(bundle_id)
        if bundle_id in self.failing:
            raise LibraryApiError(f"GET {bundle_id} failed with status 500")
        return self.orders[bundle_id]


async def test_fetch_titles_merges_bundles_and_reports_failures() -> None:
    source = FakeSource(
        {
            "one": order([sub("worlds", WORLDS)]),
            "two": order([sub("x", "Other")], human="Second Bundle"),
            "bad": order([], human="Broken Bundle"),
        },
        failing={"bad"},
    )
    titles, failed = await fetch_titles(source, only=None)
    assert titles == {f"{BUNDLE_DIR}/{WORLDS_DIR}": WORLDS, "Second Bundle/Other": "Other"}
    assert failed == ["Broken Bundle"]


async def test_fetch_titles_only_filters_bundles() -> None:
    source = FakeSource({"one": order([sub("w", WORLDS)]), "two": order([], human="Second")})
    titles, failed = await fetch_titles(source, only="example")
    assert source.requested == ["one"]
    assert list(titles.values()) == [WORLDS]
    assert failed == []


def test_load_titles_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_titles(tmp_path / "titles.json") == {}


def test_save_titles_merges_with_existing_and_sorts(tmp_path: Path) -> None:
    path = tmp_path / "state" / "titles.json"
    save_titles(path, {"B/b": "Old b", "A/a": "A"})
    save_titles(path, {"B/b": "New b", "C/c": "C"})
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "A/a": "A",
        "B/b": "New b",
        "C/c": "C",
    }
    assert list(load_titles(path)) == ["A/a", "B/b", "C/c"]
    assert not list(path.parent.glob("*.tmp"))


def test_item_titles_tidy_whitespace() -> None:
    titles = item_titles(order([sub("w", "Ultimate Guide 2017:  Vol. 54 ")]))
    assert list(titles.values()) == ["Ultimate Guide 2017: Vol. 54"]
