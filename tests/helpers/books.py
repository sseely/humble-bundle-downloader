"""Shared builders for Books/EPUB tests."""

import zipfile
from collections.abc import Sequence
from pathlib import Path

from humble_dl.services.books import CONFIRM_POLL_S, CONFIRM_TIMEOUT_S, OPEN_COMMAND, BooksDeps

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


class FakeBooks:
    """Stands in for Apple Books and its clock; never launches anything.

    A successful ``open`` adds a library entry named after each file, like Books
    copying the file into its iCloud library. Calls are numbered from 1:
    ``fail_on`` calls exit non-zero, ``drop_on`` calls exit 0 but add nothing
    (the silent drop Books does with batched opens). ``sleep`` advances the clock.
    """

    def __init__(
        self,
        library: Path,
        fail_on: frozenset[int] = frozenset(),
        drop_on: frozenset[int] = frozenset(),
    ) -> None:
        library.mkdir(parents=True, exist_ok=True)
        self.library = library
        self.fail_on = fail_on
        self.drop_on = drop_on
        self.calls: list[list[str]] = []
        self.sleeps: list[float] = []
        self.now = 0.0

    def __call__(self, argv: Sequence[str]) -> bool:
        self.calls.append(list(argv))
        number = len(self.calls)
        if number in self.fail_on:
            return False
        if number not in self.drop_on:
            for arg in argv[len(OPEN_COMMAND) :]:
                (self.library / Path(arg).name).mkdir(exist_ok=True)
        return True

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        return self.now

    def forget(self) -> None:
        """Empty the library, as if every book were removed from Books."""
        for entry in self.library.iterdir():
            entry.rmdir() if entry.is_dir() else entry.unlink()

    def deps(
        self,
        platform: str = "darwin",
        confirm_timeout: float = CONFIRM_TIMEOUT_S,
        poll: float = CONFIRM_POLL_S,
    ) -> BooksDeps:
        return BooksDeps(
            runner=self,
            sleep=self.sleep,
            platform=platform,
            library_dir=self.library,
            clock=self.clock,
            confirm_timeout=confirm_timeout,
            poll=poll,
        )
