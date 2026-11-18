"""mtime-keyed md5 cache for completed files.

The cache is an optimization only: disk is the source of truth, and deleting
the cache file only costs re-hashing time.
"""

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Self, cast

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
KEY_ENTRIES = "entries"
KEY_PATH = "path"
KEY_SIZE = "size"
KEY_MTIME_NS = "mtime_ns"
KEY_MD5 = "md5"

type CacheKey = tuple[str, int, int]
type Opener = Callable[[Path], BinaryIO]


def _open_binary(path: Path) -> BinaryIO:
    return path.open("rb")


@dataclass(frozen=True)
class LocalFile:
    """A file on disk with its size and md5 digest.

    Attributes:
        size: File size in bytes.
        md5: Lowercase hex md5 digest of the file contents.
    """

    size: int
    md5: str


def _key_for(path: Path) -> CacheKey:
    stat = path.stat()
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _hash_stream(stream: BinaryIO) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    while chunk := stream.read(CHUNK_SIZE):
        digest.update(chunk)
    return digest.hexdigest()


def _parse_entry(raw: object) -> tuple[CacheKey, str]:
    if not isinstance(raw, dict):
        raise ValueError("cache entry is not an object")
    fields = cast("dict[str, object]", raw)
    path, size = fields.get(KEY_PATH), fields.get(KEY_SIZE)
    mtime_ns, md5 = fields.get(KEY_MTIME_NS), fields.get(KEY_MD5)
    if not (
        isinstance(path, str)
        and isinstance(size, int)
        and isinstance(mtime_ns, int)
        and isinstance(md5, str)
    ):
        raise ValueError("cache entry has missing or mistyped fields")
    return (path, size, mtime_ns), md5


def _parse_cache(text: str) -> dict[CacheKey, str]:
    data: object = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("cache root is not an object")
    entries = cast("dict[str, object]", data).get(KEY_ENTRIES)
    if not isinstance(entries, list):
        raise ValueError("cache entries is not a list")
    return dict(_parse_entry(entry) for entry in cast("list[object]", entries))


class HashCache:
    """Cache of md5 digests keyed by (resolved path, size, mtime_ns)."""

    def __init__(
        self,
        path: Path,
        entries: dict[CacheKey, str] | None = None,
        opener: Opener = _open_binary,
    ) -> None:
        """Create a cache.

        Args:
            path: Location of the JSON cache file used by `save`.
            entries: Initial cache entries.
            opener: Opens a file for binary reading; injectable for tests.
        """
        self._path = path
        self._entries: dict[CacheKey, str] = dict(entries or {})
        self._opener = opener

    @classmethod
    def load(cls, path: Path, opener: Opener = _open_binary) -> Self:
        """Load a cache from disk; a missing or corrupt file yields an empty cache.

        Args:
            path: JSON cache file location.
            opener: Opens a file for binary reading; injectable for tests.

        Returns:
            The loaded cache.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path, opener=opener)
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("hash cache unreadable, starting empty: %s", type(exc).__name__)
            return cls(path, opener=opener)
        try:
            entries = _parse_cache(text)
        except ValueError as exc:
            logger.warning("hash cache corrupt, starting empty: %s", type(exc).__name__)
            return cls(path, opener=opener)
        return cls(path, entries, opener)

    def md5_for(self, path: Path) -> str:
        """Return the md5 of a file, using the cache when size and mtime match.

        Args:
            path: File to hash; must exist.

        Returns:
            Lowercase hex md5 digest.
        """
        key = _key_for(path)
        cached = self._entries.get(key)
        if cached is not None:
            return cached
        with self._opener(path) as stream:
            md5 = _hash_stream(stream)
        self._entries[key] = md5
        return md5

    def local_file(self, path: Path) -> LocalFile | None:
        """Describe a local file.

        Args:
            path: File to inspect.

        Returns:
            Size and md5, or None if the file does not exist.
        """
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return None
        return LocalFile(size=size, md5=self.md5_for(path))

    def record(self, path: Path, md5: str) -> None:
        """Record a known md5 for a file using its current stat, without reading it.

        Args:
            path: File that exists on disk.
            md5: Digest to associate with the file's current size and mtime.
        """
        self._entries[_key_for(path)] = md5

    def save(self) -> None:
        """Atomically write the cache to disk, creating the parent directory."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            KEY_ENTRIES: [
                {KEY_PATH: p, KEY_SIZE: size, KEY_MTIME_NS: mtime_ns, KEY_MD5: md5}
                for (p, size, mtime_ns), md5 in sorted(self._entries.items())
            ]
        }
        fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=f".{self._path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp_name, self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
