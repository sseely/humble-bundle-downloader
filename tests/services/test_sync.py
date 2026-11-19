"""Tests for sync orchestration."""

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from humble_dl.api.client import LibraryApiError
from humble_dl.config import Settings, load_settings
from humble_dl.lib.hashcache import HashCache
from humble_dl.models.library import BundleRef, Download, FileVersion, Order, Subproduct
from humble_dl.services.planner import ArchiveThenFetch, Fetch, FileKey, Skip
from humble_dl.services.sync import (
    InsufficientSpaceError,
    Summary,
    SyncDeps,
    SyncOptions,
    filter_bundles,
    make_refresh,
    match_refreshed,
    nearest_existing,
    remote_ids,
    run_sync,
    summarize,
)
from humble_dl.workers.downloader import Result

BASE = "https://dl.example.test/files"
BIG = 10**12


def md5_of(body: bytes) -> str:
    return hashlib.md5(body, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Spec:
    """One remote file: filename, body and url query."""

    filename: str
    body: bytes
    query: str = "sig=a"

    @property
    def url(self) -> str:
        return f"{BASE}/{self.filename}?{self.query}"


def make_order(gamekey: str, title: str, specs: list[Spec], machine: str = "book") -> Order:
    files = [
        FileVersion(label="EPUB", url=s.url, md5=md5_of(s.body), file_size=len(s.body))
        for s in specs
    ]
    sub = Subproduct(
        machine_name=machine,
        human_name="Book",
        downloads=[Download(platform="ebook", files=files)],
    )
    return Order(gamekey=gamekey, human_name=title, subproducts=[sub])


@dataclass
class FakeLibrary:
    orders: dict[str, Order]
    refreshed: dict[str, Order] = field(default_factory=lambda: dict[str, Order]())
    get_calls: list[str] = field(default_factory=lambda: list[str]())
    refresh_calls: list[str] = field(default_factory=lambda: list[str]())

    async def list_bundles(self) -> list[BundleRef]:
        return [BundleRef(id=k, title=o.human_name) for k, o in self.orders.items()]

    async def get_order(self, bundle_id: str) -> tuple[Order, list[str]]:
        self.get_calls.append(bundle_id)
        return self.orders[bundle_id], []

    async def refresh(self, bundle_id: str) -> Order:
        self.refresh_calls.append(bundle_id)
        return self.refreshed[bundle_id]


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings({}, tmp_path / "home", output_override=tmp_path / "out")


@pytest.fixture
def cache(tmp_path: Path) -> HashCache:
    return HashCache(tmp_path / "cache.json")


def deps(
    library: FakeLibrary, client: httpx.AsyncClient, cache: HashCache, free: int = BIG
) -> SyncDeps:
    return SyncDeps(
        library=library, http=client, cache=cache, free_bytes=lambda _: free, sleep=no_sleep
    )


ALPHA = Spec("alpha.epub", b"alpha body")
BETA = Spec("beta.pdf", b"beta body!")


def two_bundles() -> FakeLibrary:
    return FakeLibrary(
        {
            "k1": make_order("k1", "Python Bundle", [ALPHA], "alpha"),
            "k2": make_order("k2", "Cooking Bundle", [BETA], "beta"),
        }
    )


async def test_second_run_downloads_nothing(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=ALPHA.url, content=ALPHA.body)
    httpx_mock.add_response(url=BETA.url, content=BETA.body)
    opts = SyncOptions(dry_run=False, only=None)
    async with httpx.AsyncClient() as client:
        first = await run_sync(settings, opts, deps(two_bundles(), client, cache))
        second = await run_sync(settings, opts, deps(two_bundles(), client, cache))
    assert (first.planned, first.downloaded, first.skipped, first.failed) == (2, 2, 0, 0)
    assert first.bytes == len(ALPHA.body) + len(BETA.body)
    assert (second.downloaded, second.skipped, second.planned) == (0, 2, 2)
    assert (
        settings.output_dir / "Python Bundle" / "Book" / "alpha.epub"
    ).read_bytes() == ALPHA.body
    assert (
        cache.local_file(settings.output_dir / "Cooking Bundle" / "Book" / "beta.pdf") is not None
    )


async def test_insufficient_space_creates_nothing(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    probed: list[Path] = []

    def free(path: Path) -> int:
        probed.append(path)
        return 5

    d = SyncDeps(library=two_bundles(), http=httpx.AsyncClient(), cache=cache, free_bytes=free)
    async with d.http:
        with pytest.raises(InsufficientSpaceError, match=r"need 20 bytes, only 5 bytes free"):
            await run_sync(settings, SyncOptions(False, None), d)
    assert probed == [settings.output_dir.parent]
    assert not settings.output_dir.exists()
    assert httpx_mock.get_requests() == []


async def test_dry_run_writes_and_requests_nothing(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    async with httpx.AsyncClient() as client:
        summary = await run_sync(
            settings, SyncOptions(dry_run=True, only=None), deps(two_bundles(), client, cache)
        )
    assert (summary.planned, summary.downloaded, summary.skipped) == (2, 0, 0)
    assert all(isinstance(a, Fetch) for a in summary.actions)
    assert not settings.output_dir.exists()
    assert not settings.hash_cache_path.exists()
    assert httpx_mock.get_requests() == []


async def test_dry_run_counts_skips(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    target = settings.output_dir / "Python Bundle" / "Book"
    target.mkdir(parents=True)
    (target / "alpha.epub").write_bytes(ALPHA.body)
    async with httpx.AsyncClient() as client:
        summary = await run_sync(
            settings, SyncOptions(True, None), deps(two_bundles(), client, cache)
        )
    assert summary.skipped == 1
    assert [type(a) for a in summary.actions] == [Skip, Fetch]
    assert httpx_mock.get_requests() == []


async def test_only_filters_before_loading_orders(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=ALPHA.url, content=ALPHA.body)
    library = two_bundles()
    async with httpx.AsyncClient() as client:
        summary = await run_sync(
            settings, SyncOptions(False, "pYTHON"), deps(library, client, cache)
        )
    assert library.get_calls == ["k1"]
    assert summary.planned == 1


async def test_versioned_name_refreshes_by_remote_filename(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    v1 = Spec("book.epub", b"first edition")
    v2 = Spec("book.epub", b"second edition", query="sig=old")
    v2_new = Spec("book.epub", v2.body, query="sig=new")
    versioned = f"book.{md5_of(v2.body)[:8]}.epub"
    library = FakeLibrary(
        {"k1": make_order("k1", "Bundle", [v1, v2])},
        refreshed={"k1": make_order("k1", "Bundle", [v1, v2_new])},
    )
    httpx_mock.add_response(url=v1.url, content=v1.body)
    httpx_mock.add_response(url=v2.url, status_code=403)
    httpx_mock.add_response(url=v2_new.url, content=v2.body)
    async with httpx.AsyncClient() as client:
        summary = await run_sync(settings, SyncOptions(False, None), deps(library, client, cache))
    item_dir = settings.output_dir / "Bundle" / "Book"
    assert summary.failed == 0
    assert (item_dir / versioned).read_bytes() == v2.body
    assert library.refresh_calls == ["k1"]
    assert [r.url.query for r in httpx_mock.get_requests()].count(b"sig=new") == 1


async def test_failure_reported_and_other_files_continue(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=ALPHA.url, status_code=404)
    httpx_mock.add_response(url=BETA.url, content=BETA.body)
    async with httpx.AsyncClient() as client:
        summary = await run_sync(
            settings, SyncOptions(False, None), deps(two_bundles(), client, cache)
        )
    assert (summary.downloaded, summary.failed) == (1, 1)
    assert summary.failures == [settings.output_dir / "Python Bundle" / "Book" / "alpha.epub"]


@dataclass
class FlakyLibrary(FakeLibrary):
    """get_order fails for chosen bundles, as the client does after retries."""

    failing: frozenset[str] = frozenset()

    async def get_order(self, bundle_id: str) -> tuple[Order, list[str]]:
        if bundle_id in self.failing:
            raise LibraryApiError("signed-URL request for /home/library/x failed: upstream_error")
        return await super().get_order(bundle_id)


async def test_bundle_failure_is_isolated_and_reported(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock
) -> None:
    httpx_mock.add_response(url=ALPHA.url, content=ALPHA.body)
    library = FlakyLibrary(two_bundles().orders, failing=frozenset({"k2"}))
    async with httpx.AsyncClient() as client:
        summary = await run_sync(settings, SyncOptions(False, None), deps(library, client, cache))

    assert summary.failed_bundles == ["Cooking Bundle"]
    assert (summary.planned, summary.downloaded, summary.failed) == (1, 1, 0)
    assert (
        settings.output_dir / "Python Bundle" / "Book" / "alpha.epub"
    ).read_bytes() == ALPHA.body


def test_filter_bundles() -> None:
    bundles = [BundleRef(id="1", title="Python Pack"), BundleRef(id="2", title="Rust")]
    assert filter_bundles(bundles, None) == bundles
    assert filter_bundles(bundles, "PYTHON") == bundles[:1]
    assert filter_bundles(bundles, "zzz") == []


def test_match_refreshed_omits_unmatched() -> None:
    ours = Spec("a.epub", b"a")
    gone = Spec("gone.epub", b"g")
    key_a = FileKey("k", "book", "a.1234.epub")
    key_gone = FileKey("k", "book", "gone.epub")
    key_other = FileKey("other", "book", "a.epub")
    fetches = [
        Fetch(key_a, Path("x"), ours.url, md5_of(b"a"), 1),
        Fetch(key_gone, Path("y"), gone.url, md5_of(b"g"), 1),
        Fetch(key_other, Path("z"), ours.url, md5_of(b"a"), 1),
    ]
    recorded = remote_ids(fetches)
    fresh = make_order("k", "T", [Spec("a.epub", b"a", query="sig=new")])
    assert match_refreshed(fresh, recorded) == {key_a: f"{BASE}/a.epub?sig=new"}


def test_remote_ids_ignores_skips_and_unwraps_archive() -> None:
    key = FileKey("k", "m", "book.1234.epub")
    fetch = Fetch(key, Path("p"), f"{BASE}/book.epub?s=1", "ABC", None)
    archive = ArchiveThenFetch(Path("p"), Path("q"), fetch)
    assert remote_ids([Skip(Path("s")), archive]) == {key: ("m", "book.epub", "abc")}


def test_nearest_existing(tmp_path: Path) -> None:
    assert nearest_existing(tmp_path / "a" / "b") == tmp_path
    assert nearest_existing(tmp_path) == tmp_path


def test_nearest_existing_stops_at_root() -> None:
    assert nearest_existing(Path("/")) == Path("/")


def test_summarize_counts_each_status() -> None:
    p = Path("f")
    results = [
        Result(p, "skipped", 0, None, None),
        Result(p, "downloaded", 10, "m", None),
        Result(p, "archived_and_downloaded", 5, "m", None),
        Result(Path("bad"), "failed", 0, None, "boom"),
    ]
    summary = summarize([Skip(p)] * 4, results, 1.5)
    assert summary == Summary(
        planned=4,
        skipped=1,
        downloaded=2,
        failed=1,
        archived=1,
        bytes=15,
        duration_s=1.5,
        failures=[Path("bad")],
        actions=[Skip(p)] * 4,
    )


async def test_missing_signed_urls_are_reported(
    settings: Settings, cache: HashCache, httpx_mock: HTTPXMock, capsys: pytest.CaptureFixture[str]
) -> None:
    class Missing(FakeLibrary):
        async def get_order(self, bundle_id: str) -> tuple[Order, list[str]]:
            order, _ = await super().get_order(bundle_id)
            return order, ["lost.epub"]

    httpx_mock.add_response(url=ALPHA.url, content=ALPHA.body)
    library = Missing({"k1": make_order("k1", "Python Bundle", [ALPHA], "alpha")})
    async with httpx.AsyncClient() as client:
        summary = await run_sync(settings, SyncOptions(False, None), deps(library, client, cache))
    # Files with no signed URL are reported, never silently dropped.
    assert summary.unsigned_files == ["Python Bundle/lost.epub"]
    assert '"unsigned_files": 1' in capsys.readouterr().err


@dataclass
class OverlapLibrary(FakeLibrary):
    """Records how many refreshes run at once (the client shares one page)."""

    in_flight: int = 0
    max_in_flight: int = 0

    async def refresh(self, bundle_id: str) -> Order:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.in_flight -= 1
        return await super().refresh(bundle_id)


async def test_refreshes_for_different_bundles_never_overlap() -> None:
    library = two_bundles()
    overlap = OverlapLibrary(library.orders, refreshed=dict(library.orders))
    refresh = make_refresh(overlap, {})

    await asyncio.gather(refresh("k1"), refresh("k2"))

    assert overlap.max_in_flight == 1
    assert sorted(overlap.refresh_calls) == ["k1", "k2"]
