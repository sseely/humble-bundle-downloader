"""Pure download planner: decides every action from the library and a disk view.

The planner performs no I/O. The disk is observed only through an injected
``local`` callable, and the result is a data structure for the shell to execute.
It never plans a delete or an overwrite of a completed file.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from itertools import count
from pathlib import Path, PurePosixPath

from humble_dl.lib.hashcache import LocalFile
from humble_dl.lib.naming import (
    filename_from_url,
    item_dir_names,
    sanitize_component,
    versioned_name,
)
from humble_dl.models.library import FileVersion, Order, Subproduct, ebook_items

ARCHIVE_COUNTER_START = 1
VERSION_COUNTER_START = 2
COUNTER_SEPARATOR = "-"

type LocalLookup = Callable[[Path], LocalFile | None]


@dataclass(frozen=True)
class FileKey:
    """Identity of a remote file within the library."""

    gamekey: str
    machine_name: str
    filename: str


@dataclass(frozen=True)
class Fetch:
    """Download ``url`` to ``path``; ``path`` holds no completed file."""

    key: FileKey
    path: Path
    url: str
    md5: str | None
    size: int | None


@dataclass(frozen=True)
class ArchiveThenFetch:
    """Rename ``existing`` to ``archive_to``, then perform ``fetch`` to ``existing``."""

    existing: Path
    archive_to: Path
    fetch: Fetch


@dataclass(frozen=True)
class Skip:
    """The file at ``path`` is already complete."""

    path: Path


Action = Fetch | ArchiveThenFetch | Skip


@dataclass(frozen=True)
class Plan:
    """Ordered actions plus the total known bytes to download."""

    actions: list[Action]
    bytes_to_fetch: int


def _key(path: Path) -> str:
    """Case-insensitive identity of a path (default macOS volumes are)."""
    return str(path).casefold()


def _norm_md5(md5: str | None) -> str | None:
    return None if md5 is None else md5.casefold()


class _Claims:
    """Paths claimed so far in one plan() call, with the md5 each will hold.

    Scoped to a single plan() call and never shared, so plan() stays pure.
    """

    def __init__(self) -> None:
        self._md5_by_path: dict[str, str | None] = {}

    def is_claimed(self, path: Path) -> bool:
        return _key(path) in self._md5_by_path

    def claim(self, path: Path, md5: str | None) -> None:
        self._md5_by_path[_key(path)] = _norm_md5(md5)

    def holds(self, path: Path, md5: str | None) -> bool:
        """True when ``path`` is claimed with the same known md5."""
        claimed = self._md5_by_path[_key(path)]
        return claimed is not None and claimed == _norm_md5(md5)


def _counter_name(filename: str, number: int) -> str:
    path = PurePosixPath(filename)
    return f"{path.stem}{COUNTER_SEPARATOR}{number}{path.suffix}"


def _candidate_names(filename: str, md5: str | None) -> Iterator[str]:
    """Yield the name itself, then md5-versioned, then numbered fallbacks."""
    yield filename
    base = filename if md5 is None else versioned_name(filename, md5)
    if md5 is not None:
        yield base
    for number in count(VERSION_COUNTER_START):
        yield _counter_name(base, number)


def _is_complete(local: LocalFile, remote: FileVersion) -> bool:
    size_ok = remote.file_size is None or remote.file_size == local.size
    md5_ok = remote.md5 is None or remote.md5.casefold() == local.md5.casefold()
    return size_ok and md5_ok


class _Builder:
    """Accumulates actions for one plan() call."""

    def __init__(self, root: Path, local: LocalLookup) -> None:
        self._root = root
        self._local = local
        self._claims = _Claims()
        self.actions: list[Action] = []

    def add_order(self, order: Order) -> None:
        items = ebook_items(order)
        names = item_dir_names([(sub.human_name, sub.machine_name) for sub, _ in items])
        bundle_dir = self._root / sanitize_component(order.human_name)
        for sub, files in items:
            self._add_item(order.gamekey, bundle_dir / names[sub.machine_name], sub, files)

    def _add_item(
        self, gamekey: str, directory: Path, sub: Subproduct, files: list[FileVersion]
    ) -> None:
        for remote in files:
            filename = filename_from_url(remote.url)
            path = self._free_path(directory, filename, remote.md5)
            if path is not None:
                key = FileKey(gamekey, sub.machine_name, path.name)
                self.actions.append(self._decide(path, key, remote))

    def _free_path(self, directory: Path, filename: str, md5: str | None) -> Path | None:
        """Pick an unclaimed path for the file, or None if identical content is planned."""
        for name in _candidate_names(filename, md5):
            path = directory / name
            if not self._claims.is_claimed(path):
                return path
            if self._claims.holds(path, md5):
                return None
        raise AssertionError("unreachable: candidate names are unbounded")  # pragma: no cover

    def _decide(self, path: Path, key: FileKey, remote: FileVersion) -> Action:
        self._claims.claim(path, remote.md5)
        fetch = Fetch(key, path, remote.url, remote.md5, remote.file_size)
        existing = self._local(path)
        if existing is None:
            return fetch
        if _is_complete(existing, remote):
            return Skip(path)
        return ArchiveThenFetch(path, self._archive_target(path, existing), fetch)

    def _archive_target(self, path: Path, existing: LocalFile) -> Path:
        base = versioned_name(path.name, existing.md5)
        target = path.with_name(base)
        for number in count(ARCHIVE_COUNTER_START):
            if not self._claims.is_claimed(target) and self._local(target) is None:
                break
            target = path.with_name(_counter_name(base, number))
        self._claims.claim(target, existing.md5)
        return target


def _bytes_to_fetch(actions: list[Action]) -> int:
    total = 0
    for action in actions:
        if isinstance(action, Fetch):
            total += action.size or 0
        elif isinstance(action, ArchiveThenFetch):
            total += action.fetch.size or 0
    return total


def plan(orders: Sequence[Order], root: Path, local: LocalLookup) -> Plan:
    """Decide fetch, archive, and skip actions for every ebook file.

    No two actions write the same case-folded path, and no fetch targets a
    completed file that is not first archived.

    Args:
        orders: Parsed library orders.
        root: Output root directory.
        local: Read-only disk view returning size and md5, or None if absent.

    Returns:
        The ordered actions and total known bytes to download.
    """
    builder = _Builder(root, local)
    for order in orders:
        builder.add_order(order)
    return Plan(actions=builder.actions, bytes_to_fetch=_bytes_to_fetch(builder.actions))
