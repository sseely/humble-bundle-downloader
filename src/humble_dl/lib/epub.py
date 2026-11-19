"""Read-only access to EPUB metadata (title only)."""

import xml.etree.ElementTree as ET  # noqa: S405  (members are size-capped, see below)
import zipfile
import zlib
from pathlib import Path

from humble_dl.lib.logging import FILE_NAME_KEY, get_logger

CONTAINER_PATH = "META-INF/container.xml"
MAX_MEMBER_BYTES = 1024 * 1024
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"
_ROOTFILE_XPATH = f".//{_CONTAINER_NS}rootfile"
_TITLE_XPATH = f".//{_DC_NS}title"
_FULL_PATH_ATTR = "full-path"

_UNREADABLE = (
    zipfile.BadZipFile,
    zlib.error,
    ET.ParseError,
    KeyError,
    ValueError,  # includes UnicodeError
    RuntimeError,  # encrypted members
    NotImplementedError,  # unsupported compression
    EOFError,
    OSError,
)

_log = get_logger(__name__)


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    """Read a member, or None when it exceeds ``MAX_MEMBER_BYTES``."""
    info = archive.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        return None
    with archive.open(info) as handle:
        data = handle.read(MAX_MEMBER_BYTES + 1)
    return None if len(data) > MAX_MEMBER_BYTES else data


def _opf_path(archive: zipfile.ZipFile) -> str | None:
    container = _read_member(archive, CONTAINER_PATH)
    if container is None:
        return None
    rootfile = ET.fromstring(container).find(_ROOTFILE_XPATH)  # noqa: S314
    return rootfile.get(_FULL_PATH_ATTR) if rootfile is not None else None


def _opf_title(archive: zipfile.ZipFile, opf_path: str) -> str | None:
    opf = _read_member(archive, opf_path)
    if opf is None:
        return None
    title = ET.fromstring(opf).find(_TITLE_XPATH)  # noqa: S314
    text = (title.text or "").strip() if title is not None else ""
    return text or None


def read_epub_title(path: Path) -> str | None:
    """Return the first ``dc:title`` of an EPUB, or None when it cannot be read.

    The file is opened read-only as a zip. Each XML member is capped at 1 MiB.
    Never raises for a malformed archive; the reason is logged at debug level.

    Args:
        path: The ``.epub`` file.

    Returns:
        The stripped title text, or None if absent or unreadable.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            opf_path = _opf_path(archive)
            return _opf_title(archive, opf_path) if opf_path else None
    except _UNREADABLE as exc:
        _log.debug(
            "epub_title_unreadable",
            extra={FILE_NAME_KEY: path.name, "reason": type(exc).__name__},
        )
        return None
