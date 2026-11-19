"""Tests for the mtime-keyed md5 hash cache."""

import hashlib
import json
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

import pytest

from humble_dl.lib.hashcache import HashCache, LocalFile

CONTENT = b"hello humble"
CONTENT_MD5 = hashlib.md5(CONTENT, usedforsecurity=False).hexdigest()
OTHER_CONTENT = b"different bytes!"
OTHER_MD5 = hashlib.md5(OTHER_CONTENT, usedforsecurity=False).hexdigest()
FAKE_MD5 = "a" * 32
BIG_SIZE = 2 * 1024 * 1024 + 17


class CountingOpener:
    """Opener that counts how many times files are opened."""

    def __init__(self) -> None:
        self.opened: list[Path] = []

    def __call__(self, path: Path) -> BinaryIO:
        self.opened.append(path)
        return path.open("rb")


@pytest.fixture
def book(tmp_path: Path) -> Path:
    path = tmp_path / "book.epub"
    path.write_bytes(CONTENT)
    return path


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "hash-cache.json"


def test_md5_for_unchanged_file_reads_once(book: Path, cache_path: Path) -> None:
    opener = CountingOpener()
    cache = HashCache.load(cache_path, opener=opener)

    first = cache.md5_for(book)
    second = cache.md5_for(book)

    assert first == CONTENT_MD5
    assert second == CONTENT_MD5
    assert opener.opened == [book]


def test_md5_for_recomputes_when_mtime_changes(book: Path, cache_path: Path) -> None:
    opener = CountingOpener()
    cache = HashCache.load(cache_path, opener=opener)
    assert cache.md5_for(book) == CONTENT_MD5

    stat = book.stat()
    book.write_bytes(OTHER_CONTENT)
    os.utime(book, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    assert cache.md5_for(book) == OTHER_MD5
    assert len(opener.opened) == 2


def test_md5_for_hashes_multiple_chunks(tmp_path: Path, cache_path: Path) -> None:
    big = tmp_path / "big.pdf"
    data = b"x" * BIG_SIZE
    big.write_bytes(data)

    cache = HashCache.load(cache_path)

    assert cache.md5_for(big) == hashlib.md5(data, usedforsecurity=False).hexdigest()


def test_md5_for_uses_injected_opener_stream(tmp_path: Path, cache_path: Path) -> None:
    path = tmp_path / "virtual.epub"
    path.write_bytes(b"on disk")

    def opener(_: Path) -> BinaryIO:
        return BytesIO(CONTENT)

    cache = HashCache.load(cache_path, opener=opener)

    assert cache.md5_for(path) == CONTENT_MD5


def test_record_then_local_file_does_not_read(book: Path, cache_path: Path) -> None:
    opener = CountingOpener()
    cache = HashCache.load(cache_path, opener=opener)

    cache.record(book, FAKE_MD5)

    assert cache.local_file(book) == LocalFile(size=len(CONTENT), md5=FAKE_MD5)
    assert opener.opened == []


def test_local_file_missing_returns_none(tmp_path: Path, cache_path: Path) -> None:
    cache = HashCache.load(cache_path)

    assert cache.local_file(tmp_path / "absent.epub") is None


def test_local_file_computes_when_not_cached(book: Path, cache_path: Path) -> None:
    cache = HashCache.load(cache_path)

    assert cache.local_file(book) == LocalFile(size=len(CONTENT), md5=CONTENT_MD5)


def test_save_and_load_round_trip(book: Path, cache_path: Path) -> None:
    cache = HashCache.load(cache_path)
    cache.record(book, FAKE_MD5)
    cache.save()

    opener = CountingOpener()
    reloaded = HashCache.load(cache_path, opener=opener)

    assert reloaded.md5_for(book) == FAKE_MD5
    assert opener.opened == []


def test_save_creates_parent_dir_and_leaves_no_temp_files(book: Path, cache_path: Path) -> None:
    cache = HashCache.load(cache_path)
    cache.record(book, FAKE_MD5)

    cache.save()

    assert [p.name for p in cache_path.parent.iterdir()] == [cache_path.name]
    assert json.loads(cache_path.read_text(encoding="utf-8")) is not None


def test_load_missing_file_is_empty(book: Path, cache_path: Path) -> None:
    cache = HashCache.load(cache_path)

    assert cache.md5_for(book) == CONTENT_MD5


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "[1, 2]",
        '{"entries": "nope"}',
        '{"entries": [{"path": "p"}]}',
        '{"entries": [{"path": 1, "size": 1, "mtime_ns": 1, "md5": "x"}]}',
        '{"entries": ["bad"]}',
    ],
)
def test_load_corrupt_cache_is_empty_and_warns(
    payload: str, book: Path, cache_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(payload, encoding="utf-8")
    opener = CountingOpener()

    with caplog.at_level(logging.WARNING, logger="humble_dl.lib.hashcache"):
        cache = HashCache.load(cache_path, opener=opener)

    assert cache.md5_for(book) == CONTENT_MD5
    assert opener.opened == [book]
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert payload not in caplog.text


def test_load_undecodable_cache_is_empty_and_warns(
    cache_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"\xff\xfe\x00bad")

    with caplog.at_level(logging.WARNING, logger="humble_dl.lib.hashcache"):
        HashCache.load(cache_path)

    assert len(caplog.records) == 1


def test_save_failure_cleans_up_temp_file_and_raises(book: Path, tmp_path: Path) -> None:
    target = tmp_path / "cache-dir"
    target.mkdir()
    cache = HashCache.load(target)
    cache.record(book, FAKE_MD5)

    with pytest.raises(OSError):
        cache.save()

    assert [p.name for p in target.iterdir()] == []
