"""Tests for the pure download planner."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from humble_dl.lib.hashcache import LocalFile
from humble_dl.models.library import (
    Download,
    FileVersion,
    Order,
    Subproduct,
    parse_order,
)
from humble_dl.services.planner import (
    Action,
    ArchiveThenFetch,
    Fetch,
    FileKey,
    Plan,
    Skip,
    plan,
)

ROOT = Path("/root")
FIXTURE = Path(__file__).parents[1] / "helpers" / "fixtures" / "legacy_order_synthetic.json"
URL_BASE = "https://dl.example.test/files/"
MD5_A = "a" * 32
MD5_B = "b" * 32
MD5_DEAD = "deadbeef" + "0" * 24
SIZE = 100
BUNDLE = "Bundle"
ITEM = "Book"
BOOK_DIR = ROOT / BUNDLE / ITEM

type LocalMap = dict[Path, LocalFile]


def url(name: str) -> str:
    return f"{URL_BASE}{name}?token=secret"


def fv(name: str, md5: str | None = MD5_A, size: int | None = SIZE) -> FileVersion:
    return FileVersion(label=name, url=url(name), md5=md5, file_size=size)


def sub(machine: str, files: list[FileVersion], human: str = ITEM) -> Subproduct:
    return Subproduct(
        machine_name=machine,
        human_name=human,
        downloads=[Download(platform="ebook", files=files)],
    )


def order(subs: list[Subproduct], gamekey: str = "gk1", human: str = BUNDLE) -> Order:
    return Order(gamekey=gamekey, human_name=human, subproducts=subs)


def fake_local(files: LocalMap | None = None) -> Callable[[Path], LocalFile | None]:
    table = files or {}
    return table.get


def run(orders: list[Order], files: LocalMap | None = None) -> Plan:
    return plan(orders, ROOT, fake_local(files))


def written_paths(result: Plan) -> list[Path]:
    paths: list[Path] = []
    for action in result.actions:
        if isinstance(action, Fetch):
            paths.append(action.path)
        elif isinstance(action, ArchiveThenFetch):
            paths.extend([action.archive_to, action.fetch.path])
    return paths


def test_empty_orders_yield_empty_plan() -> None:
    assert run([]) == Plan(actions=[], bytes_to_fetch=0)


def test_empty_disk_fetches_with_full_details() -> None:
    result = run([order([sub("b1", [fv("book.pdf")])])])
    expected = Fetch(
        key=FileKey(gamekey="gk1", machine_name="b1", filename="book.pdf"),
        path=BOOK_DIR / "book.pdf",
        url=url("book.pdf"),
        md5=MD5_A,
        size=SIZE,
    )
    assert result == Plan(actions=[expected], bytes_to_fetch=SIZE)


def test_matching_size_and_md5_skips_case_insensitively() -> None:
    files = {BOOK_DIR / "book.pdf": LocalFile(size=SIZE, md5=MD5_A.upper())}
    result = run([order([sub("b1", [fv("book.pdf")])])], files)
    assert result.actions == [Skip(path=BOOK_DIR / "book.pdf")]
    assert result.bytes_to_fetch == 0


@pytest.mark.parametrize(
    ("remote_md5", "remote_size"),
    [(None, SIZE), (MD5_A, None), (None, None)],
)
def test_unknown_remote_fields_are_not_compared(
    remote_md5: str | None, remote_size: int | None
) -> None:
    files = {BOOK_DIR / "book.pdf": LocalFile(size=SIZE, md5=MD5_A)}
    result = run([order([sub("b1", [fv("book.pdf", remote_md5, remote_size)])])], files)
    assert result.actions == [Skip(path=BOOK_DIR / "book.pdf")]


def test_both_unknown_with_different_local_still_skips() -> None:
    files = {BOOK_DIR / "book.pdf": LocalFile(size=1, md5=MD5_B)}
    result = run([order([sub("b1", [fv("book.pdf", None, None)])])], files)
    assert result.actions == [Skip(path=BOOK_DIR / "book.pdf")]


def test_different_md5_archives_then_fetches() -> None:
    path = BOOK_DIR / "book.pdf"
    files = {path: LocalFile(size=SIZE, md5=MD5_DEAD)}
    result = run([order([sub("b1", [fv("book.pdf")])])], files)
    (action,) = result.actions
    assert isinstance(action, ArchiveThenFetch)
    assert action.existing == path
    assert action.archive_to == BOOK_DIR / "book.deadbeef.pdf"
    assert action.fetch.path == path
    assert result.bytes_to_fetch == SIZE


def test_different_size_archives() -> None:
    path = BOOK_DIR / "book.pdf"
    files = {path: LocalFile(size=SIZE + 1, md5=MD5_A)}
    (action,) = run([order([sub("b1", [fv("book.pdf")])])], files).actions
    assert isinstance(action, ArchiveThenFetch)
    assert action.archive_to == BOOK_DIR / "book.aaaaaaaa.pdf"


def test_archive_target_occupied_on_disk_gets_counter() -> None:
    path = BOOK_DIR / "book.pdf"
    files = {
        path: LocalFile(size=SIZE, md5=MD5_DEAD),
        BOOK_DIR / "book.deadbeef.pdf": LocalFile(size=1, md5=MD5_B),
        BOOK_DIR / "book.deadbeef-1.pdf": LocalFile(size=1, md5=MD5_B),
    }
    (action,) = run([order([sub("b1", [fv("book.pdf")])])], files).actions
    assert isinstance(action, ArchiveThenFetch)
    assert action.archive_to == BOOK_DIR / "book.deadbeef-2.pdf"


def test_archive_target_occupied_once_gets_dash_one() -> None:
    files = {
        BOOK_DIR / "book.pdf": LocalFile(size=SIZE, md5=MD5_DEAD),
        BOOK_DIR / "book.deadbeef.pdf": LocalFile(size=1, md5=MD5_B),
    }
    (action,) = run([order([sub("b1", [fv("book.pdf")])])], files).actions
    assert isinstance(action, ArchiveThenFetch)
    assert action.archive_to == BOOK_DIR / "book.deadbeef-1.pdf"


def test_archive_target_colliding_with_planned_path_gets_counter() -> None:
    files = {BOOK_DIR / "book.pdf": LocalFile(size=SIZE, md5=MD5_DEAD)}
    first = fv("book.deadbeef.pdf", md5=MD5_B)
    second = fv("book.pdf", md5=MD5_A)
    result = run([order([sub("b1", [first, second])])], files)
    fetch, archive = result.actions
    assert isinstance(fetch, Fetch)
    assert fetch.path == BOOK_DIR / "book.deadbeef.pdf"
    assert isinstance(archive, ArchiveThenFetch)
    assert archive.archive_to == BOOK_DIR / "book.deadbeef-1.pdf"


def test_same_book_in_two_orders_fetches_under_both_bundles() -> None:
    orders = [
        order([sub("b1", [fv("book.pdf", size=30)])], "gk1", "Bundle One"),
        order([sub("b1", [fv("book.pdf", size=70)])], "gk2", "Bundle Two"),
    ]
    result = run(orders)
    paths = [a.path for a in result.actions if isinstance(a, Fetch)]
    assert paths == [
        ROOT / "Bundle One" / ITEM / "book.pdf",
        ROOT / "Bundle Two" / ITEM / "book.pdf",
    ]
    assert result.bytes_to_fetch == 100


def test_unknown_sizes_count_as_zero_bytes() -> None:
    result = run([order([sub("b1", [fv("a.pdf", size=None), fv("b.pdf", size=5)])])])
    assert result.bytes_to_fetch == 5


def test_duplicate_path_same_md5_planned_once() -> None:
    result = run([order([sub("b1", [fv("book.pdf"), fv("book.pdf", md5=MD5_A.upper())])])])
    assert [a.path for a in result.actions if isinstance(a, Fetch)] == [BOOK_DIR / "book.pdf"]
    assert result.bytes_to_fetch == SIZE


def test_duplicate_path_different_md5_gets_versioned_name() -> None:
    result = run([order([sub("b1", [fv("book.pdf", MD5_A), fv("book.pdf", MD5_B)])])])
    assert written_paths(result) == [BOOK_DIR / "book.pdf", BOOK_DIR / "book.bbbbbbbb.pdf"]


def test_third_version_with_same_md5_as_second_is_skipped() -> None:
    files = [fv("book.pdf", MD5_A), fv("book.pdf", MD5_B), fv("book.pdf", MD5_B)]
    result = run([order([sub("b1", files)])])
    assert written_paths(result) == [BOOK_DIR / "book.pdf", BOOK_DIR / "book.bbbbbbbb.pdf"]


def test_same_md5_prefix_different_md5_gets_counter() -> None:
    other = "aaaaaaaa" + "1" * 24
    files = [
        fv("book.pdf", MD5_A + ""),
        fv("book.pdf", other),
        fv("book.pdf", "aaaaaaaa" + "2" * 24),
    ]
    result = run([order([sub("b1", files)])])
    assert written_paths(result) == [
        BOOK_DIR / "book.pdf",
        BOOK_DIR / "book.aaaaaaaa.pdf",
        BOOK_DIR / "book.aaaaaaaa-2.pdf",
    ]


def test_duplicate_path_unknown_md5_gets_counter_names() -> None:
    files = [fv("book.pdf", None), fv("book.pdf", None), fv("book.pdf", None)]
    result = run([order([sub("b1", files)])])
    assert written_paths(result) == [
        BOOK_DIR / "book.pdf",
        BOOK_DIR / "book-2.pdf",
        BOOK_DIR / "book-3.pdf",
    ]


def test_duplicate_with_unknown_md5_after_known_is_not_merged() -> None:
    result = run([order([sub("b1", [fv("book.pdf", MD5_A), fv("book.pdf", None)])])])
    assert written_paths(result) == [BOOK_DIR / "book.pdf", BOOK_DIR / "book-2.pdf"]


def test_collision_across_orders_with_same_bundle_name_different_md5() -> None:
    orders = [
        order([sub("b1", [fv("book.pdf", MD5_A)])], "gk1"),
        order([sub("b1", [fv("book.pdf", MD5_B)])], "gk2"),
    ]
    assert written_paths(run(orders)) == [BOOK_DIR / "book.pdf", BOOK_DIR / "book.bbbbbbbb.pdf"]


def test_collision_across_orders_same_md5_planned_once() -> None:
    orders = [
        order([sub("b1", [fv("book.pdf")])], "gk1"),
        order([sub("b1", [fv("book.pdf")])], "gk2"),
    ]
    assert written_paths(run(orders)) == [BOOK_DIR / "book.pdf"]


def test_collision_is_case_insensitive_across_orders() -> None:
    orders = [
        order([sub("b1", [fv("Book.pdf", MD5_A)])], "gk1", "Bundle"),
        order([sub("b1", [fv("book.PDF", MD5_B)])], "gk2", "BUNDLE"),
    ]
    paths = written_paths(run(orders))
    assert paths == [BOOK_DIR / "Book.pdf", ROOT / "BUNDLE" / ITEM / "book.bbbbbbbb.PDF"]
    assert len({str(p).casefold() for p in paths}) == len(paths)


def test_skip_claims_path_so_later_different_version_is_versioned() -> None:
    files = {BOOK_DIR / "book.pdf": LocalFile(size=SIZE, md5=MD5_A)}
    result = run([order([sub("b1", [fv("book.pdf", MD5_A), fv("book.pdf", MD5_B)])])], files)
    assert result.actions[0] == Skip(path=BOOK_DIR / "book.pdf")
    second = result.actions[1]
    assert isinstance(second, Fetch)
    assert second.path == BOOK_DIR / "book.bbbbbbbb.pdf"


def test_items_with_same_name_get_machine_name_suffix() -> None:
    subs = [sub("one", [fv("a.pdf")], human="Same"), sub("two", [fv("a.pdf")], human="Same")]
    paths = written_paths(run([order(subs)]))
    assert paths == [ROOT / BUNDLE / "Same" / "a.pdf", ROOT / BUNDLE / "Same-two" / "a.pdf"]


def test_bundle_and_item_names_are_sanitized() -> None:
    result = run([order([sub("b1", [fv("a.pdf")], human="Vol: 1/2")], human="Pack: A")])
    assert written_paths(result) == [ROOT / "Pack - A" / "Vol - 1 - 2" / "a.pdf"]


def test_non_ebook_downloads_are_ignored() -> None:
    audio = Subproduct(
        machine_name="a",
        human_name="Audio",
        downloads=[Download(platform="audio", files=[fv("a.mp3")])],
    )
    assert run([order([audio])]).actions == []


def test_plan_is_deterministic() -> None:
    orders = [order([sub("b1", [fv("book.pdf"), fv("book.pdf", MD5_B)])])]
    assert run(orders) == run(orders)


def fixture_orders() -> list[Order]:
    first = parse_order(json.loads(FIXTURE.read_text(encoding="utf-8")))
    clone = first.model_copy(update={"gamekey": "OTHER"})
    return [first, clone]


def assert_global_invariants(result: Plan, files: LocalMap) -> None:
    paths = written_paths(result)
    assert len({str(p).casefold() for p in paths}) == len(paths)
    for action in result.actions:
        if isinstance(action, Fetch):
            assert action.path not in files


def test_fixture_plan_has_no_collisions_on_empty_disk() -> None:
    result = run(fixture_orders())
    assert_global_invariants(result, {})
    assert all(isinstance(a, Fetch) for a in result.actions)
    assert result.actions


def test_fixture_plan_invariants_with_stale_disk() -> None:
    first = run(fixture_orders())
    stale: LocalMap = {
        a.path: LocalFile(size=1, md5=MD5_DEAD) for a in first.actions if isinstance(a, Fetch)
    }
    result = run(fixture_orders(), stale)
    paths = written_paths(result)
    assert len({str(p).casefold() for p in paths}) == len(paths)
    actions: list[Action] = result.actions
    for action in actions:
        assert isinstance(action, ArchiveThenFetch)
        assert action.existing in stale
        assert action.archive_to not in stale
