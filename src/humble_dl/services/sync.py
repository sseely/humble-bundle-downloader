"""Sync orchestration: the thin imperative shell composing library, planner, downloader.

Decisions live in the pure planner; this module only sequences effects and holds
small pure helpers for filtering, refresh mapping and summarising.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from humble_dl.config import Settings
from humble_dl.lib.hashcache import HashCache
from humble_dl.lib.logging import get_logger
from humble_dl.lib.naming import filename_from_url
from humble_dl.models.library import BundleRef, Order, ebook_items
from humble_dl.services.planner import Action, ArchiveThenFetch, Fetch, FileKey, Skip, plan
from humble_dl.workers.downloader import Result, execute

EVENT_STARTED = "sync_started"
EVENT_COMPLETE = "sync_complete"
EVENT_MISSING_URLS = "signed_urls_missing"
STATUS_DOWNLOADED = "downloaded"
STATUS_ARCHIVED = "archived_and_downloaded"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

type RemoteId = tuple[str, str, str | None]
type FreeBytes = Callable[[Path], int]
type Sleep = Callable[[float], Awaitable[None]]
type Clock = Callable[[], float]

_log = get_logger(__name__)


class InsufficientSpaceError(Exception):
    """The output volume cannot hold the planned downloads."""


class LibraryLike(Protocol):
    """The slice of ``LibraryClient`` that sync uses."""

    async def list_bundles(self) -> list[BundleRef]: ...
    async def get_order(self, bundle_id: str) -> tuple[Order, list[str]]: ...
    async def refresh(self, bundle_id: str) -> Order: ...


@dataclass(frozen=True)
class SyncOptions:
    """User options for one sync run.

    Attributes:
        dry_run: Plan only; create no files and make no HTTP requests.
        only: Case-insensitive substring of the bundle title to restrict to.
    """

    dry_run: bool
    only: str | None


@dataclass(frozen=True)
class SyncDeps:
    """Injected collaborators for ``run_sync``."""

    library: LibraryLike
    http: httpx.AsyncClient
    cache: HashCache
    free_bytes: FreeBytes
    sleep: Sleep = asyncio.sleep
    clock: Clock = time.monotonic


@dataclass(frozen=True)
class Summary:
    """Outcome of a sync run.

    ``downloaded`` counts every file written (including archived-and-replaced
    ones); ``archived`` counts the subset whose old version was archived. For a
    dry run only ``planned``, ``skipped`` and ``actions`` are populated.
    """

    planned: int
    skipped: int
    downloaded: int
    failed: int
    archived: int
    bytes: int
    duration_s: float
    failures: list[Path]
    actions: list[Action] = field(default_factory=lambda: list[Action]())


def filter_bundles(bundles: Iterable[BundleRef], only: str | None) -> list[BundleRef]:
    """Keep bundles whose title contains ``only`` (case-insensitive); all if None."""
    if only is None:
        return list(bundles)
    needle = only.casefold()
    return [b for b in bundles if needle in b.title.casefold()]


def fetch_of(action: Action) -> Fetch | None:
    """Return the fetch an action performs, or None for a skip."""
    if isinstance(action, ArchiveThenFetch):
        return action.fetch
    if isinstance(action, Fetch):
        return action
    return None


def _md5(md5: str | None) -> str | None:
    return None if md5 is None else md5.casefold()


def remote_ids(actions: Sequence[Action]) -> dict[FileKey, RemoteId]:
    """Map each fetch's on-disk key to ``(machine_name, remote filename, md5)``.

    The md5 disambiguates versions that share a remote filename.
    """
    ids: dict[FileKey, RemoteId] = {}
    for action in actions:
        fetch = fetch_of(action)
        if fetch is not None:
            ids[fetch.key] = (fetch.key.machine_name, filename_from_url(fetch.url), _md5(fetch.md5))
    return ids


def match_refreshed(order: Order, recorded: dict[FileKey, RemoteId]) -> dict[FileKey, str]:
    """Find fresh URLs in ``order`` for the recorded keys of its gamekey.

    Keys with no ``(machine_name, remote filename, md5)`` match are omitted.
    """
    fresh: dict[RemoteId, str] = {}
    for sub, files in ebook_items(order):
        for remote in files:
            fresh.setdefault(
                (sub.machine_name, filename_from_url(remote.url), _md5(remote.md5)), remote.url
            )
    return {
        key: fresh[rid]
        for key, rid in recorded.items()
        if key.gamekey == order.gamekey and rid in fresh
    }


def nearest_existing(path: Path) -> Path:
    """Return ``path`` or its closest ancestor that exists."""
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def summarize(actions: list[Action], results: Sequence[Result], duration_s: float) -> Summary:
    """Fold executor results into a ``Summary`` (pure)."""
    statuses = [r.status for r in results]
    return Summary(
        planned=len(actions),
        skipped=statuses.count(STATUS_SKIPPED),
        downloaded=statuses.count(STATUS_DOWNLOADED) + statuses.count(STATUS_ARCHIVED),
        failed=statuses.count(STATUS_FAILED),
        archived=statuses.count(STATUS_ARCHIVED),
        bytes=sum(r.bytes for r in results),
        duration_s=duration_s,
        failures=[r.path for r in results if r.status == STATUS_FAILED],
        actions=actions,
    )


def _dry_summary(actions: list[Action], duration_s: float) -> Summary:
    skipped = sum(isinstance(a, Skip) for a in actions)
    return Summary(len(actions), skipped, 0, 0, 0, 0, duration_s, [], actions)


async def _load_orders(deps: SyncDeps, opts: SyncOptions) -> list[Order]:
    orders: list[Order] = []
    for bundle in filter_bundles(await deps.library.list_bundles(), opts.only):
        order, missing = await deps.library.get_order(bundle.id)
        if missing:
            _log.warning(EVENT_MISSING_URLS, extra={"count": len(missing)})
        orders.append(order)
    return orders


def make_refresh(
    library: LibraryLike, recorded: dict[FileKey, RemoteId]
) -> Callable[[str], Awaitable[dict[FileKey, str]]]:
    """Build the downloader's refresh callback.

    The library client drives a single browser page, so refreshes for
    different bundles are serialized with one lock (the downloader only
    serializes per gamekey).
    """
    lock = asyncio.Lock()

    async def refresh(gamekey: str) -> dict[FileKey, str]:
        async with lock:
            order = await library.refresh(gamekey)
        return match_refreshed(order, recorded)

    return refresh


def _check_space(needed: int, root: Path, free_bytes: FreeBytes) -> None:
    free = free_bytes(nearest_existing(root))
    if needed > free:
        raise InsufficientSpaceError(
            f"Not enough disk space: need {needed} bytes, only {free} bytes free."
        )


def _record_hashes(cache: HashCache, results: Sequence[Result]) -> None:
    for result in results:
        if result.status in (STATUS_DOWNLOADED, STATUS_ARCHIVED) and result.md5 is not None:
            cache.record(result.path, result.md5)
    cache.save()


def _log_complete(summary: Summary) -> None:
    _log.info(
        EVENT_COMPLETE,
        extra={
            "planned": summary.planned,
            "skipped": summary.skipped,
            "downloaded": summary.downloaded,
            "failed": summary.failed,
            "archived": summary.archived,
            "bytes": summary.bytes,
            "duration_s": summary.duration_s,
        },
    )


async def run_sync(settings: Settings, opts: SyncOptions, deps: SyncDeps) -> Summary:
    """Plan and execute a library sync.

    Args:
        settings: Output directory, concurrency and timeouts.
        opts: Dry-run and bundle filter options.
        deps: Library, HTTP client, hash cache and free-space probe.

    Returns:
        The run ``Summary`` (``actions`` holds the plan, for dry-run reporting).

    Raises:
        InsufficientSpaceError: Planned bytes exceed free space; nothing was written.
    """
    started = deps.clock()
    _log.info(
        EVENT_STARTED,
        extra={"output_dir": str(settings.output_dir), "dry_run": opts.dry_run, "only": opts.only},
    )
    orders = await _load_orders(deps, opts)
    plan_ = plan(orders, settings.output_dir, deps.cache.local_file)
    _check_space(plan_.bytes_to_fetch, settings.output_dir, deps.free_bytes)
    if opts.dry_run:
        summary = _dry_summary(plan_.actions, deps.clock() - started)
    else:
        refresh = make_refresh(deps.library, remote_ids(plan_.actions))
        results = await execute(plan_.actions, deps.http, refresh, settings, sleep=deps.sleep)
        _record_hashes(deps.cache, results)
        summary = summarize(plan_.actions, results, deps.clock() - started)
    _log_complete(summary)
    return summary
