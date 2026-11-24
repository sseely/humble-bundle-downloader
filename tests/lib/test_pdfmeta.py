"""Tests for humble_dl.lib.pdfmeta."""

from pathlib import Path

import pytest

from humble_dl.lib import pdfmeta
from humble_dl.lib.pdfmeta import pdf_creation_year

INFO = b"<< /Producer (Acrobat) /CreationDate (D:20141215093000-05'00') >>"


def write(path: Path, body: bytes) -> Path:
    path.write_bytes(body)
    return path


def test_reads_year_near_the_start(tmp_path: Path) -> None:
    assert pdf_creation_year(write(tmp_path / "a.pdf", b"%PDF-1.4\n" + INFO)) == 2014


def test_reads_year_in_the_tail_of_a_large_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdfmeta, "SCAN_BYTES", 64)
    body = b"%PDF-1.4\n" + b"x" * 500 + INFO
    assert pdf_creation_year(write(tmp_path / "b.pdf", body)) == 2014


def test_missing_date_or_file_is_none(tmp_path: Path) -> None:
    assert pdf_creation_year(write(tmp_path / "c.pdf", b"%PDF-1.4\n<< >>")) is None
    assert pdf_creation_year(tmp_path / "absent.pdf") is None


def test_xmp_dates_count_and_the_latest_year_wins(tmp_path: Path) -> None:
    body = (
        b"%PDF-1.7\n<x:xmpmeta><xmp:CreateDate>2008-01-02T00:00:00</xmp:CreateDate>"
        b'<rdf:Description xmp:CreateDate="2012-06-14T15:06:40-04:00"/></x:xmpmeta>'
    )
    assert pdf_creation_year(write(tmp_path / "d.pdf", body)) == 2012
