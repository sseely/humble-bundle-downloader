"""Tests for the Apple Books import service (Books is never launched)."""

import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from humble_dl.config import ConfigError, Settings
from humble_dl.services import books
from humble_dl.services.books import (
    BooksDeps,
    BooksOptions,
    BooksPlatformError,
    BooksStateError,
    FileStamp,
)

OPEN_PREFIX = ["open", "-a", "Books"]
PAUSE = 3.0


class FakeRunner:
    """Records argv lists; fails the calls whose 1-based index is in ``fail_on``."""

    def __init__(self, fail_on: frozenset[int] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv: Sequence[str]) -> bool:
        self.calls.append(list(argv))
        return len(self.calls) not in self.fail_on


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


def test_chunk() -> None:
    paths = [Path(str(n)) for n in range(5)]
    assert [len(c) for c in books.chunk(paths, 2)] == [2, 2, 1]
    assert books.chunk([], 3) == []


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
        books.save_state(tmp_path / "s.json", {})
    assert list(tmp_path.iterdir()) == []


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


@pytest.mark.parametrize(("batch", "pause"), [(0, 5.0), (51, 5.0), (5, -1.0), (5, 61.0)])
def test_invalid_options(tmp_path: Path, batch: int, pause: float) -> None:
    runner = FakeRunner()
    with pytest.raises(ConfigError):
        books.import_books(
            make_settings(tmp_path),
            BooksOptions(batch=batch, pause=pause),
            BooksDeps(runner=runner, sleep=lambda s: None, platform="darwin"),
        )
    assert runner.calls == []


def seed(settings: Settings, count: int = 5) -> list[Path]:
    return [touch(settings.output_dir / "B" / f"I{n}" / f"b{n}.epub") for n in range(count)]


def deps_for(runner: FakeRunner, sleeps: list[float]) -> BooksDeps:
    return BooksDeps(runner=runner, sleep=sleeps.append, platform="darwin")


def test_first_run_batches_then_second_run_skips(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = [p.resolve() for p in seed(settings)]
    runner, sleeps = FakeRunner(), list[float]()
    opts = BooksOptions(batch=2, pause=PAUSE)
    report = books.import_books(settings, opts, deps_for(runner, sleeps))
    assert runner.calls == [
        [*OPEN_PREFIX, *map(str, files[0:2])],
        [*OPEN_PREFIX, *map(str, files[2:4])],
        [*OPEN_PREFIX, *map(str, files[4:5])],
    ]
    assert sleeps == [PAUSE, PAUSE]
    assert (report.selected, report.already, report.imported, report.changed) == (5, 0, 5, 0)
    saved = json.loads(settings.books_state_path.read_text())["files"]
    assert sorted(saved) == [str(f) for f in files]
    again = books.import_books(settings, opts, deps_for(runner, sleeps))
    assert len(runner.calls) == 3
    assert (again.selected, again.already, again.imported) == (5, 5, 0)


def test_zero_pause_does_not_sleep(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings, 2)
    sleeps: list[float] = []
    books.import_books(settings, BooksOptions(batch=1, pause=0), deps_for(FakeRunner(), sleeps))
    assert sleeps == []


def test_changed_file_reimported_alone(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = seed(settings, 3)
    opts = BooksOptions(batch=10, pause=0)
    books.import_books(settings, opts, deps_for(FakeRunner(), list[float]()))
    files[1].write_text("longer content")
    runner = FakeRunner()
    report = books.import_books(settings, opts, deps_for(runner, list[float]()))
    assert runner.calls == [[*OPEN_PREFIX, str(files[1].resolve())]]
    assert (report.imported, report.changed, report.already) == (0, 1, 2)


def test_failed_batch_not_recorded_run_continues(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = [p.resolve() for p in seed(settings, 6)]
    runner = FakeRunner(fail_on=frozenset({2}))
    report = books.import_books(
        settings, BooksOptions(batch=2, pause=0), deps_for(runner, list[float]())
    )
    assert len(runner.calls) == 3
    assert report.failed == tuple(files[2:4])
    assert report.imported == 4
    saved = sorted(json.loads(settings.books_state_path.read_text())["files"])
    assert saved == [str(f) for f in (*files[0:2], *files[4:6])]


def test_corrupt_state_stops_before_any_call(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings)
    touch(settings.books_state_path, "garbage")
    runner = FakeRunner()
    with pytest.raises(BooksStateError):
        books.import_books(settings, BooksOptions(), deps_for(runner, list[float]()))
    assert runner.calls == []


def test_non_darwin_rejected(tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(BooksPlatformError, match="requires macOS"):
        books.import_books(
            make_settings(tmp_path),
            BooksOptions(),
            BooksDeps(runner=runner, sleep=lambda s: None, platform="linux"),
        )
    assert runner.calls == []


def test_dry_run_any_platform_no_calls_no_writes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed(settings, 2)
    runner = FakeRunner()
    report = books.import_books(
        settings,
        BooksOptions(dry_run=True),
        BooksDeps(runner=runner, sleep=lambda s: None, platform="linux"),
    )
    assert report.lines == (
        "import B/I0/b0.epub",
        "import B/I1/b1.epub",
        "Dry run: 2 files to import.",
    )
    assert runner.calls == []
    assert not settings.books_state_path.exists()


def test_dry_run_marks_reimport(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    files = seed(settings, 2)
    books.import_books(settings, BooksOptions(pause=0), deps_for(FakeRunner(), list[float]()))
    files[0].write_text("changed!!")
    report = books.import_books(settings, BooksOptions(dry_run=True))
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
