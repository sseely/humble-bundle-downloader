"""Import downloaded ebooks into Apple Books (macOS) exactly once per file.

Functional core: selection, classification against saved state and new-entry
detection are pure. The shell (:func:`import_books`) only sequences effects:
reading and writing the state file, listing the library, and invoking the
injected runner, clock and sleep.

Files go to Books one per ``open`` call, and a file is recorded only after a new
entry appears in Books' library folder: ``open`` exits 0 even when Books drops
the file, which it did silently for batched opens.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from humble_dl.config import ConfigError, Settings
from humble_dl.lib.logging import FILE_NAME_KEY, get_logger
from humble_dl.services.books_library import (
    HIDDEN_PREFIX,
    PREFERRED_SUFFIXES,
    default_library_dir,
    is_book_entry,
    keys_for_selection,
    library_keys,
    partition,
    read_library_names,
)

DARWIN = "darwin"
OPEN_COMMAND = ("open", "-a", "Books")
OPEN_TIMEOUT_S = 120.0

DEFAULT_PAUSE_S = 1.0
MIN_PAUSE_S = 0.0
MAX_PAUSE_S = 60.0
# Observed imports land in 2-4 s; large PDFs take longer to copy.
CONFIRM_TIMEOUT_S = 60.0
CONFIRM_POLL_S = 0.5

MSG_PLATFORM = "Apple Books import requires macOS."
MSG_STATE_CORRUPT = (
    "Books state file {path} is unreadable or corrupt. Fix or delete it to continue; "
    "deleting it makes the next run re-import every book and create duplicates in Books."
)
MSG_NO_LIBRARY = (
    "Apple Books library folder {path} not found, so imports cannot be confirmed. "
    "Turn on iCloud for Books, open Books once, then run again."
)
MSG_BAD_PAUSE = "--pause must be in range {low}..{high} seconds, got {value}."
MSG_DRY_LINE = "{verb} {relative}"
MSG_DRY_TOTAL = "Dry run: {count} files to import."
VERB_IMPORT = "import"
VERB_REIMPORT = "reimport"
VERB_SKIP_DUPLICATE = "skip-duplicate"
VERB_SKIP_IN_BOOKS = "skip-in-books"

_ARCHIVED_STEM = re.compile(r"\.[0-9a-f]{8}$")

_log = get_logger(__name__)


class BooksError(Exception):
    """Base class for expected Apple Books import failures."""


class BooksStateError(BooksError):
    """The imported-files state is unreadable or corrupt."""


class BooksPlatformError(BooksError):
    """Apple Books import was requested on a platform other than macOS."""


class BooksLibraryError(BooksError):
    """Books' library folder is missing, so no import could be confirmed."""


class BooksFailedError(BooksError):
    """One or more files failed to import; the message lists them."""


class FileStamp(BaseModel):
    """Size and modification time identifying one version of a file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: StrictInt
    mtime_ns: StrictInt


class _StateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: dict[str, FileStamp]


class Runner(Protocol):
    """Runs one ``open`` invocation; returns True when it succeeded."""

    def __call__(self, argv: Sequence[str]) -> bool: ...


Sleeper = Callable[[float], None]
Clock = Callable[[], float]


def run_open(argv: Sequence[str]) -> bool:
    """Run ``argv`` (an argument list, never a shell string) with a timeout.

    Returns:
        True on exit status 0; False on a non-zero exit, timeout or launch error.
    """
    try:
        done = subprocess.run(  # noqa: S603
            list(argv), check=False, capture_output=True, timeout=OPEN_TIMEOUT_S
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return done.returncode == 0


@dataclass(frozen=True)
class BooksOptions:
    """Command options for one run."""

    dry_run: bool = False
    only: str | None = None
    pause: float = DEFAULT_PAUSE_S  # seconds between files


@dataclass(frozen=True)
class BooksDeps:
    """Injectable effects; tests replace these so Books is never launched."""

    runner: Runner = run_open
    sleep: Sleeper = time.sleep
    platform: str = field(default_factory=lambda: sys.platform)
    library_dir: Path | None = None  # None: default_library_dir() at call time
    clock: Clock = time.monotonic
    confirm_timeout: float = CONFIRM_TIMEOUT_S  # wait for the library entry
    poll: float = CONFIRM_POLL_S


@dataclass(frozen=True)
class BooksPlan:
    """Selected files classified against the saved state."""

    new: tuple[Path, ...]
    changed: tuple[Path, ...]
    already: tuple[Path, ...]
    stamps: Mapping[Path, FileStamp]
    duplicates: tuple[Path, ...] = ()
    in_books: tuple[Path, ...] = ()

    @property
    def selected(self) -> int:
        """Total number of selected files, including skipped ones."""
        kinds = (self.new, self.changed, self.already, self.duplicates, self.in_books)
        return sum(len(kind) for kind in kinds)

    @property
    def to_import(self) -> list[Path]:
        """Files to hand to Books, sorted."""
        return sorted((*self.new, *self.changed))


@dataclass(frozen=True)
class BooksReport:
    """Outcome of a run."""

    selected: int
    already: int
    imported: int
    changed: int
    failed: tuple[Path, ...]
    lines: tuple[str, ...] = ()
    duplicates: int = 0
    in_books: int = 0


def validate_options(opts: BooksOptions) -> None:
    """Check the pause range.

    Raises:
        ConfigError: If ``pause`` is outside its allowed range.
    """
    if not MIN_PAUSE_S <= opts.pause <= MAX_PAUSE_S:
        raise ConfigError(MSG_BAD_PAUSE.format(low=MIN_PAUSE_S, high=MAX_PAUSE_S, value=opts.pause))


def _is_visible(path: Path) -> bool:
    return not path.name.startswith(HIDDEN_PREFIX)


def _is_current(path: Path) -> bool:
    """A visible regular file that is not an archived ``<stem>.<md5[:8]><suffix>``."""
    return path.is_file() and _is_visible(path) and not _ARCHIVED_STEM.search(path.stem)


def _subdirs(path: Path) -> list[Path]:
    return sorted(d for d in path.iterdir() if d.is_dir() and _is_visible(d))


def _item_files(item: Path) -> list[Path]:
    """Current epubs if any, else current pdfs, else nothing."""
    files = [f for f in item.iterdir() if _is_current(f)]
    for suffix in PREFERRED_SUFFIXES:
        matching = [f for f in files if f.suffix.lower() == suffix]
        if matching:
            return matching
    return []


def select_books(output_dir: Path, only: str | None = None) -> list[Path]:
    """Pick the one-format-per-item files to import from ``<Bundle>/<Item>/``.

    Args:
        output_dir: Download root; only two directory levels below it are walked.
        only: Keep bundles whose folder name contains this text (case-insensitive).

    Returns:
        Sorted paths: all current ``.epub`` files of an item, else its ``.pdf`` files.
    """
    if not output_dir.is_dir():
        return []
    needle = only.casefold() if only else None
    selected: list[Path] = []
    for bundle in _subdirs(output_dir):
        if needle is not None and needle not in bundle.name.casefold():
            continue
        for item in _subdirs(bundle):
            selected.extend(_item_files(item))
    return sorted(selected)


def stat_stamps(paths: Sequence[Path]) -> dict[Path, FileStamp]:
    """Read the current size and mtime of each file."""
    stamps: dict[Path, FileStamp] = {}
    for path in paths:
        info = path.stat()
        stamps[path] = FileStamp(size=info.st_size, mtime_ns=info.st_mtime_ns)
    return stamps


def classify(stamps: Mapping[Path, FileStamp], state: Mapping[str, FileStamp]) -> BooksPlan:
    """Split files into new, changed (size or mtime differ) and already imported."""
    new: list[Path] = []
    changed: list[Path] = []
    already: list[Path] = []
    for path in sorted(stamps):
        recorded = state.get(str(path))
        if recorded is None:
            new.append(path)
        elif recorded != stamps[path]:
            changed.append(path)
        else:
            already.append(path)
    return BooksPlan(tuple(new), tuple(changed), tuple(already), stamps)


def new_entries(before: Collection[str], after: Iterable[str]) -> list[str]:
    """Book entries in ``after`` that were not in ``before``, sorted."""
    return sorted(name for name in after if is_book_entry(name) and name not in before)


def _skip_lines(verb: str, paths: Sequence[Path], output_dir: Path) -> list[str]:
    return [
        MSG_DRY_LINE.format(verb=verb, relative=p.relative_to(output_dir).as_posix()) for p in paths
    ]


def dry_run_lines(plan: BooksPlan, output_dir: Path) -> tuple[str, ...]:
    """One line per file that would be imported, then a total line."""
    changed = set(plan.changed)
    lines = [
        MSG_DRY_LINE.format(
            verb=VERB_REIMPORT if path in changed else VERB_IMPORT,
            relative=path.relative_to(output_dir).as_posix(),
        )
        for path in plan.to_import
    ]
    lines.extend(_skip_lines(VERB_SKIP_DUPLICATE, plan.duplicates, output_dir))
    lines.extend(_skip_lines(VERB_SKIP_IN_BOOKS, plan.in_books, output_dir))
    lines.append(MSG_DRY_TOTAL.format(count=len(plan.to_import)))
    return tuple(lines)


def load_state(path: Path) -> dict[str, FileStamp]:
    """Load the imported-files state; a missing file means empty.

    Raises:
        BooksStateError: If the file exists but is unreadable or malformed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BooksStateError(MSG_STATE_CORRUPT.format(path=path)) from exc
    try:
        return dict(_StateFile.model_validate_json(text).files)
    except ValidationError as exc:
        raise BooksStateError(MSG_STATE_CORRUPT.format(path=path)) from exc


def save_state(path: Path, state: Mapping[str, FileStamp]) -> None:
    """Write ``state`` atomically (temp file in the same directory, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"files": {key: stamp.model_dump() for key, stamp in sorted(state.items())}}
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _wait_for_entry(library: Path, before: Collection[str], deps: BooksDeps) -> bool:
    """Poll the library until a new book entry appears or the timeout passes."""
    deadline = deps.clock() + deps.confirm_timeout
    while not new_entries(before, read_library_names(library)):
        if deps.clock() >= deadline:
            return False
        deps.sleep(deps.poll)
    return True


def _import_one(path: Path, library: Path, deps: BooksDeps) -> bool:
    """Send one file to Books; True once its new library entry appears."""
    before = frozenset(read_library_names(library))
    opened = deps.runner([*OPEN_COMMAND, str(path)])
    confirmed = opened and _wait_for_entry(library, before, deps)
    extra = {"opened": opened, "confirmed": confirmed, FILE_NAME_KEY: path.name}
    (_log.info if confirmed else _log.warning)("books_file", extra=extra)
    return confirmed


def _import_all(
    plan: BooksPlan,
    state: dict[str, FileStamp],
    state_path: Path,
    opts: BooksOptions,
    deps: BooksDeps,
) -> BooksReport:
    library = deps.library_dir or default_library_dir()
    done: list[Path] = []
    failed: list[Path] = []
    for index, path in enumerate(plan.to_import):
        if index and opts.pause > 0:
            deps.sleep(opts.pause)
        confirmed = _import_one(path, library, deps)
        if confirmed:
            state[str(path)] = plan.stamps[path]
            save_state(state_path, state)
        (done if confirmed else failed).append(path)
    changed = set(plan.changed)
    reimported = sum(1 for p in done if p in changed)
    return BooksReport(
        selected=plan.selected,
        already=len(plan.already),
        imported=len(done) - reimported,
        changed=reimported,
        failed=tuple(failed),
        duplicates=len(plan.duplicates),
        in_books=len(plan.in_books),
    )


def _build_plan(
    root: Path, opts: BooksOptions, state: Mapping[str, FileStamp], deps: BooksDeps
) -> BooksPlan:
    names = read_library_names(deps.library_dir or default_library_dir())
    selected = select_books(root, opts.only)
    keys = keys_for_selection(selected)
    remaining, duplicates, in_books = partition(selected, keys, library_keys(names))
    plan = classify(stat_stamps(remaining), state)
    return replace(plan, duplicates=tuple(duplicates), in_books=tuple(in_books))


def _require_import_ready(deps: BooksDeps) -> None:
    if deps.platform != DARWIN:
        raise BooksPlatformError(MSG_PLATFORM)
    library = deps.library_dir or default_library_dir()
    if not library.is_dir():
        raise BooksLibraryError(MSG_NO_LIBRARY.format(path=library))


def import_books(
    settings: Settings, opts: BooksOptions, deps: BooksDeps | None = None
) -> BooksReport:
    """Import selected books into Apple Books, skipping already-imported files.

    Args:
        settings: Provides ``output_dir`` and ``books_state_path``.
        opts: Run options; ``dry_run`` plans only (no ``open`` calls, no state writes).
        deps: Injected runner, sleep and platform name.

    Returns:
        The report; for a dry run ``lines`` holds the planned imports.

    Raises:
        ConfigError: If pause is out of range.
        BooksPlatformError: If not a dry run and the platform is not macOS.
        BooksLibraryError: If not a dry run and Books' library folder is missing.
        BooksStateError: If the state file is corrupt.
    """
    deps = deps or BooksDeps()
    validate_options(opts)
    if not opts.dry_run:
        _require_import_ready(deps)
    root = settings.output_dir.resolve()
    _log.info("books_started", extra={"dry_run": opts.dry_run, "only": opts.only})
    state = load_state(settings.books_state_path)
    plan = _build_plan(root, opts, state, deps)
    if opts.dry_run:
        return BooksReport(
            plan.selected,
            len(plan.already),
            0,
            0,
            (),
            dry_run_lines(plan, root),
            len(plan.duplicates),
            len(plan.in_books),
        )
    report = _import_all(plan, state, settings.books_state_path, opts, deps)
    _log.info(
        "books_complete",
        extra={
            "selected": report.selected,
            "already": report.already,
            "imported": report.imported,
            "changed": report.changed,
            "failed": len(report.failed),
            "duplicates": report.duplicates,
            "in_books": report.in_books,
        },
    )
    return report
