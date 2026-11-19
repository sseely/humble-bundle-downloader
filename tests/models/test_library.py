"""Tests for the flight-page library models and parsers."""

import json
from pathlib import Path

import pytest

from humble_dl.models.library import (
    EBOOK_PLATFORM,
    BundleRef,
    Download,
    FileVersion,
    Order,
    SchemaDriftError,
    Subproduct,
    apply_signed_urls,
    ebook_items,
    parse_bundle,
    parse_library,
    parse_signed_urls,
)

FIXTURES = Path(__file__).parent.parent / "helpers" / "fixtures" / "library"
FIRST_BUNDLE_ID = "00000000-0000-4000-8000-000000000054"
FIRST_BUNDLE_TITLE = "Humble Book Bundle: The John Scalzi Collection: Old Man's War & More"
KAIJU_MACHINE = "kaijupreservationsociety_johnscalzi"
KAIJU_KEY = f"{KAIJU_MACHINE}_ebook"
KAIJU_FILE = f"{KAIJU_MACHINE}.epub"
KAIJU_BOOK_ID = "00000000-0000-4000-8000-000000000047"
KAIJU_MD5 = "23baf40ea9e6a4fc0cabb4017b12dc10"
KAIJU_SIZE = 3040473
REDSHIRTS_KEY = "redshirts_johnscalzi_ebook"
KAIJU_SIGNED = "https://cdn.example.invalid/kaiju.epub?sig=1"
REDSHIRTS_SIGNED = "https://cdn.example.invalid/redshirts.epub?sig=2"
BUNDLE_COUNT = 12
BOOK_COUNT = 22


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def flight_row(row_id: str, payload: object) -> str:
    return f"{row_id}:{json.dumps(payload)}\n"


def book(downloads: object, book_id: str = "b1", title: str = "Book") -> dict[str, object]:
    return {"id": book_id, "title": title, "downloads": downloads}


def raw_download(name: str = "EPUB", web: str | None = "b.epub") -> dict[str, object]:
    struct: dict[str, object] = {
        "name": name,
        "fileSize": 10,
        "md5": "m" * 32,
        "url": {"web": web},
    }
    return {"key": "b_ebook", "platform": "ebook", "downloadStruct": [struct]}


def bundle_flight(books: list[dict[str, object]], title: str | None = "Bundle") -> str:
    item: dict[str, object] = {"id": "i", "entitlements": {"downloads": books}}
    if title is not None:
        item["title"] = title
    return flight_row("1", {"children": {"userLibraryItem": item}})


def fv(name: str, url: str) -> FileVersion:
    return FileVersion(label=name, url=url, md5=None, file_size=None)


def dl(platform: str, files: list[FileVersion], key: str | None = None) -> Download:
    return Download(platform=platform, files=files, key=key)


def sub(downloads: list[Download], machine: str = "m") -> Subproduct:
    return Subproduct(machine_name=machine, human_name=machine, downloads=downloads)


def test_ebook_platform_constant() -> None:
    assert EBOOK_PLATFORM == "ebook"


# --- parse_library -----------------------------------------------------------


def test_parse_library_real_fixture() -> None:
    refs = parse_library(read_fixture("list.rsc.txt"))

    assert len(refs) == BUNDLE_COUNT
    assert refs[0].id == FIRST_BUNDLE_ID
    assert refs[0].title.startswith(FIRST_BUNDLE_TITLE)
    assert len({r.id for r in refs}) == BUNDLE_COUNT


def test_parse_library_dedupes_filters_and_falls_back_to_id() -> None:
    items = [
        {"id": "a", "productEntitlementId": "pa", "title": "A", "type": "bundle"},
        {"id": "x", "productEntitlementId": "pa", "title": "A again", "type": "bundle"},
        {"id": "b", "title": "B", "type": "bundle"},
        {"id": "c", "title": "C", "type": "other"},
        {"id": "d", "type": "bundle"},
        "junk",
    ]
    flight = flight_row("2", {"x": [{"bundleItems": items}]})

    assert parse_library(flight) == [
        BundleRef(id="pa", title="A"),
        BundleRef(id="b", title="B"),
    ]


def test_parse_library_without_bundle_items_raises() -> None:
    with pytest.raises(SchemaDriftError) as info:
        parse_library(flight_row("2", {"other": 1}))

    assert info.value.path == "bundleItems"


# --- parse_bundle ------------------------------------------------------------


def test_parse_bundle_real_fixture() -> None:
    order = parse_bundle(read_fixture("bundle.rsc.txt"), FIRST_BUNDLE_ID)

    assert order.gamekey == FIRST_BUNDLE_ID
    assert order.human_name.startswith("Humble Book Bundle: The John Scalzi")
    assert len(order.subproducts) == BOOK_COUNT
    items = ebook_items(order)
    assert len(items) == BOOK_COUNT
    assert all(len(files) == 1 for _, files in items)


def test_parse_bundle_kaiju_mapping() -> None:
    order = parse_bundle(read_fixture("bundle.rsc.txt"), FIRST_BUNDLE_ID)

    kaiju, files = next(i for i in ebook_items(order) if i[0].machine_name == KAIJU_MACHINE)

    assert kaiju.entitlement_id == KAIJU_BOOK_ID
    assert kaiju.human_name == "The Kaiju Preservation Society"
    assert kaiju.downloads[0].key == KAIJU_KEY
    assert files == [FileVersion(label="EPUB", url=KAIJU_FILE, md5=KAIJU_MD5, file_size=KAIJU_SIZE)]


def test_parse_bundle_skips_undefined_downloads_and_empty_urls() -> None:
    flight = bundle_flight(
        [
            book("$undefined", "skip"),
            book([raw_download(web="")], "empty"),
            book([], "nodl"),
        ]
    )

    order = parse_bundle(flight, "gk")

    assert [s.entitlement_id for s in order.subproducts] == ["empty", "nodl"]
    assert order.subproducts[0].downloads[0].files == []
    assert order.subproducts[1].machine_name == "nodl"
    assert order.subproducts[1].downloads == []


def test_parse_bundle_machine_name_without_platform_suffix() -> None:
    raw = raw_download()
    raw["key"] = "plainkey"

    order = parse_bundle(bundle_flight([book([raw])]), "gk")

    assert order.subproducts[0].machine_name == "plainkey"


def test_parse_bundle_missing_title_names_path() -> None:
    with pytest.raises(SchemaDriftError) as info:
        parse_bundle(bundle_flight([], title=None), "gk")

    assert info.value.path == "userLibraryItem.title"


def test_parse_bundle_undefined_required_field_is_drift() -> None:
    with pytest.raises(SchemaDriftError) as info:
        parse_bundle(bundle_flight([], title="$undefined"), "gk")

    assert info.value.path == "userLibraryItem.title"


def test_parse_bundle_non_object_url_is_drift() -> None:
    raw = raw_download()
    raw["downloadStruct"] = [{"name": "EPUB", "url": "oops"}]

    with pytest.raises(SchemaDriftError) as info:
        parse_bundle(bundle_flight([book([raw])]), "gk")

    assert info.value.path.endswith("downloadStruct.0.url")


def test_parse_bundle_without_library_item_raises() -> None:
    with pytest.raises(SchemaDriftError) as info:
        parse_bundle(flight_row("1", {"nothing": True}), "gk")

    assert info.value.path == "userLibraryItem"


# --- signed urls -------------------------------------------------------------


def test_parse_signed_urls_merges_machines() -> None:
    assert parse_signed_urls(read_fixture("signed_urls.rsc.txt")) == {
        KAIJU_KEY: {
            KAIJU_FILE: KAIJU_SIGNED,
            f"{KAIJU_FILE}.torrent": "https://cdn.example.invalid/kaiju.epub.torrent?sig=1",
        },
        REDSHIRTS_KEY: {
            "redshirts_johnscalzi.epub": REDSHIRTS_SIGNED,
            "redshirts_johnscalzi.epub.torrent": (
                "https://cdn.example.invalid/redshirts.epub.torrent?sig=2"
            ),
        },
    }


@pytest.mark.parametrize(
    "text",
    [
        flight_row("1", {"ok": False, "data": {}}),
        flight_row("0", {"a": 1}),
        "",
    ],
)
def test_parse_signed_urls_not_ok_raises(text: str) -> None:
    with pytest.raises(SchemaDriftError) as info:
        parse_signed_urls(text)

    assert info.value.path == "ok"


def test_parse_signed_urls_malformed_shape_raises() -> None:
    text = flight_row("1", {"ok": True, "data": {"signedUrls": {"m": {"k": "oops"}}}})

    with pytest.raises(SchemaDriftError) as info:
        parse_signed_urls(text)

    assert info.value.path.startswith("data.signedUrls.m.k")


def test_apply_signed_urls_swaps_and_reports_missing() -> None:
    original = Order(
        gamekey="g",
        human_name="h",
        subproducts=[
            sub(
                [
                    dl("ebook", [fv("EPUB", "a.epub"), fv("PDF", "a.pdf")], key="k1"),
                    dl("ebook", [fv("EPUB", "b.epub")], key=None),
                ]
            )
        ],
    )

    result, missing = apply_signed_urls(original, {"k1": {"a.epub": "https://x.invalid/a"}})

    assert missing == ["a.pdf", "b.epub"]
    files = result.subproducts[0].downloads[0].files
    assert [(f.label, f.url) for f in files] == [("EPUB", "https://x.invalid/a")]
    assert result.subproducts[0].downloads[1].files == []
    assert original.subproducts[0].downloads[0].files[0].url == "a.epub"
    assert len(original.subproducts[0].downloads[0].files) == 2


def test_apply_signed_urls_on_real_fixtures() -> None:
    order = parse_bundle(read_fixture("bundle.rsc.txt"), FIRST_BUNDLE_ID)

    result, missing = apply_signed_urls(
        order, parse_signed_urls(read_fixture("signed_urls.rsc.txt"))
    )

    urls = [f.url for _, files in ebook_items(result) for f in files]
    assert sorted(urls) == sorted([KAIJU_SIGNED, REDSHIRTS_SIGNED])
    assert len(missing) == BOOK_COUNT - 2


# --- ebook_items -------------------------------------------------------------


def test_ebook_items_excludes_non_ebook_and_empty() -> None:
    ebook = sub([dl("ebook", [fv("EPUB", "u1")]), dl("windows", [fv("EXE", "u2")])], "a")
    only_other = sub([dl("windows", [fv("EXE", "u3")])], "b")
    empty = sub([dl("ebook", [])], "c")
    order = Order(gamekey="g", human_name="h", subproducts=[ebook, only_other, empty])

    items = ebook_items(order)

    assert [(s.machine_name, [f.url for f in files]) for s, files in items] == [("a", ["u1"])]
