"""Tests for Books library keys, cross-bundle dedupe and the duplicates report."""

from pathlib import Path

import pytest

from humble_dl.services import books_library as lib
from tests.helpers.books import make_epub, make_library

REAL_LIBRARY_DIRS = [
    "Tress of the Emerald Sea.epub",
    "Tress of the Emerald Sea 1.epub",
    "Tress of the Emerald Sea 2.epub",
    "Masquerade 1.epub",
    "Masquerade 3.epub",
    "Chastity Volume 1 2.epub",
    "Shadows for Silence in the Forests of Hell.epub",
    "Vampirella Archives Vol 1.epub",
]
REAL_LIBRARY_FILES = ["shadowsforsilenceintheforestsofhell.pdf", "vampirellaarchives_vol1.pdf"]


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Old Man's War", "oldmanswar"), ("Tiny Lego® Wonders", "tinylegowonders"), ("--", "")],
)
def test_norm(text: str, expected: str) -> None:
    assert lib.norm(text) == expected


def test_default_library_dir_uses_current_home(isolated_home: Path) -> None:
    expected = isolated_home / "Library/Mobile Documents/iCloud~com~apple~iBooks/Documents"
    assert lib.default_library_dir() == expected


def test_entry_keys_strip_collision_suffix_only_for_long_digits() -> None:
    assert lib.entry_key("foo_1418163538.pdf") == "foo"
    assert lib.entry_key("x_12345678.pdf") == "x12345678"
    assert lib.entry_key("Old Mans War.epub") == "oldmanswar"


def test_library_keys_ignore_hidden_and_other_entries() -> None:
    keys = lib.library_keys(["A b.epub", ".hid.epub", "n.txt", "c_1403628954.pdf", "---.pdf"])
    assert keys == {"ab", "c"}


def test_book_keys_include_title_for_readable_epub(tmp_path: Path) -> None:
    path = make_epub(tmp_path / "redshirts_johnscalzi.epub", "Redshirts")
    assert lib.book_keys(path, include_item=False) == {"redshirtsjohnscalzi", "redshirts"}


def test_book_keys_fall_back_to_filename_when_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "bad_book.epub"
    path.write_text("not a zip")
    assert lib.book_keys(path, include_item=False) == {"badbook"}


def test_book_keys_pdf_uses_filename_only(tmp_path: Path) -> None:
    path = tmp_path / "Foo Bar.pdf"
    path.write_text("x")
    assert lib.book_keys(path, include_item=False) == {"foobar"}


def test_dedupe_shares_any_key_and_prefers_epub_then_path(tmp_path: Path) -> None:
    a_epub = tmp_path / "B2" / "a.epub"
    a_pdf = tmp_path / "B1" / "A.pdf"
    b_epub = tmp_path / "B1" / "old_mans_war.epub"
    c_epub = tmp_path / "B0" / "oldmanswar_johnscalzi.epub"
    keys = {
        a_epub: frozenset({"a"}),
        a_pdf: frozenset({"a"}),
        b_epub: frozenset({"oldmanswar"}),
        c_epub: frozenset({"oldmanswarjohnscalzi", "oldmanswar"}),
    }
    kept, dropped = lib.dedupe(list(keys), keys)
    assert kept == [c_epub, a_epub]
    assert dropped == sorted([a_pdf, b_epub])


def test_partition_skips_in_books_keeps_rest(tmp_path: Path) -> None:
    a, b, c = (tmp_path / n for n in ("a.epub", "b.epub", "c.epub"))
    keys = {a: frozenset({"a", "titlea"}), b: frozenset({"b"}), c: frozenset({"c"})}
    remaining, dups, in_books = lib.partition([a, b, c], keys, {"titlea", "zzz"})
    assert (remaining, dups, in_books) == ([b, c], [], [a])


def test_missing_library_is_empty() -> None:
    assert lib.read_library_names(Path("/nonexistent-books-library")) == []


def test_duplicate_groups_real_library_names(tmp_path: Path) -> None:
    library = make_library(tmp_path / "lib", REAL_LIBRARY_FILES, REAL_LIBRARY_DIRS)
    groups = lib.duplicate_groups(lib.read_library_names(library))
    assert groups == {
        "shadowsforsilenceintheforestsofhell": [
            "Shadows for Silence in the Forests of Hell.epub",
            "shadowsforsilenceintheforestsofhell.pdf",
        ],
        "tressoftheemeraldsea": [
            "Tress of the Emerald Sea 1.epub",
            "Tress of the Emerald Sea 2.epub",
            "Tress of the Emerald Sea.epub",
        ],
        "vampirellaarchivesvol1": [
            "Vampirella Archives Vol 1.epub",
            "vampirellaarchives_vol1.pdf",
        ],
    }


def test_duplicates_report_lines(tmp_path: Path) -> None:
    library = make_library(tmp_path / "lib", REAL_LIBRARY_FILES, REAL_LIBRARY_DIRS)
    lines = lib.library_duplicates_report(library)
    assert len(lines) == 4
    assert lines[1] == (
        "Tress of the Emerald Sea 1.epub | Tress of the Emerald Sea 2.epub | "
        "Tress of the Emerald Sea.epub"
    )
    assert lines[-1] == "3 book(s) with more than one copy in Books."


def test_duplicates_report_missing_library_defaults_to_home() -> None:
    assert lib.library_duplicates_report() == ("0 book(s) with more than one copy in Books.",)


@pytest.mark.parametrize(
    ("name", "key"),
    [
        # Live 2026-11-19: Books holds HackSlashEva01HQ_1418163538.pdf; Humble sells
        # HackSlashEva01_1418163538.epub. Same comic (editions do not matter).
        ("HackSlashEva01HQ_1418163538.pdf", "hackslasheva01"),
        ("HackSlashEva01_1418163538.epub", "hackslasheva01"),
        ("THE_ALLOY_OF_LAW_EPUB.epub", "thealloyoflaw"),
        ("Tress_of_the_Emerald_Sea_for_Kindle.epub", "tressoftheemeraldsea"),
        ("Tress_of_the_Emerald_Sea_for_Kobo.kepub.epub", "tressoftheemeraldsea"),
        ("The Frugal Wizard - for Kindle.epub", "thefrugalwizard"),
        ("smbc_-_the_most_dangerous_game-high.pdf", "smbcthemostdangerousgame"),
        ("xkcd-volume0-low.pdf", "xkcdvolume0"),
        ("Highlow.pdf", "highlow"),
    ],
)
def test_filename_keys_strip_variant_tags_symmetrically(name: str, key: str) -> None:
    assert lib.entry_key(name) == key
    assert lib.book_keys(Path("B") / "Item" / name, include_item=False) == {key}


def test_book_keys_add_item_title_when_requested(tmp_path: Path) -> None:
    # Live 2026-11-19: Humble ships only athena_issue1.pdf for item "Athena #1";
    # Books holds "Athena 1.epub". The item folder carries Humble's title.
    path = tmp_path / "Bundle" / "Athena #1" / "athena_issue1.pdf"
    path.parent.mkdir(parents=True)
    path.write_text("x")
    assert lib.book_keys(path, include_item=True) == {"athenaissue1", "athena1"}
    assert lib.book_keys(path, include_item=False) == {"athenaissue1"}


def test_item_title_key_only_for_single_file_items(tmp_path: Path) -> None:
    item = tmp_path / "Bundle" / "Collected Works"
    item.mkdir(parents=True)
    one, two = item / "vol1.pdf", item / "vol2.pdf"
    solo = tmp_path / "Bundle" / "Athena #1" / "athena_issue1.pdf"
    solo.parent.mkdir()
    for p in (one, two, solo):
        p.write_text("x")

    keys = lib.keys_for_selection([one, two, solo])

    assert keys[one] == {"vol1"} and keys[two] == {"vol2"}
    assert keys[solo] == {"athenaissue1", "athena1"}


@pytest.mark.parametrize(
    ("item", "expected_extra"),
    [
        # Books holds "The Boys Vol 2.epub"; Humble's item is "The Boys Vol. 2 - Get Some".
        ("The Boys Vol. 2 - Get Some", {"theboysvol2getsome", "theboysvol2"}),
        # No number before the dash: too generic, never a key on its own.
        ("Red Sonja - She-Devil with a Sword #2", {"redsonjashedevilwithasword2"}),
        ("Athena #1", {"athena1"}),
    ],
)
def test_item_title_keys_add_numbered_series_prefix(
    tmp_path: Path, item: str, expected_extra: set[str]
) -> None:
    path = tmp_path / "Bundle" / item / "file.pdf"
    assert lib.book_keys(path, include_item=True) == {"file"} | expected_extra
