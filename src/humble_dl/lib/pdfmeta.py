"""Read a PDF's creation year from its metadata without a PDF library."""

import re
from pathlib import Path

SCAN_BYTES = 1 << 20  # document metadata sits near the start or the end
# Info dictionary date or XMP CreateDate (element or attribute form).
_CREATED = re.compile(rb'(?:/CreationDate\s*\(D:|<xmp:CreateDate>|xmp:CreateDate=")(\d{4})')


def pdf_creation_year(path: Path) -> int | None:
    """The latest creation year in the first and last ``SCAN_BYTES``, or None.

    Embedded images carry their own (older) XMP dates; the document itself is
    created after them, so the latest year is the document's.
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(SCAN_BYTES)
            size = handle.seek(0, 2)
            handle.seek(max(size - SCAN_BYTES, 0))
            tail = handle.read(SCAN_BYTES)
    except FileNotFoundError:
        return None
    years = [int(y) for y in _CREATED.findall(head) + _CREATED.findall(tail)]
    return max(years, default=None)
