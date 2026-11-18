"""Tests for the legacy-schema library models."""

import copy
import json
from pathlib import Path
from typing import Any, cast

import pytest

from humble_dl.models.library import (
    EBOOK_PLATFORM,
    FileVersion,
    Order,
    SchemaDriftError,
    ebook_items,
    parse_order,
)

FIXTURE_PATH = Path(__file__).parent.parent / "helpers" / "fixtures" / "legacy_order_synthetic.json"
FAKE_GAMEKEY = "FAKEGAMEKEY0001"
BUNDLE_NAME = "Fake Ebook Bundle: Learn Things"
EXPECTED_LABELS = ["PDF", "EPUB", "MOBI"]
EXPECTED_EBOOK_NAMES = ["fake_book_one", "fake_book_two"]


def load_raw_order() -> dict[str, Any]:
    """Load a fresh copy of the synthetic legacy order fixture."""
    return cast(dict[str, Any], json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def raw_order() -> dict[str, Any]:
    return load_raw_order()


def test_ebook_platform_constant() -> None:
    assert EBOOK_PLATFORM == "ebook"


def test_parse_order_flattens_top_level(raw_order: dict[str, Any]) -> None:
    order = parse_order(raw_order)

    assert order.gamekey == FAKE_GAMEKEY
    assert order.human_name == BUNDLE_NAME
    assert [s.machine_name for s in order.subproducts] == [
        "fake_book_one",
        "fake_game",
        "fake_book_two",
        "fake_album",
    ]


def test_parse_order_maps_file_fields(raw_order: dict[str, Any]) -> None:
    order = parse_order(raw_order)

    first = order.subproducts[0].downloads[0].files[0]
    assert first == FileVersion(
        label="PDF",
        url=f"https://dl.humble.com/fake_book_one.pdf?gamekey={FAKE_GAMEKEY}&ttl=1&t=faketoken",
        md5="00000000000000000000000000000004",
        file_size=12345679,
    )
    assert order.subproducts[0].downloads[0].platform == EBOOK_PLATFORM


def test_entry_without_url_is_skipped(raw_order: dict[str, Any]) -> None:
    raw_files = raw_order["subproducts"][0]["downloads"][0]["download_struct"]
    assert len(raw_files) == 4

    order = parse_order(raw_order)

    labels = [f.label for f in order.subproducts[0].downloads[0].files]
    assert labels == EXPECTED_LABELS


@pytest.mark.parametrize("url_value", [None, {}, {"web": None}, {"web": ""}])
def test_entry_with_empty_web_url_is_skipped(
    raw_order: dict[str, Any], url_value: dict[str, Any] | None
) -> None:
    raw_order["subproducts"][0]["downloads"][0]["download_struct"][0]["url"] = url_value

    order = parse_order(raw_order)

    labels = [f.label for f in order.subproducts[0].downloads[0].files]
    assert labels == ["EPUB", "MOBI"]


def test_optional_md5_and_size_default_to_none(raw_order: dict[str, Any]) -> None:
    entry = raw_order["subproducts"][0]["downloads"][0]["download_struct"][0]
    del entry["md5"]
    entry["file_size"] = None

    file = parse_order(raw_order).subproducts[0].downloads[0].files[0]

    assert file.md5 is None
    assert file.file_size is None


def test_unknown_fields_are_ignored(raw_order: dict[str, Any]) -> None:
    raw_order["brand_new_field"] = {"x": 1}
    raw_order["subproducts"][0]["brand_new_field"] = [1, 2]
    raw_order["subproducts"][0]["downloads"][0]["brand_new_field"] = "y"
    raw_order["subproducts"][0]["downloads"][0]["download_struct"][0]["brand_new_field"] = 3

    order = parse_order(raw_order)

    assert isinstance(order, Order)
    assert not hasattr(order, "brand_new_field")
    assert len(order.subproducts[0].downloads[0].files) == 3


def test_ebook_items_returns_only_ebook_subproducts_in_order(
    raw_order: dict[str, Any],
) -> None:
    items = ebook_items(parse_order(raw_order))

    assert [sub.machine_name for sub, _ in items] == EXPECTED_EBOOK_NAMES
    for _, files in items:
        assert [f.label for f in files] == EXPECTED_LABELS


def test_ebook_items_omits_subproduct_with_zero_ebook_files(
    raw_order: dict[str, Any],
) -> None:
    downloads = raw_order["subproducts"][2]["downloads"][0]
    downloads["download_struct"] = [{"name": "Streaming"}]

    items = ebook_items(parse_order(raw_order))

    assert [sub.machine_name for sub, _ in items] == ["fake_book_one"]


def test_ebook_items_merges_multiple_ebook_downloads(raw_order: dict[str, Any]) -> None:
    extra = copy.deepcopy(raw_order["subproducts"][0]["downloads"][0])
    extra["download_struct"] = extra["download_struct"][:1]
    extra["download_struct"][0]["name"] = "CBZ"
    raw_order["subproducts"][0]["downloads"].append(extra)

    items = ebook_items(parse_order(raw_order))

    assert [f.label for f in items[0][1]] == [*EXPECTED_LABELS, "CBZ"]


def test_missing_machine_name_raises_schema_drift(raw_order: dict[str, Any]) -> None:
    del raw_order["subproducts"][0]["machine_name"]

    with pytest.raises(SchemaDriftError) as excinfo:
        parse_order(raw_order)

    assert excinfo.value.path == "subproducts.0.machine_name"
    assert "subproducts.0.machine_name" in str(excinfo.value)


def test_missing_product_raises_schema_drift(raw_order: dict[str, Any]) -> None:
    del raw_order["product"]

    with pytest.raises(SchemaDriftError) as excinfo:
        parse_order(raw_order)

    assert excinfo.value.path == "product"


def test_nested_file_error_path_uses_raw_names(raw_order: dict[str, Any]) -> None:
    del raw_order["subproducts"][1]["downloads"][0]["download_struct"][0]["name"]

    with pytest.raises(SchemaDriftError) as excinfo:
        parse_order(raw_order)

    assert excinfo.value.path == "subproducts.1.downloads.0.download_struct.0.name"


def test_wrong_type_raises_schema_drift(raw_order: dict[str, Any]) -> None:
    raw_order["subproducts"] = "nope"

    with pytest.raises(SchemaDriftError) as excinfo:
        parse_order(raw_order)

    assert excinfo.value.path == "subproducts"


def test_schema_drift_error_is_chained_from_validation_error(
    raw_order: dict[str, Any],
) -> None:
    del raw_order["gamekey"]

    with pytest.raises(SchemaDriftError) as excinfo:
        parse_order(raw_order)

    assert excinfo.value.__cause__ is not None
    assert excinfo.value.path == "gamekey"


def test_schema_drift_error_empty_loc_gives_empty_path() -> None:
    with pytest.raises(SchemaDriftError) as excinfo:
        parse_order(cast(Any, []))

    assert excinfo.value.path == ""
