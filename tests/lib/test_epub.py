"""Tests for read-only EPUB title extraction."""

import zipfile
from pathlib import Path

from humble_dl.lib import epub
from humble_dl.lib.epub import read_epub_title
from tests.helpers.books import CONTAINER_XML, OPF_PATH, make_epub


def test_reads_title_stripped(tmp_path: Path) -> None:
    path = make_epub(tmp_path / "a.epub", "  Old Man's War \n")
    assert read_epub_title(path) == "Old Man's War"


def test_first_title_wins(tmp_path: Path) -> None:
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>First</dc:title>'
        "<dc:title>Second</dc:title></metadata></package>"
    )
    assert read_epub_title(make_epub(tmp_path / "a.epub", opf=opf)) == "First"


def test_missing_container_is_none(tmp_path: Path) -> None:
    assert read_epub_title(make_epub(tmp_path / "a.epub", container=None)) is None


def test_missing_title_is_none(tmp_path: Path) -> None:
    assert read_epub_title(make_epub(tmp_path / "a.epub", None)) is None


def test_blank_title_is_none(tmp_path: Path) -> None:
    assert read_epub_title(make_epub(tmp_path / "a.epub", "   ")) is None


def test_container_without_rootfile_is_none(tmp_path: Path) -> None:
    container = '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"/>'
    assert read_epub_title(make_epub(tmp_path / "a.epub", container=container)) is None


def test_missing_opf_member_is_none(tmp_path: Path) -> None:
    path = tmp_path / "a.epub"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", CONTAINER_XML)
    assert read_epub_title(path) is None


def test_oversized_member_is_none(tmp_path: Path) -> None:
    padding = "<!--" + "x" * epub.MAX_MEMBER_BYTES + "-->"
    opf = f'<package xmlns="http://www.idpf.org/2007/opf">{padding}</package>'
    path = make_epub(tmp_path / "a.epub", opf=opf)
    assert read_epub_title(path) is None
    with zipfile.ZipFile(path) as archive:
        assert archive.getinfo(OPF_PATH).file_size > epub.MAX_MEMBER_BYTES


def test_oversized_container_is_none(tmp_path: Path) -> None:
    padding = "<!--" + "x" * epub.MAX_MEMBER_BYTES + "-->"
    container = f"<container>{padding}</container>"
    assert read_epub_title(make_epub(tmp_path / "a.epub", container=container)) is None


def test_not_a_zip_is_none(tmp_path: Path) -> None:
    path = tmp_path / "a.epub"
    path.write_text("not a zip")
    assert read_epub_title(path) is None


def test_malformed_xml_is_none(tmp_path: Path) -> None:
    assert read_epub_title(make_epub(tmp_path / "a.epub", opf="<package")) is None


def test_directory_is_none(tmp_path: Path) -> None:
    assert read_epub_title(tmp_path) is None


def test_file_is_not_modified(tmp_path: Path) -> None:
    path = make_epub(tmp_path / "a.epub")
    before = path.read_bytes()
    read_epub_title(path)
    assert path.read_bytes() == before
