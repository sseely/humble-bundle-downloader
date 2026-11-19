"""Tests for humble_dl.lib.naming."""

import unicodedata

import pytest

from humble_dl.lib.naming import (
    MAX_COMPONENT_BYTES,
    filename_from_url,
    item_dir_names,
    sanitize_component,
    versioned_name,
)

MD5 = "abcdef1234567890"
EMPTY_FALLBACK = "_"

SANITIZE_SNAPSHOT: list[tuple[str, str]] = [
    ("Humble Book Bundle: Python by O'Reilly", "Humble Book Bundle - Python by O'Reilly"),
    ("Plain Name", "Plain Name"),
    ("AC/DC: The Book", "AC - DC - The Book"),
    ("a/b", "a - b"),
    ("Title:Subtitle", "Title - Subtitle"),
    ("  padded   name  ", "padded name"),
    ("tabs\tand\nnewlines", "tabs and newlines"),
    ("ctrl\x00char\x07s", "ctrlchars"),
    ("trailing dots...", "trailing dots"),
    ("trailing . . ", "trailing"),
    ("...", EMPTY_FALLBACK),
    ("", EMPTY_FALLBACK),
    ("   ", EMPTY_FALLBACK),
    ("\x00\x01", EMPTY_FALLBACK),
    ("Café", "Café"),
    ("Café", "Café"),
    ("日本語の本: 入門", "日本語の本 - 入門"),
    ("Learn Python 3.13", "Learn Python 3.13"),
    ("Why? Because *reasons*", "Why? Because *reasons*"),
    ("Ends with colon:", "Ends with colon -"),
    ("Hello World (2nd Edition)", "Hello World (2nd Edition)"),
]


@pytest.mark.parametrize(("raw", "expected"), SANITIZE_SNAPSHOT)
def test_sanitize_component_snapshot(raw: str, expected: str) -> None:
    assert sanitize_component(raw) == expected


def test_sanitize_truncates_multibyte_without_splitting() -> None:
    raw = "é" * 150 + ". "  # 300 + 2 bytes
    result = sanitize_component(raw)
    assert len(result.encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert result == "é" * 127  # 254 bytes; the 128th would reach 256
    assert not result.endswith((".", " "))


def test_sanitize_truncation_retrims_trailing_dot_and_space() -> None:
    raw = "a" * 254 + " b"
    result = sanitize_component(raw)
    assert result == "a" * 254


def test_sanitize_exact_limit_untouched() -> None:
    raw = "x" * MAX_COMPONENT_BYTES
    assert sanitize_component(raw) == raw


def test_sanitize_four_byte_characters() -> None:
    raw = "\U0001f4da" * 100  # 400 bytes
    result = sanitize_component(raw)
    assert result == "\U0001f4da" * 63  # 252 bytes
    assert unicodedata.is_normalized("NFC", result)


def test_item_dir_names_collision_gets_machine_suffix() -> None:
    items = [("Python", "py_a"), ("Python", "py_b")]
    assert item_dir_names(items) == {"py_a": "Python", "py_b": "Python-py_b"}


def test_item_dir_names_three_way_collision() -> None:
    items = [("Go", "g1"), ("Go", "g2"), ("Go", "g3")]
    assert item_dir_names(items) == {"g1": "Go", "g2": "Go-g2", "g3": "Go-g3"}


def test_item_dir_names_distinct_names_unchanged() -> None:
    items = [("Rust: The Book", "r1"), ("Zig", "z1")]
    assert item_dir_names(items) == {"r1": "Rust - The Book", "z1": "Zig"}


def test_item_dir_names_collision_after_sanitizing() -> None:
    items = [("A/B", "m1"), ("A - B", "m2")]
    assert item_dir_names(items) == {"m1": "A - B", "m2": "A - B-m2"}


def test_item_dir_names_case_insensitive_collision() -> None:
    items = [("Python", "a"), ("python", "b")]
    assert item_dir_names(items) == {"a": "Python", "b": "python-b"}


def test_item_dir_names_chained_collision_gets_counter() -> None:
    items = [("Python-b", "x"), ("Python", "a"), ("Python", "b")]
    assert item_dir_names(items) == {"x": "Python-b", "a": "Python", "b": "Python-b-2"}
    assert len({v.casefold() for v in item_dir_names(items).values()}) == 3


def test_item_dir_names_counter_keeps_incrementing() -> None:
    items = [("Go-b", "x"), ("go-b-2", "y"), ("Go", "a"), ("Go", "b")]
    assert item_dir_names(items)["b"] == "Go-b-3"


def test_item_dir_names_suffixed_names_respect_byte_cap() -> None:
    long_name = "é" * 200
    items = [(long_name, "m1"), (long_name, "m2")]
    result = item_dir_names(items)
    assert result["m2"].endswith("-m2")
    assert len(result["m2"].encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert result["m1"].casefold() != result["m2"].casefold()


def test_item_dir_names_huge_machine_name_stays_within_cap() -> None:
    machine = "m" * 400
    result = item_dir_names([("Same", "k"), ("Same", machine)])
    assert len(result[machine].encode("utf-8")) <= MAX_COMPONENT_BYTES
    assert result[machine].startswith("Same-mmm")


def test_item_dir_names_empty() -> None:
    assert item_dir_names([]) == {}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://dl.example.com/files/book.pdf?token=abc", "book.pdf"),
        ("https://dl.example.com/a/b/My%20Book.epub", "My Book.epub"),
        ("https://dl.example.com/a/Q%3AA.mobi", "Q - A.mobi"),
        ("https://dl.example.com/a/x%2Fy.pdf", "x - y.pdf"),
        ("https://dl.example.com/book.cbz#frag", "book.cbz"),
        ("https://dl.example.com/", EMPTY_FALLBACK),
    ],
)
def test_filename_from_url(url: str, expected: str) -> None:
    assert filename_from_url(url) == expected


def test_versioned_name_with_suffix() -> None:
    assert versioned_name("book.pdf", MD5) == "book.abcdef12.pdf"


def test_versioned_name_without_suffix() -> None:
    assert versioned_name("README", MD5) == "README.abcdef12"


def test_versioned_name_multi_dot_keeps_last_suffix_only() -> None:
    assert versioned_name("my.book.v2.epub", MD5) == "my.book.v2.abcdef12.epub"
