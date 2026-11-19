"""Shared builders for Books/EPUB tests."""

import zipfile
from collections.abc import Sequence
from pathlib import Path

OPF_PATH = "OEBPS/content.opf"
CONTAINER_XML = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    f'<rootfile full-path="{OPF_PATH}" media-type="application/oebps-package+xml"/>'
    "</rootfiles></container>"
)
OPF_TEMPLATE = (
    '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{title}</metadata></package>'
)


def make_epub(
    path: Path,
    title: str | None = "A Title",
    *,
    container: str | None = CONTAINER_XML,
    opf: str | None = None,
) -> Path:
    """Write a tiny EPUB (zip) with the given title; pass ``container=None`` to omit it."""
    title_xml = "" if title is None else f"<dc:title>{title}</dc:title>"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        if container is not None:
            archive.writestr("META-INF/container.xml", container)
        archive.writestr(OPF_PATH, opf if opf is not None else OPF_TEMPLATE.format(title=title_xml))
    return path


def make_library(library: Path, files: Sequence[str] = (), dirs: Sequence[str] = ()) -> Path:
    """Create a fake Books library directory with the given file and directory entries."""
    library.mkdir(parents=True, exist_ok=True)
    for name in files:
        (library / name).write_text("x")
    for name in dirs:
        (library / name).mkdir()
    return library
