"""Tests for the streaming downloader."""

import asyncio
import errno
import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from humble_dl.config import Settings
from humble_dl.services.planner import Action, ArchiveThenFetch, Fetch, FileKey, Skip
from humble_dl.workers import downloader
from humble_dl.workers.downloader import Result, execute

GAMEKEY = "gk-ONE"
BASE_URL = "https://dl.example.test/files"
OLD_QUERY = "sig=TOPSECRET&token=abc"
NEW_QUERY = "sig=FRESH"
PARTIAL = b"p" * downloader.CHUNK_SIZE
BODY = b"hello ebook bytes"
BODY_MD5 = hashlib.md5(BODY, usedforsecurity=False).hexdigest()
OLD_BODY = b"old version"
OLD_MD5 = hashlib.md5(OLD_BODY, usedforsecurity=False).hexdigest()
EXPECTED_SLEEPS = [downloader.BACKOFF_BASE_S, downloader.BACKOFF_BASE_S * 2]

Handler = Callable[[httpx.Request], httpx.Response]


class _DroppingStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield PARTIAL
        raise httpx.ReadError("dropped")


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _Server:
    """Routes by URL path to a queue of responses; the last response repeats."""

    def __init__(self, routes: dict[str, list[Callable[[], httpx.Response]]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes[request.url.path]
        factory = queue.pop(0) if len(queue) > 1 else queue[0]
        return factory()


def _ok(body: bytes = BODY) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(200, content=body)


def _status(code: int) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(code)


def _drop() -> httpx.Response:
    return httpx.Response(200, stream=_DroppingStream())


def _url(name: str, query: str = OLD_QUERY) -> str:
    return f"{BASE_URL}/{name}?{query}"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=tmp_path,
        profile_dir=tmp_path / "p",
        debug_dir=tmp_path / "d",
        hash_cache_path=tmp_path / "h.json",
        books_state_path=tmp_path / "books.json",
        concurrency=4,
        api_rps=2.0,
        http_timeout_s=5.0,
        api_timeout_s=5.0,
        login_timeout_s=5.0,
    )


@pytest.fixture
def sleeps() -> _Sleeps:
    return _Sleeps()


def _fetch(
    root: Path,
    name: str = "a.pdf",
    *,
    md5: str | None = BODY_MD5,
    size: int | None = len(BODY),
    query: str = OLD_QUERY,
) -> Fetch:
    path = root / "Bundle" / "Item" / name
    return Fetch(FileKey(GAMEKEY, "item", name), path, _url(name, query), md5, size)


async def _no_refresh(gamekey: str) -> dict[FileKey, str]:
    raise AssertionError(f"unexpected refresh for {gamekey}")


async def _run(
    actions: list[Action],
    handler: Handler,
    settings: Settings,
    sleeps: _Sleeps,
    refresh: downloader.Refresh = _no_refresh,
) -> list[Result]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await execute(actions, client, refresh, settings, sleep=sleeps)


def _server_for(*names: str, body: bytes = BODY) -> _Server:
    return _Server({f"/files/{n}": [_ok(body)] for n in names})


def _log_events(err: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")]


async def test_skip_returns_skipped(tmp_path: Path, settings: Settings, sleeps: _Sleeps) -> None:
    [result] = await _run([Skip(tmp_path / "x.pdf")], _Server({}), settings, sleeps)
    assert result == Result(tmp_path / "x.pdf", "skipped", 0, None, None)


async def test_fetch_downloads_and_verifies(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path)
    [result] = await _run([fetch], _server_for("a.pdf"), settings, sleeps)
    assert result == Result(fetch.path, "downloaded", len(BODY), BODY_MD5, None)
    assert fetch.path.read_bytes() == BODY
    assert not fetch.path.with_name("a.pdf.part").exists()


async def test_md5_compare_is_case_insensitive(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path, md5=BODY_MD5.upper())
    [result] = await _run([fetch], _server_for("a.pdf"), settings, sleeps)
    assert result.status == "downloaded"
    assert result.md5 == BODY_MD5


async def test_unknown_size_and_md5_are_not_verified(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path, md5=None, size=None)
    [result] = await _run([fetch], _server_for("a.pdf"), settings, sleeps)
    assert result.status == "downloaded"
    assert result.bytes == len(BODY)


async def test_md5_mismatch_fails_and_removes_part(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path, md5="0" * 32)
    [result] = await _run([fetch], _server_for("a.pdf"), settings, sleeps)
    assert result.status == "failed"
    assert result.error is not None and "md5" in result.error
    assert list(fetch.path.parent.iterdir()) == []


async def test_size_mismatch_fails(tmp_path: Path, settings: Settings, sleeps: _Sleeps) -> None:
    fetch = _fetch(tmp_path, size=len(BODY) + 1)
    [result] = await _run([fetch], _server_for("a.pdf"), settings, sleeps)
    assert result.status == "failed"
    assert result.error == f"size mismatch: expected {len(BODY) + 1}, got {len(BODY)}"
    assert list(fetch.path.parent.iterdir()) == []


async def test_mid_body_drop_keeps_part_and_retries(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path)
    server = _Server({"/files/a.pdf": [_drop]})
    [result] = await _run([fetch], server, settings, sleeps)
    assert result.status == "failed"
    assert fetch.path.with_name("a.pdf.part").read_bytes() == PARTIAL
    assert not fetch.path.exists()
    assert len(server.requests) == downloader.MAX_ATTEMPTS
    assert sleeps.calls == EXPECTED_SLEEPS


async def test_retry_restarts_part_from_zero(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path)
    server = _Server({"/files/a.pdf": [_drop, _status(503), _ok()]})
    [result] = await _run([fetch], server, settings, sleeps)
    assert result.status == "downloaded"
    assert fetch.path.read_bytes() == BODY
    assert sleeps.calls == EXPECTED_SLEEPS


async def test_not_found_fails_without_retry(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    server = _Server({"/files/a.pdf": [_status(404)]})
    [result] = await _run([_fetch(tmp_path)], server, settings, sleeps)
    assert result.status == "failed"
    assert result.error == "HTTP 404"
    assert len(server.requests) == 1
    assert sleeps.calls == []


async def test_error_never_contains_query(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _Server({"/files/a.pdf": [_status(500)]})
    [result] = await _run([_fetch(tmp_path)], server, settings, sleeps)
    assert result.error is not None and "TOPSECRET" not in result.error
    assert "TOPSECRET" not in capsys.readouterr().err


async def test_403_refreshes_once_and_retries_with_new_url(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY) if "FRESH" in str(request.url) else _status(403)()

    async def refresh(gamekey: str) -> dict[FileKey, str]:
        calls.append(gamekey)
        return {fetch.key: _url("a.pdf", NEW_QUERY)}

    [result] = await _run([fetch], handler, settings, sleeps, refresh)
    assert result.status == "downloaded"
    assert calls == [GAMEKEY]


async def test_concurrent_403s_on_one_gamekey_refresh_once(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetches = [_fetch(tmp_path, f"f{i}.pdf") for i in range(3)]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=BODY) if "FRESH" in str(request.url) else _status(403)()

    async def refresh(gamekey: str) -> dict[FileKey, str]:
        calls.append(gamekey)
        await asyncio.sleep(0.01)
        return {f.key: _url(f.path.name, NEW_QUERY) for f in fetches}

    results = await _run(list[Action](fetches), handler, settings, sleeps, refresh)
    assert [r.status for r in results] == ["downloaded"] * 3
    assert calls == [GAMEKEY]


async def test_refresh_missing_key_fails(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    async def refresh(gamekey: str) -> dict[FileKey, str]:
        return {}

    server = _Server({"/files/a.pdf": [_status(403)]})
    [result] = await _run([_fetch(tmp_path)], server, settings, sleeps, refresh)
    assert result.status == "failed"
    assert result.error == "refreshed order no longer lists this file"


async def test_still_403_after_refresh_fails(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path)

    async def refresh(gamekey: str) -> dict[FileKey, str]:
        return {fetch.key: _url("a.pdf", NEW_QUERY)}

    server = _Server({"/files/a.pdf": [_status(410)]})
    [result] = await _run([fetch], server, settings, sleeps, refresh)
    assert result.status == "failed"
    assert result.error == "download URL still rejected after refresh"
    assert len(server.requests) == 2


async def test_refresh_exception_fails_only_that_gamekey(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    bad = _fetch(tmp_path, "bad.pdf")
    good = _fetch(tmp_path, "good.pdf")

    async def refresh(gamekey: str) -> dict[FileKey, str]:
        raise RuntimeError(f"boom at {_url('x')}")

    server = _Server({"/files/bad.pdf": [_status(403)], "/files/good.pdf": [_ok()]})
    results = await _run([bad, good], server, settings, sleeps, refresh)
    assert [r.status for r in results] == ["failed", "downloaded"]
    assert results[0].error == f"refresh failed: boom at {BASE_URL}/x"


async def test_never_overwrites_completed_file(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    fetch = _fetch(tmp_path)
    fetch.path.parent.mkdir(parents=True)
    fetch.path.write_bytes(OLD_BODY)
    [result] = await _run([fetch], _server_for("a.pdf"), settings, sleeps)
    assert result.status == "failed"
    assert fetch.path.read_bytes() == OLD_BODY
    assert not fetch.path.with_name("a.pdf.part").exists()


def _archive_action(root: Path) -> ArchiveThenFetch:
    fetch = _fetch(root)
    fetch.path.parent.mkdir(parents=True)
    fetch.path.write_bytes(OLD_BODY)
    return ArchiveThenFetch(fetch.path, fetch.path.with_name(f"a.{OLD_MD5[:8]}.pdf"), fetch)


async def test_archive_then_fetch_success(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps, capsys: pytest.CaptureFixture[str]
) -> None:
    action = _archive_action(tmp_path)
    [result] = await _run([action], _server_for("a.pdf"), settings, sleeps)
    assert result == Result(action.existing, "archived_and_downloaded", len(BODY), BODY_MD5, None)
    assert action.archive_to.read_bytes() == OLD_BODY
    assert action.existing.read_bytes() == BODY
    events = {e["event"]: e for e in _log_events(capsys.readouterr().err)}
    assert events["file_archived"]["filename"] == action.archive_to.name
    assert events["file_downloaded"]["filename"] == "a.pdf"


async def test_archive_then_fetch_fetch_failure_leaves_existing(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    action = _archive_action(tmp_path)
    server = _Server({"/files/a.pdf": [_status(404)]})
    [result] = await _run([action], server, settings, sleeps)
    assert result.status == "failed"
    assert action.existing.read_bytes() == OLD_BODY
    assert not action.archive_to.exists()


async def test_archive_target_taken_fails_and_keeps_existing(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    action = _archive_action(tmp_path)
    action.archive_to.write_bytes(b"squatter")
    [result] = await _run([action], _server_for("a.pdf"), settings, sleeps)
    assert result.status == "failed"
    assert action.existing.read_bytes() == OLD_BODY
    assert action.archive_to.read_bytes() == b"squatter"
    assert not action.existing.with_name("a.pdf.part").exists()


async def test_concurrency_is_bounded(tmp_path: Path, settings: Settings, sleeps: _Sleeps) -> None:
    limit = settings.concurrency
    inflight = peak = 0
    reached, release = asyncio.Event(), asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        if inflight == limit:
            reached.set()
        await release.wait()
        inflight -= 1
        return httpx.Response(200, content=BODY)

    names = [f"f{i}.pdf" for i in range(20)]
    actions = list[Action](_fetch(tmp_path, n) for n in names)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        task = asyncio.create_task(execute(actions, client, _no_refresh, settings, sleep=sleeps))
        await reached.wait()
        for _ in range(20):
            await asyncio.sleep(0)
        assert inflight == limit
        release.set()
        results = await task
    assert peak == limit
    assert [r.status for r in results] == ["downloaded"] * 20


async def test_results_in_input_order_and_failures_isolated(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second, third = (_fetch(tmp_path, n) for n in ("a.pdf", "b.pdf", "c.pdf"))
    server = _Server(
        {"/files/a.pdf": [_ok()], "/files/b.pdf": [_status(404)], "/files/c.pdf": [_ok()]}
    )
    results = await _run([first, second, third], server, settings, sleeps)
    assert [(r.path, r.status) for r in results] == [
        (first.path, "downloaded"),
        (second.path, "failed"),
        (third.path, "downloaded"),
    ]
    events = _log_events(capsys.readouterr().err)
    failed = next(e for e in events if e["event"] == "file_failed")
    assert failed == {
        **failed,
        "bundle": "Bundle",
        "item": "Item",
        "filename": "b.pdf",
        "bytes": 0,
        "url": f"{BASE_URL}/b.pdf",
    }


async def test_download_logs_fields(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps, capsys: pytest.CaptureFixture[str]
) -> None:
    await _run([_fetch(tmp_path)], _server_for("a.pdf"), settings, sleeps)
    [event] = _log_events(capsys.readouterr().err)
    assert (event["event"], event["bundle"], event["item"], event["filename"]) == (
        "file_downloaded",
        "Bundle",
        "Item",
        "a.pdf",
    )
    assert event["bytes"] == len(BODY)
    assert event["url"] == f"{BASE_URL}/a.pdf"


def test_place_no_clobber_link_path(tmp_path: Path) -> None:
    src, dst = tmp_path / "s", tmp_path / "d"
    src.write_bytes(b"x")
    downloader._place_no_clobber(src, dst)  # pyright: ignore[reportPrivateUsage]
    assert dst.read_bytes() == b"x"
    assert not src.exists()


def test_place_no_clobber_existing_dst_raises(tmp_path: Path) -> None:
    src, dst = tmp_path / "s", tmp_path / "d"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        downloader._place_no_clobber(src, dst)  # pyright: ignore[reportPrivateUsage]
    assert dst.read_bytes() == b"old"
    assert src.read_bytes() == b"new"


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EXDEV])
def test_place_no_clobber_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def no_link(src: object, dst: object) -> None:
        raise OSError(code, "unsupported")

    monkeypatch.setattr(os, "link", no_link)
    src, dst = tmp_path / "s", tmp_path / "d"
    src.write_bytes(b"x")
    downloader._place_no_clobber(src, dst)  # pyright: ignore[reportPrivateUsage]
    assert dst.read_bytes() == b"x"
    assert not src.exists()


def test_place_no_clobber_fallback_refuses_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_link(src: object, dst: object) -> None:
        raise OSError(errno.EPERM, "unsupported")

    monkeypatch.setattr(os, "link", no_link)
    src, dst = tmp_path / "s", tmp_path / "d"
    src.write_bytes(b"new")
    dst.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        downloader._place_no_clobber(src, dst)  # pyright: ignore[reportPrivateUsage]
    assert dst.read_bytes() == b"old"


def test_place_no_clobber_other_oserror_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(src: object, dst: object) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(os, "link", broken)
    src = tmp_path / "s"
    src.write_bytes(b"x")
    with pytest.raises(OSError, match="io") as info:
        downloader._place_no_clobber(src, tmp_path / "d")  # pyright: ignore[reportPrivateUsage]
    assert info.value.errno == errno.EIO
    assert not (tmp_path / "d").exists()


async def test_unexpected_oserror_fails_only_that_file(
    tmp_path: Path, settings: Settings, sleeps: _Sleeps
) -> None:
    blocked = _fetch(tmp_path, "a.pdf")
    blocked.path.parent.parent.write_bytes(b"i am a file, not a directory")
    good = _fetch(tmp_path / "other", "b.pdf")
    results = await _run([blocked, good], _server_for("a.pdf", "b.pdf"), settings, sleeps)
    assert [r.status for r in results] == ["failed", "downloaded"]
    assert results[0].error is not None and results[0].error.startswith("NotADirectoryError")
