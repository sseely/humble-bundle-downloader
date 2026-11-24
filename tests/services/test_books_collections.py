"""Tests for planning Apple Books collections from Books library labels."""

from pathlib import Path

import pytest

from humble_dl.services import books_collections as bc
from tests.helpers.books import make_epub

BUNDLE = "Humble Comics Bundle - Example Comics"
OTHER = "Humble Book Bundle - Scalzi"
VOL1_FILE = "rickandmorty_vol1, Unknown Author, 1% finished"
VOL1_TITLE = "Rick and Morty Vol 1, Zac Gorman, 3% finished"
VOL2_FILE = "rickandmorty_vol2, Unknown Author, New"
REDSHIRTS = "Redshirts, John Scalzi, Finished book, In iCloud"
STRANGER = "Some Other Book, Anon, 1% finished"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (VOL1_FILE, "rickandmorty_vol1, Unknown Author"),
        (REDSHIRTS, "Redshirts, John Scalzi"),
        ("A, B, Finished, In iCloud", "A, B"),
        ("A, B, Sample", "A, B"),
        ("A, B, New", "A, B"),
        ("Plain", "Plain"),
    ],
)
def test_strip_progress(label: str, expected: str) -> None:
    assert bc.strip_progress(label) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (VOL1_FILE, True),
        ("lowercaseword, Anon, New", True),
        (VOL1_TITLE, False),
        ("Redshirts, John Scalzi", False),
    ],
)
def test_is_file_named(label: str, expected: bool) -> None:
    assert bc.is_file_named(label) is expected


def test_rekey_progress_merges_labels_whose_progress_changed() -> None:
    done = {
        "Redshirts, John Scalzi, 1% finished": [OTHER],
        REDSHIRTS: ["My Books"],
        "Redshirts, John Scalzi": [OTHER],
    }
    assert bc.rekey_progress(done) == {"Redshirts, John Scalzi": [OTHER, "My Books"]}


def _library(root: Path) -> Path:
    vol1 = root / BUNDLE / "Rick and Morty Vol. 1"
    vol1.mkdir(parents=True)
    (vol1 / "rickandmorty_vol1.pdf").write_text("x")
    (vol1 / "rickandmorty_vol1.1a2b3c4d.pdf").write_text("old")
    vol2 = root / BUNDLE / "Rick and Morty Vol. 2"
    vol2.mkdir()
    (vol2 / "rickandmorty_vol2.pdf").write_text("x")
    (vol2 / "notes.txt").write_text("x")
    make_epub(root / OTHER / "Redshirts" / "redshirts_johnscalzi.epub", "Redshirts")
    (root / OTHER / "Empty Item").mkdir()
    return root


def test_owner_index_keys_files_titles_and_single_item_titles(tmp_path: Path) -> None:
    index = bc.owner_index(_library(tmp_path))
    vol1 = (BUNDLE, "Rick and Morty Vol. 1")
    assert index["rickandmortyvol1"] == {vol1}
    assert index["redshirts"] == {(OTHER, "Redshirts")}
    assert "1a2b3c4d" not in "".join(index)
    assert not any(owner[1] == "Empty Item" for owners in index.values() for owner in owners)


def test_owner_index_of_missing_folder_is_empty(tmp_path: Path) -> None:
    assert bc.owner_index(tmp_path / "missing") == {}


def test_assign_labels_matches_longest_title_prefix(tmp_path: Path) -> None:
    index = bc.owner_index(_library(tmp_path))
    assigned, unmatched = bc.assign_labels([VOL1_FILE, REDSHIRTS, STRANGER], index)
    assert assigned == {
        VOL1_FILE: {(BUNDLE, "Rick and Morty Vol. 1")},
        REDSHIRTS: {(OTHER, "Redshirts")},
    }
    assert unmatched == [STRANGER]


def test_build_plan_skips_file_named_duplicates_and_files_unmatched() -> None:
    vol1: bc.Owner = (BUNDLE, "Rick and Morty Vol. 1")
    assigned: dict[str, set[bc.Owner]] = {
        VOL1_FILE: {vol1},
        VOL1_TITLE: {vol1},
        VOL2_FILE: {(BUNDLE, "Rick and Morty Vol. 2")},
        REDSHIRTS: {(OTHER, "Redshirts"), (BUNDLE, "Bonus Redshirts")},
    }
    labels = [VOL1_FILE, VOL1_TITLE, VOL2_FILE, REDSHIRTS, STRANGER, bc.NOT_A_BOOK]
    plan = bc.build_plan(labels, assigned)
    assert plan.duplicates == [VOL1_FILE]
    assert plan.collections == {
        VOL1_TITLE: [BUNDLE],
        VOL2_FILE: [BUNDLE],
        REDSHIRTS: [OTHER, BUNDLE],
        STRANGER: [bc.UNFILED_COLLECTION],
    }


def test_pending_ignores_progress_in_labels() -> None:
    collections = {VOL1_FILE: [BUNDLE, OTHER], VOL2_FILE: [BUNDLE]}
    done = {bc.strip_progress(VOL1_FILE): [BUNDLE]}
    assert bc.pending(collections, done) == {VOL1_FILE: [OTHER], VOL2_FILE: [BUNDLE]}


def test_collection_counts() -> None:
    plan = bc.CollectionsPlan(collections={"a": [BUNDLE, OTHER], "b": [BUNDLE]}, duplicates=[])
    assert plan.counts() == [(BUNDLE, 2), (OTHER, 1)]


def test_is_cloud_only_reads_the_in_icloud_suffix() -> None:
    assert bc.is_cloud_only("theboys_vol11_x, Unknown Author, 1% finished, In iCloud")
    assert not bc.is_cloud_only("theboys_vol8_x, Unknown Author, Finished book")
    assert not bc.is_cloud_only("In iCloud Stories, Some Author, New")


def test_duplicate_keepers_names_the_titled_copy_kept() -> None:
    owner: bc.Owner = ("Bundle", "Old Man's War")
    other: bc.Owner = ("Bundle", "Redshirts")
    old = "old_mans_war_-_john_scalzi, John Scalzi, 1% finished"
    assigned: dict[str, set[bc.Owner]] = {
        old: {owner},
        "Old Man's War, John Scalzi, 12% finished": {owner},
        "redshirts_scalzi, Unknown Author, New": {other},
        "theboys_vol1, Unknown Author, New": {("B", "The Boys Vol. 1")},
    }
    assert bc.duplicate_keepers(assigned) == {
        old: "Old Man's War, John Scalzi, 12% finished",
    }
