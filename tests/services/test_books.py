"""Tests for the Apple Books import service (Books is never launched)."""

import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from humble_dl.config import ConfigError, Settings
from humble_dl.services import books
from humble_dl.services.books import (
    BooksDeps,
    BooksLibraryError,
    BooksOptions,
    BooksPlatformError,
    BooksStateError,
    FileStamp,
)
from tests.helpers.books import FakeBooks, make_epub, make_library

OPEN_PREFIX = ["open", "-a", "Books"]
PAUSE = 3.0


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point HOME at an empty dir so the default Books library path is never real."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def touch(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=tmp_path / "out",
        profile_dir=tmp_path / "p",
        debug_dir=tmp_path / "d",
        hash_cache_path=tmp_path / "h.json",
        books_state_path=tmp_path / "state" / "books.json",
        titles_path=tmp_path / "titles.json",
        concurrency=1,
        api_rps=1.0,
        http_timeout_s=1.0,
        api_timeout_s=1.0,
        login_timeout_s=1.0,
    )


def names(paths: Sequence[Path], root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in paths]


def test_selects_only_current_epub(tmp_path: Path) -> None:
    item = tmp_path / "B" / "I"
    for name in ("book.epub", "book.pdf", "book.mobi", "book.1a2b3c4d.epub", "book.epub.part"):
        touch(item / name)
    touch(item / ".hidden.epub")
    assert names(books.select_books(tmp_path), tmp_path) == ["B/I/book.epub"]


def test_falls_back_to_pdf_case_insensitive(tmp_path: Path) -> None:
    touch(tmp_path / "B" / "I" / "x.pdf")
    touch(tmp_path / "B" / "I" / "x.MOBI")
    touch(tmp_path / "B" / "J" / "y.PDF")
    touch(tmp_path / "B" / "K" / "z.EPUB")
    touch(tmp_path / "B" / "K" / "z.pdf")
    assert names(books.select_books(tmp_path), tmp_path) == ["B/I/x.pdf", "B/J/y.PDF", "B/K/z.EPUB"]


def test_mobi_only_selects_nothing(tmp_path: Path) -> None:
    touch(tmp_path / "B" / "I" / "x.mobi")
    assert books.select_books(tmp_path) == []


def test_two_current_epubs_both_selected(tmp_path: Path) -> None:
    touch(tmp_path / "B" / "I" / "a.epub")
    touch(tmp_path / "B" / "I" / "b.epub")
    assert names(books.select_books(tmp_path), tmp_path) == ["B/I/a.epub", "B/I/b.epub"]


def test_wrong_depths_and_hidden_dirs_ignored(tmp_path: Path) -> None:
    touch(tmp_path / "top.epub")
    touch(tmp_path / "B" / "bundle.epub")
    touch(tmp_path / "B" / "I" / "deep" / "d.epub")
    touch(tmp_path / ".hid" / "I" / "h.epub")
    touch(tmp_path / "B" / ".hid" / "h.epub")
    assert books.select_books(tmp_path) == []


def test_missing_output_dir_selects_nothing(tmp_path: Path) -> None:
    assert books.select_books(tmp_path / "nope") == []


def test_only_filters_bundle_case_insensitively(tmp_path: Path) -> None:
    touch(tmp_path / "Python Bundle" / "I" / "a.epub")
    touch(tmp_path / "Rust Bundle" / "I" / "b.epub")
    assert names(books.select_books(tmp_path, "PYTHON"), tmp_path) == ["Python Bundle/I/a.epub"]


def test_classify_new_changed_already(tmp_path: Path) -> None:
    a, b, c = (tmp_path / n for n in "abc")
    stamps = {p: FileStamp(size=1, mtime_ns=1) for p in (a, b, c)}
    state = {str(b): FileStamp(size=1, mtime_ns=2), str(c): FileStamp(size=1, mtime_ns=1)}
    plan = books.classify(stamps, state)
    assert (plan.new, plan.changed, plan.already) == ((a,), (b,), (c,))
    assert plan.selected == 3
    assert plan.to_import == [a, b]


def test_state_missing_is_empty(tmp_path: Path) -> None:
    assert books.load_state(tmp_path / "none.json") == {}


def test_state_round_trip_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "s.json"
    state = {"/a": FileStamp(size=3, mtime_ns=9)}
    books.save_state(path, state)
    assert books.load_state(path) == state
    assert [p.name for p in path.parent.iterdir()] == ["s.json"]


def test_save_state_failure_cleans_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(src: str, dst: Path) -> None:
        raise OSError("disk")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk"):
        books.save_state(tmp_path / "state" / "s.json", {})
    assert list((tmp_path / "state").iterdir()) == []


@pytest.mark.parametrize(
    "text",
    ["{not json", "[]", '{"files": {"/a": {"size": "x", "mtime_ns": 1}}}', '{"files": 1}'],
)
def test_corrupt_state_raises(tmp_path: Path, text: str) -> None:
    path = touch(tmp_path / "s.json", text)
    with pytest.raises(BooksStateError, match="corrupt"):
        books.load_state(path)


def test_unreadable_state_raises(tmp_path: Path) -> None:
    (tmp_path / "s.json").mkdir()
    with pytest.raises(BooksStateError):
        books.load_state(tmp_path / "s.json")


@pytest.mark.parametrize("pause", [-1.0, 61.0])
def test_invalid_pause(tmp_path: Path, pause: float) -> None:
    fake = FakeBooks(tmp_path / "lib")
    with pytest.raises(ConfigError):
        books.import_books(make_settings(tmp_path), BooksOptions(pause=pause), fake.deps())
    assert fake.calls == []


def seed(settings: Settings, count: int = 5) -> list[Path]:
    return [touch(settings.output_dir / "B" / f"I{n}" / f"b{n}.epub") for n in range(count)]


def test_one_file_per_open_then_second_run_skips(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = [p.resolve() for p in seed(settings, 3)]
    fake = FakeBooks(tmp_path / "lib")
    opts = BooksOptions(pause=PAUSE)
    report = books.import_books(settings, opts, fake.deps())
    assert fake.calls == [[*OPEN_PREFIX, str(f)] for f in files]
    assert fake.sleeps == [PAUSE, PAUSE]
    assert (report.selected, report.already, report.imported, report.changed) == (3, 0, 3, 0)
    saved = json.loads(settings.books_state_path.read_text())["files"]
    assert sorted(saved) == [str(f) for f in files]
    fake.forget()  # skipping must come from the state file, not the library
    again = books.import_books(settings, opts, fake.deps())
    assert len(fake.calls) == 3
    assert (again.selected, again.already, again.imported) == (3, 3, 0)


def test_zero_pause_does_not_sleep(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings, 2)
    fake = FakeBooks(tmp_path / "lib")
    books.import_books(settings, BooksOptions(pause=0), fake.deps())
    assert fake.sleeps == []


def test_changed_file_reimported_alone(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = seed(settings, 3)
    fake = FakeBooks(tmp_path / "lib")
    opts = BooksOptions(pause=0)
    books.import_books(settings, opts, fake.deps())
    files[1].write_text("longer content")
    fake.forget()
    report = books.import_books(settings, opts, fake.deps())
    assert fake.calls[3:] == [[*OPEN_PREFIX, str(files[1].resolve())]]
    assert (report.imported, report.changed, report.already) == (0, 1, 2)


def test_failed_open_not_recorded_run_continues(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = [p.resolve() for p in seed(settings, 3)]
    fake = FakeBooks(tmp_path / "lib", fail_on=frozenset({2}))
    report = books.import_books(settings, BooksOptions(pause=0), fake.deps())
    assert len(fake.calls) == 3
    assert fake.sleeps == []  # a failed open is not waited on
    assert (report.failed, report.imported) == ((files[1],), 2)
    saved = sorted(json.loads(settings.books_state_path.read_text())["files"])
    assert saved == [str(files[0]), str(files[2])]


def test_silently_dropped_file_not_recorded(tmp_path: Path) -> None:
    """``open`` exits 0 but Books never adds the book: wait, then report it failed."""
    settings = make_settings(tmp_path)
    files = [p.resolve() for p in seed(settings, 3)]
    fake = FakeBooks(tmp_path / "lib", drop_on=frozenset({2}))
    report = books.import_books(
        settings, BooksOptions(pause=0), fake.deps(confirm_timeout=2.0, poll=0.5)
    )
    assert fake.sleeps == [0.5, 0.5, 0.5, 0.5]
    assert (report.failed, report.imported) == ((files[1],), 2)
    saved = sorted(json.loads(settings.books_state_path.read_text())["files"])
    assert saved == [str(files[0]), str(files[2])]


def test_slow_import_confirmed_when_entry_appears(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    (only,) = [p.resolve() for p in seed(settings, 1)]
    fake = FakeBooks(tmp_path / "lib", drop_on=frozenset({1}))
    entry = fake.library / only.name

    def slow_sleep(seconds: float) -> None:
        fake.sleep(seconds)
        if fake.now >= 1.0:
            entry.mkdir(exist_ok=True)  # Books finishes copying after a second

    deps = replace(fake.deps(confirm_timeout=5.0, poll=0.5), sleep=slow_sleep)
    report = books.import_books(settings, BooksOptions(pause=0), deps)
    assert fake.sleeps == [0.5, 0.5]
    assert (report.failed, report.imported) == ((), 1)


def test_missing_library_stops_before_any_open(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings, 1)
    fake = FakeBooks(tmp_path / "lib")
    deps = replace(fake.deps(), library_dir=tmp_path / "none")
    with pytest.raises(BooksLibraryError, match="cannot be confirmed"):
        books.import_books(settings, BooksOptions(pause=0), deps)
    assert fake.calls == []
    assert not settings.books_state_path.exists()


def test_corrupt_state_stops_before_any_call(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings)
    touch(settings.books_state_path, "garbage")
    fake = FakeBooks(tmp_path / "lib")
    with pytest.raises(BooksStateError):
        books.import_books(settings, BooksOptions(), fake.deps())
    assert fake.calls == []


def test_non_darwin_rejected(tmp_path: Path) -> None:
    fake = FakeBooks(tmp_path / "lib")
    with pytest.raises(BooksPlatformError, match="requires macOS"):
        books.import_books(make_settings(tmp_path), BooksOptions(), fake.deps(platform="linux"))
    assert fake.calls == []


def test_dry_run_any_platform_no_calls_no_writes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings, 2)
    fake = FakeBooks(tmp_path / "lib")
    report = books.import_books(settings, BooksOptions(dry_run=True), fake.deps(platform="linux"))
    assert report.lines == (
        "import B/I0/b0.epub",
        "import B/I1/b1.epub",
        "Dry run: 2 files to import.",
    )
    assert fake.calls == []
    assert not settings.books_state_path.exists()


def test_dry_run_without_library_still_plans(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings, 1)
    report = books.import_books(
        settings,
        BooksOptions(dry_run=True),
        BooksDeps(library_dir=tmp_path / "none", platform="linux"),
    )
    assert report.lines == ("import B/I0/b0.epub", "Dry run: 1 files to import.")


def test_dry_run_marks_reimport(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = seed(settings, 2)
    books.import_books(settings, BooksOptions(pause=0), FakeBooks(tmp_path / "lib").deps())
    files[0].write_text("changed!!")
    report = books.import_books(
        settings,
        BooksOptions(dry_run=True),
        BooksDeps(library_dir=tmp_path / "no-library", platform="linux"),
    )
    assert report.lines == ("reimport B/I0/b0.epub", "Dry run: 1 files to import.")


class _Done:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


@pytest.mark.parametrize(("code", "expected"), [(0, True), (1, False)])
def test_run_open_uses_arg_list(monkeypatch: pytest.MonkeyPatch, code: int, expected: bool) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> _Done:
        seen.update(argv=argv, **kwargs)
        return _Done(code)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert books.run_open(["open", "-a", "Books", "f"]) is expected
    assert seen == {
        "argv": ["open", "-a", "Books", "f"],
        "check": False,
        "capture_output": True,
        "timeout": books.OPEN_TIMEOUT_S,
    }


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("open", 1), FileNotFoundError("open")])
def test_run_open_failure_returns_false(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> _Done:
        raise error

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert books.run_open(["open"]) is False


def test_cross_bundle_duplicates_import_one_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    epub = touch(settings.output_dir / "B1" / "I" / "a.epub").resolve()
    touch(settings.output_dir / "B2" / "J" / "A.pdf")
    runner = FakeBooks(tmp_path / "lib")
    report = books.import_books(settings, BooksOptions(pause=0), runner.deps())
    assert runner.calls == [[*OPEN_PREFIX, str(epub)]]
    assert (report.selected, report.imported, report.duplicates) == (2, 1, 1)


def test_books_already_in_library_skipped_not_recorded(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    touch(settings.output_dir / "B" / "I" / "a.epub")
    touch(settings.output_dir / "B" / "J" / "b.epub")
    touch(settings.output_dir / "B" / "K" / "c.epub")
    library = make_library(tmp_path / "lib", files=["a_1403628954.pdf"], dirs=["b.epub"])
    runner = FakeBooks(library)
    report = books.import_books(settings, BooksOptions(pause=0), runner.deps())
    assert [Path(c[-1]).name for c in runner.calls] == ["c.epub"]
    assert (report.selected, report.in_books, report.imported) == (3, 2, 1)
    saved = json.loads(settings.books_state_path.read_text())["files"]
    assert [Path(k).name for k in saved] == ["c.epub"]


def test_dry_run_lists_skip_lines(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    touch(settings.output_dir / "B1" / "I" / "a.epub")
    touch(settings.output_dir / "B2" / "I" / "a.pdf")
    touch(settings.output_dir / "B1" / "J" / "b.epub")
    library = make_library(tmp_path / "lib", dirs=["b.epub"])
    report = books.import_books(
        settings,
        BooksOptions(dry_run=True),
        BooksDeps(library_dir=library, platform="linux"),
    )
    assert report.lines == (
        "import B1/I/a.epub",
        "skip-duplicate B2/I/a.pdf",
        "skip-in-books B1/J/b.epub",
        "Dry run: 1 files to import.",
    )
    assert (report.duplicates, report.in_books) == (1, 1)


def test_title_match_skips_epub_already_in_books(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    make_epub(settings.output_dir / "B" / "I" / "redshirts_johnscalzi.epub", "Redshirts")
    library = make_library(tmp_path / "lib", dirs=["Redshirts.epub"])
    runner = FakeBooks(library)
    report = books.import_books(settings, BooksOptions(pause=0), runner.deps())
    assert (runner.calls, report.in_books, report.imported) == ([], 1, 0)
    assert not settings.books_state_path.exists()


def test_same_title_across_bundles_imports_once(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    first = make_epub(
        settings.output_dir / "A" / "I" / "oldmanswar_johnscalzi.epub", "Old Man's War"
    ).resolve()
    make_epub(settings.output_dir / "B" / "J" / "old_mans_war.epub", "Old Man's War")
    runner = FakeBooks(tmp_path / "lib")
    report = books.import_books(settings, BooksOptions(pause=0), runner.deps())
    assert runner.calls == [[*OPEN_PREFIX, str(first)]]
    assert (report.imported, report.duplicates) == (1, 1)


def test_pdf_collision_suffix_in_library_skips(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    touch(settings.output_dir / "B" / "I" / "foo.pdf")
    library = make_library(tmp_path / "lib", files=["foo_1418163538.pdf"])
    runner = FakeBooks(library)
    report = books.import_books(settings, BooksOptions(pause=0), runner.deps())
    assert (runner.calls, report.in_books) == ([], 1)
