"""Tests for humble_dl.services.book_info: renames and writer matching (pure)."""

from pathlib import Path

import pytest

from humble_dl.services.book_info import (
    OVERRIDES_NOTE,
    SKIP_NO_ITEM,
    SOURCE_COMICVINE,
    SOURCE_NONE,
    SOURCE_OPENLIBRARY,
    SOURCE_OVERRIDE,
    UNKNOWN_AUTHOR,
    CvCredit,
    CvIssue,
    CvVolume,
    Found,
    OlDoc,
    Rename,
    SeriesQuery,
    apply_overrides,
    author_text,
    book_entry,
    bundle_publisher,
    choose_volume,
    find_writer,
    item_stems,
    label_parts,
    move_progress,
    new_key,
    pick_open_library,
    rank_volumes,
    removal_targets,
    rename_plan,
    rename_targets,
    series_query,
    writers,
)

ONI_BUNDLE = "Humble Comics Bundle - Example Comics by Oni Press"
DYN_BUNDLE = "Humble Comics Bundle - Example Heroines By Dynamite"
PRESENTS_DIR = "Rick and Morty Presents Vol. 5"
PRESENTS = "Rick and Morty Presents Vol. 5"
PRESENTS_LABEL = "rickandmortypresents_vol5, Unknown Author, 1% finished"
VOL1_LABEL = "rickandmorty_vol1, Unknown Author, 1% finished"
VOL1_DIR = "Rick and Morty Vol. 1"
POOPY_DIR = "Rick and Morty Vol. 1 - Lil' Poopy Superstar"
TITLED_LABEL = "Redshirts, John Scalzi, 3% finished"
ORPHAN_LABEL = "RedSonjaVol1-04HQ_1418163538, Unknown Author, 1% finished"
DUP_LABEL = "theboys_vol1_thenameofthegame, Unknown Author, 1% finished"


# --- labels -------------------------------------------------------------------


def test_label_parts_splits_title_and_author_without_status() -> None:
    assert label_parts(PRESENTS_LABEL) == ("rickandmortypresents_vol5", UNKNOWN_AUTHOR)
    assert label_parts("x_y, A, B, In iCloud") == ("x_y", "A, B, In iCloud")
    assert label_parts("x_y, A, B, 4% finished, In iCloud") == ("x_y", "A, B")


def test_new_key_matches_what_books_will_show() -> None:
    assert new_key(PRESENTS, "Ivan Cohen") == "Rick and Morty Presents Vol. 5, Ivan Cohen"


def test_move_progress_rekeys_and_merges() -> None:
    done = {"old_name, Unknown Author": ["A"], "New, W": ["B"], "Other, X": ["C"]}
    moved = move_progress(done, "old_name, Unknown Author", "New, W")
    assert moved == {"New, W": ["B", "A"], "Other, X": ["C"]}
    assert move_progress(done, "absent", "New, W") == done


# --- rename plan --------------------------------------------------------------


def owners(*items: str, bundle: str = ONI_BUNDLE) -> set[tuple[str, str]]:
    return {(bundle, item) for item in items}


TITLES = {
    f"{ONI_BUNDLE}/{PRESENTS_DIR}": PRESENTS,
    f"{ONI_BUNDLE}/{VOL1_DIR}": "Rick and Morty Vol. 1",
    f"{ONI_BUNDLE}/{POOPY_DIR}": "Rick and Morty Vol. 1: Lil' Poopy Superstar",
}


def test_rename_plan_renames_file_named_labels_only() -> None:
    labels = [PRESENTS_LABEL, TITLED_LABEL, ORPHAN_LABEL, DUP_LABEL]
    assigned = {PRESENTS_LABEL: owners(PRESENTS_DIR), TITLED_LABEL: owners("Redshirts")}
    renames, skipped = rename_plan(labels, assigned, TITLES, {DUP_LABEL}, {})
    assert renames == [Rename(PRESENTS_LABEL, (ONI_BUNDLE, PRESENTS_DIR), PRESENTS, UNKNOWN_AUTHOR)]
    assert skipped == {ORPHAN_LABEL: "no matching Humble item"}


def test_rename_plan_picks_the_item_holding_the_file_of_that_name() -> None:
    assigned = {VOL1_LABEL: owners(VOL1_DIR, POOPY_DIR)}
    stems = {
        (ONI_BUNDLE, VOL1_DIR): {"rickandmorty_vol1"},
        (ONI_BUNDLE, POOPY_DIR): {"rickandmorty_vol1_lilpoopysuperstar"},
    }
    renames, skipped = rename_plan([VOL1_LABEL], assigned, TITLES, set(), stems)
    assert [r.title for r in renames] == ["Rick and Morty Vol. 1"]
    assert skipped == {}


def test_rename_plan_skips_ambiguous_and_untitled_items() -> None:
    assigned = {VOL1_LABEL: owners(VOL1_DIR, POOPY_DIR), PRESENTS_LABEL: owners("Gone")}
    renames, skipped = rename_plan([VOL1_LABEL, PRESENTS_LABEL], assigned, TITLES, set(), {})
    assert renames == []
    assert skipped == {
        VOL1_LABEL: "matches several items: Rick and Morty Vol. 1 | "
        "Rick and Morty Vol. 1: Lil' Poopy Superstar",
        PRESENTS_LABEL: "no saved Humble title; run `humble-dl titles`",
    }


def test_rename_plan_tidies_whitespace_in_humble_titles() -> None:
    titles = {f"{ONI_BUNDLE}/{PRESENTS_DIR}": "  Rick and Morty  Presents Vol. 5 "}
    assigned = {PRESENTS_LABEL: owners(PRESENTS_DIR)}
    renames, _ = rename_plan([PRESENTS_LABEL], assigned, titles, set(), {})
    assert [r.title for r in renames] == [PRESENTS]


def test_rename_plan_same_title_in_two_bundles_is_one_rename() -> None:
    both = owners(PRESENTS_DIR) | owners(PRESENTS_DIR, bundle="Other Bundle")
    titles = {**TITLES, f"Other Bundle/{PRESENTS_DIR}": PRESENTS}
    renames, _ = rename_plan([PRESENTS_LABEL], {PRESENTS_LABEL: both}, titles, set(), {})
    assert [(r.owner, r.title) for r in renames] == [((ONI_BUNDLE, PRESENTS_DIR), PRESENTS)]


# --- publisher and series -----------------------------------------------------


@pytest.mark.parametrize(
    ("bundle", "publisher"),
    [
        (ONI_BUNDLE, "Oni Press"),
        (DYN_BUNDLE, "Dynamite"),
        ("Humble Book Bundle - Example Science presented by No Starch Press", "No Starch Press"),
        ("Humble Book Bundle - Example Author", None),
    ],
)
def test_bundle_publisher(bundle: str, publisher: str | None) -> None:
    assert bundle_publisher(bundle) == publisher


@pytest.mark.parametrize(
    ("title", "query"),
    [
        (PRESENTS, SeriesQuery("Rick and Morty Presents", 5, None, collected=True)),
        ("The Boys Vol. 9: The Big Ride", SeriesQuery("The Boys", 9, None, collected=True)),
        ("Pantha #2", SeriesQuery("Pantha", 2, None)),
        ("Rick and Morty: Worlds Apart", SeriesQuery("Rick and Morty: Worlds Apart", None, None)),
        ("Vampirella (2011) Volume 1 Issue #06", SeriesQuery("Vampirella", 6, 2011)),
        (
            "Hack/Slash/Eva: Monster's Ball #1",
            SeriesQuery("Hack/Slash/Eva: Monster's Ball", 1, None),
        ),
        ("Legenderry 1", SeriesQuery("Legenderry", 1, None)),
        ("Vampirella Masters, Vol. 4", SeriesQuery("Vampirella Masters", 4, None, collected=True)),
        (
            "Rick and Morty: The Manga Vol. 1: Get in the Robot, Morty!",
            SeriesQuery("Rick and Morty: The Manga", 1, None, collected=True),
        ),
        (
            "Rick and Morty (2023) Vol. 2: The Space Shake Saga Part 2",
            SeriesQuery("Rick and Morty", 2, 2023, collected=True),
        ),
    ],
)
def test_series_query(title: str, query: SeriesQuery) -> None:
    assert series_query(title) == query


# --- Comic Vine volume choice -------------------------------------------------


def vol(
    vid: int,
    name: str,
    start: int | None,
    publisher: str | None = "Dynamite Entertainment",
    issues: int = 10,
) -> CvVolume:
    return CvVolume(id=vid, name=name, publisher=publisher, start_year=start, issues=issues)


PANTHA_2022 = vol(1, "Pantha", 2022, issues=5)
PANTHA_2012 = vol(2, "Pantha", 2012, issues=6)
BEST_OF = vol(3, "The Best of Pantha: The Warren Years", 2014)
ONI_PANTHA = vol(4, "Pantha", 2013, publisher="Oni Press")


def test_choose_volume_prefers_latest_series_started_by_the_file_year() -> None:
    candidates = [PANTHA_2022, BEST_OF, PANTHA_2012, ONI_PANTHA]
    query = SeriesQuery("Pantha", 2, None)
    assert choose_volume(candidates, query, "Dynamite", ref_year=2015) == PANTHA_2012
    assert choose_volume(candidates, query, "Dynamite", ref_year=2023) == PANTHA_2022


def test_choose_volume_without_year_takes_earliest_and_checks_issue_count() -> None:
    query = SeriesQuery("Pantha", 6, None)
    assert choose_volume([PANTHA_2022, PANTHA_2012], query, None, ref_year=None) == PANTHA_2012
    assert choose_volume([PANTHA_2022], query, None, ref_year=None) is None


def test_choose_volume_uses_the_title_year_and_ignores_unknown_publishers() -> None:
    query = SeriesQuery("Pantha", 1, 2022)
    no_pub = vol(5, "Pantha", 2012, publisher=None)
    assert choose_volume([PANTHA_2012, PANTHA_2022], query, "Dynamite", 2015) == PANTHA_2022
    assert choose_volume([no_pub], SeriesQuery("Pantha", 1, None), "Dynamite", None) is None
    assert choose_volume([no_pub], SeriesQuery("Pantha", 1, None), None, None) == no_pub


MONTHLY = vol(10, "Rick and Morty", 2015, publisher="Oni Press", issues=60)
TRADES = vol(11, "Rick and Morty", 2015, publisher="Oni Press", issues=12)
HARDCOVERS = vol(12, "Rick and Morty", 2016, publisher="Oni Press", issues=8)
NEW_MONTHLY = vol(13, "Rick and Morty", 2023, publisher="Oni Press", issues=12)


def test_choose_volume_for_a_collected_volume_takes_the_smallest_series() -> None:
    candidates = [MONTHLY, TRADES, HARDCOVERS, NEW_MONTHLY]
    vol9 = SeriesQuery("Rick and Morty", 9, None, collected=True)
    assert choose_volume(candidates, vol9, "Oni Press", ref_year=2019) == TRADES
    assert choose_volume(candidates, vol9, "Oni Press", ref_year=None) == TRADES
    vol5 = SeriesQuery("Rick and Morty", 5, None, collected=True)
    assert choose_volume(candidates, vol5, "Oni Press", ref_year=2019) == HARDCOVERS
    issue9 = SeriesQuery("Rick and Morty", 9, None)
    assert choose_volume(candidates, issue9, "Oni Press", ref_year=2019) in (MONTHLY, TRADES)


def test_choose_volume_treats_a_title_year_as_an_upper_bound() -> None:
    dyn2010 = vol(20, "Vampirella", 2010, issues=38)
    dyn2016 = vol(21, "Vampirella", 2016, issues=12)
    query = SeriesQuery("Vampirella", 6, 2011)
    assert choose_volume([dyn2016, dyn2010], query, "Dynamite", ref_year=2017) == dyn2010


def test_choose_volume_requires_the_exact_series_name() -> None:
    query = SeriesQuery("Pantha", 1, None)
    assert choose_volume([BEST_OF], query, "Dynamite", None) is None


# --- writers ------------------------------------------------------------------


def test_writers_keeps_writer_roles_in_order() -> None:
    credits = [
        CvCredit("Crank!", "letterer"),
        CvCredit("Josh Trujillo", "writer"),
        CvCredit("Jarrett Williams", "artist, cover"),
        CvCredit("Kyle Starks", "writer, cover"),
        CvCredit("Josh Trujillo", "writer"),
    ]
    assert writers(credits) == ["Josh Trujillo", "Kyle Starks"]


def test_author_text_joins_up_to_three() -> None:
    assert author_text([]) is None
    assert author_text(["A"]) == "A"
    assert author_text(["A", "B"]) == "A & B"
    assert author_text(["A", "B", "C", "D"]) == "A, B & C"


# --- Open Library fallback ----------------------------------------------------


def test_pick_open_library_needs_exact_title_and_publisher() -> None:
    docs = [
        OlDoc("Rick and Morty Presents Vol. 4", ["Wrong"], ["Oni Press, Incorporated"]),
        OlDoc("Rick and Morty Presents Vol. 5", ["Ivan Cohen", "Puste"], ["Other Press"]),
        OlDoc("Rick and Morty Presents: Vol. 5", ["Ivan Cohen", "Puste"], ["Oni Press, Inc."]),
    ]
    assert pick_open_library(docs, PRESENTS, "Oni Press") == ["Ivan Cohen"]
    assert pick_open_library(docs, PRESENTS, None) == ["Ivan Cohen"]
    assert pick_open_library(docs[:1], PRESENTS, None) == []
    assert pick_open_library([OlDoc(PRESENTS, [], ["Oni Press"])], PRESENTS, None) == []


def test_item_stems_lists_visible_file_stems(tmp_path: Path) -> None:
    item = tmp_path / ONI_BUNDLE / VOL1_DIR
    item.mkdir(parents=True)
    (item / "rickandmorty_vol1.pdf").write_bytes(b"")
    (item / "rickandmorty_vol1.cbz").write_bytes(b"")
    (item / ".DS_Store").write_bytes(b"")
    owner = (ONI_BUNDLE, VOL1_DIR)
    missing = (ONI_BUNDLE, "Gone")
    assert item_stems(tmp_path, [owner, missing]) == {owner: {"rickandmorty_vol1"}, missing: set()}


def test_rename_plan_adds_author_only_entries_for_unknown_authors() -> None:
    legenderry = "Legenderry 1, Unknown Author, 1% finished"
    loose = "A Loose Book, Unknown Author, New"
    known = "Redshirts, John Scalzi, 3% finished"
    assigned = {legenderry: owners("Legenderry 1", bundle=DYN_BUNDLE)}
    labels = [legenderry, loose, known, "Uploaded to iCloud"]
    renames, skipped = rename_plan(labels, assigned, {}, set(), {})
    assert renames == [
        Rename(legenderry, (DYN_BUNDLE, "Legenderry 1"), "Legenderry 1", UNKNOWN_AUTHOR),
        Rename(loose, None, "A Loose Book", UNKNOWN_AUTHOR),
    ]
    assert skipped == {}


# --- find_writer ----------------------------------------------------------------


class FakeCv:
    def __init__(
        self,
        vols: list[CvVolume],
        issues: dict[int, CvIssue] | CvIssue | None,
        credits: list[CvCredit],
    ) -> None:
        self.vols, self.credit_list = vols, credits
        self.issues = issues
        self.asked: list[tuple[str, object]] = []

    def volumes(self, series: str) -> list[CvVolume]:
        self.asked.append(("volumes", series))
        return self.vols

    def issue(self, volume_id: int, number: int) -> CvIssue | None:
        self.asked.append(("issue", (volume_id, number)))
        if isinstance(self.issues, dict):
            return self.issues.get(volume_id)
        return self.issues

    def credits(self, issue_id: int) -> list[CvCredit]:
        self.asked.append(("credits", issue_id))
        return self.credit_list


class FakeOl:
    def __init__(self, docs: list[OlDoc]) -> None:
        self.docs = docs
        self.asked: list[str] = []

    def search(self, title: str) -> list[OlDoc]:
        self.asked.append(title)
        return self.docs


SNIEGOSKI = [CvCredit("Igor Lima", "artist"), CvCredit("Tom Sniegoski", "writer")]
OL_PANTHA = [OlDoc("Pantha #2", ["Someone"], ["Dynamite"])]


def test_find_writer_uses_comic_vine_credits() -> None:
    cv = FakeCv([PANTHA_2022, PANTHA_2012], CvIssue(906228, None), SNIEGOSKI)
    found = find_writer("Pantha #2", "Dynamite", 2015, cv, FakeOl(OL_PANTHA))
    assert found == Found("Tom Sniegoski", SOURCE_COMICVINE, "Pantha (2012) #2")
    assert cv.asked == [("volumes", "Pantha"), ("issue", (2, 2)), ("credits", 906228)]


def test_find_writer_for_a_volume_takes_the_series_naming_issues_volume() -> None:
    issues = {HARDCOVERS.id: CvIssue(81, "Book Five"), TRADES.id: CvIssue(82, "Volume Five")}
    cv = FakeCv([MONTHLY, TRADES, HARDCOVERS], issues, [CvCredit("Kyle Starks", "writer")])
    found = find_writer("Rick and Morty Vol. 5", "Oni Press", 2019, cv, None)
    assert found == Found("Kyle Starks", SOURCE_COMICVINE, "Rick and Morty (2015) #5")
    assert ("credits", 82) in cv.asked


def test_find_writer_for_a_volume_falls_back_to_the_best_ranked_issue() -> None:
    issues = {HARDCOVERS.id: CvIssue(81, "Book Five"), TRADES.id: CvIssue(82, None)}
    cv = FakeCv([TRADES, HARDCOVERS], issues, [CvCredit("A", "writer")])
    found = find_writer("Rick and Morty Vol. 5", "Oni Press", 2019, cv, None)
    assert found.note == "Rick and Morty (2016) #5"


def test_rank_volumes_orders_every_fitting_series() -> None:
    vol5 = SeriesQuery("Rick and Morty", 5, None, collected=True)
    ranked = rank_volumes([MONTHLY, TRADES, HARDCOVERS, NEW_MONTHLY], vol5, "Oni Press", 2019)
    assert ranked == [HARDCOVERS, TRADES, MONTHLY, NEW_MONTHLY]


def test_find_writer_asks_for_issue_one_when_the_title_has_no_number() -> None:
    worlds = vol(9, "Rick and Morty: Worlds Apart", 2021, publisher="Oni Press", issues=4)
    cv = FakeCv([worlds], CvIssue(5, None), [CvCredit("Josh Trujillo", "writer")])
    found = find_writer("Rick and Morty: Worlds Apart", "Oni Press", None, cv, None)
    assert found.author == "Josh Trujillo"
    assert ("issue", (9, 1)) in cv.asked


def test_find_writer_falls_back_to_open_library() -> None:
    for cv in (
        FakeCv([], None, []),
        FakeCv([PANTHA_2012], None, []),
        FakeCv([PANTHA_2012], CvIssue(1, None), []),
    ):
        ol = FakeOl(OL_PANTHA)
        found = find_writer("Pantha #2", "Dynamite", None, cv, ol)
        assert found == Found("Someone", SOURCE_OPENLIBRARY, "first listed contributor")
        assert ol.asked == ["Pantha #2"]


def test_find_writer_skips_open_library_without_a_publisher() -> None:
    ol = FakeOl([OlDoc("Untitled Notes", ["A. Writer"], ["Self"])])
    found = find_writer("Untitled Notes", None, None, None, ol)
    assert found == Found(None, SOURCE_NONE, "no match")
    assert ol.asked == []


def test_find_writer_without_sources_or_matches_is_none() -> None:
    assert find_writer("Pantha #2", None, None, None, None) == Found(None, SOURCE_NONE, "no match")
    assert find_writer("Pantha #2", "Dynamite", None, None, FakeOl([])).source == SOURCE_NONE


# --- review records -------------------------------------------------------------


def test_book_entry_records_changes_for_review() -> None:
    rename = Rename(PRESENTS_LABEL, (ONI_BUNDLE, PRESENTS_DIR), PRESENTS, UNKNOWN_AUTHOR)
    found = Found("Ivan Cohen", SOURCE_COMICVINE, "Rick and Morty Presents (2019) #5")
    assert book_entry(rename, found) == {
        "title": PRESENTS,
        "author": "Ivan Cohen",
        "source": SOURCE_COMICVINE,
        "note": "Rick and Morty Presents (2019) #5",
        "item": f"{ONI_BUNDLE}/{PRESENTS_DIR}",
    }


def test_book_entry_keeps_a_retitle_without_author_and_drops_no_ops() -> None:
    nothing = Found(None, SOURCE_NONE, "no match")
    retitle = Rename(PRESENTS_LABEL, (ONI_BUNDLE, PRESENTS_DIR), PRESENTS, UNKNOWN_AUTHOR)
    entry = book_entry(retitle, nothing)
    assert entry is not None
    assert (entry["title"], entry["author"]) == (PRESENTS, None)
    same = Rename("Legenderry 1, Unknown Author", None, "Legenderry 1", UNKNOWN_AUTHOR)
    assert book_entry(same, nothing) is None


def test_rename_targets_skip_done_entries_and_keep_known_authors() -> None:
    entries: dict[str, dict[str, str | None]] = {
        PRESENTS_LABEL: {"title": PRESENTS, "author": "Ivan Cohen"},
        VOL1_LABEL: {"title": "Rick and Morty Vol. 1", "author": None},
        "pantha_issue2, Unknown Author, 1% finished": {"title": "Pantha #2", "author": "T"},
    }
    renamed = {"pantha_issue2, Unknown Author": "Pantha #2, T"}
    assert rename_targets(entries, renamed) == {
        "rickandmortypresents_vol5, Unknown Author": (PRESENTS, "Ivan Cohen"),
        "rickandmorty_vol1, Unknown Author": ("Rick and Morty Vol. 1", UNKNOWN_AUTHOR),
    }


# --- overrides ------------------------------------------------------------------

SONJA_HQ = "RedSonjaVol1-02HQ_1418163538, Unknown Author, 1% finished"
SONJA_LQ = "RedSonjaVol1-02_1418163538, Unknown Author, 1% finished, In iCloud"


def test_apply_overrides_titles_skipped_entries_and_marks_duplicates() -> None:
    overrides: dict[str, dict[str, str | None]] = {
        "RedSonjaVol1-02HQ_1418163538, Unknown Author": {
            "title": "Red Sonja (2005) #2",
            "author": "Michael Avon Oeming & Mike Carey",
        },
        "RedSonjaVol1-02_1418163538, Unknown Author": {
            "duplicate_of": "RedSonjaVol1-02HQ_1418163538"
        },
        "rickandmortypresents_vol5, Unknown Author": {"title": None, "author": "Ivan Cohen"},
        "not in the library, X": {"title": "Nope"},
    }
    entries: dict[str, dict[str, str | None]] = {
        PRESENTS_LABEL: {"title": PRESENTS, "author": "A, B & C", "source": "comicvine"}
    }
    skipped = {SONJA_HQ: SKIP_NO_ITEM, SONJA_LQ: SKIP_NO_ITEM, ORPHAN_LABEL: SKIP_NO_ITEM}
    labels = [PRESENTS_LABEL, SONJA_HQ, SONJA_LQ, ORPHAN_LABEL]
    merged, still_skipped, duplicates = apply_overrides(entries, skipped, labels, overrides)
    assert merged == {
        PRESENTS_LABEL: {
            "title": PRESENTS,
            "author": "Ivan Cohen",
            "source": SOURCE_OVERRIDE,
            "note": OVERRIDES_NOTE,
            "item": None,
        },
        SONJA_HQ: {
            "title": "Red Sonja (2005) #2",
            "author": "Michael Avon Oeming & Mike Carey",
            "source": SOURCE_OVERRIDE,
            "note": OVERRIDES_NOTE,
            "item": None,
        },
    }
    assert still_skipped == {ORPHAN_LABEL: SKIP_NO_ITEM}
    assert duplicates == {SONJA_LQ: "RedSonjaVol1-02HQ_1418163538"}
    assert entries[PRESENTS_LABEL]["source"] == "comicvine"


def test_removal_targets_key_by_label_without_status_and_skip_done() -> None:
    remove = {
        "purgatori_issue3, Unknown Author, 1% finished": "Purgatori #3, Aaron Gillespie",
        "nyx_issue4, Unknown Author, New, In iCloud": "Nyx, Volume 1, #4",
    }
    assert removal_targets(remove, ["nyx_issue4, Unknown Author"]) == {
        "purgatori_issue3, Unknown Author": "Purgatori #3, Aaron Gillespie"
    }
