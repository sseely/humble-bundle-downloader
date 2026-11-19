"""Import downloaded ebooks into Apple Books (macOS) exactly once per file.

Functional core: selection, classification against saved state and batching are
pure. The shell (:func:`import_books`) only sequences effects: reading and
writing the state file and invoking the injected runner and sleep.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
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
    keys_for_selection,
    library_keys,
    partition,
    read_library_names,
)

DARWIN = "darwin"
OPEN_COMMAND = ("open", "-a", "Books")
OPEN_TIMEOUT_S = 120.0

DEFAULT_BATCH = 10
MIN_BATCH = 1
MAX_BATCH = 50
DEFAULT_PAUSE_S = 5.0
MIN_PAUSE_S = 0.0
MAX_PAUSE_S = 60.0

MSG_PLATFORM = "Apple Books import requires macOS."
MSG_STATE_CORRUPT = (
    "Books state file {path} is unreadable or corrupt. Fix or delete it to continue; "
    "deleting it makes the next run re-import every book and create duplicates in Books."
)
MSG_BAD_BATCH = "--batch must be an integer in range {low}..{high}, got {value}."
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


class BooksFailedError(BooksError):
    """One or more batches failed to import; the message lists their files."""


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
    batch: int = DEFAULT_BATCH
    pause: float = DEFAULT_PAUSE_S


@dataclass(frozen=True)
class BooksDeps:
    """Injectable effects; tests replace these so Books is never launched."""

    runner: Runner = run_open
    sleep: Sleeper = time.sleep
    platform: str = field(default_factory=lambda: sys.platform)
    library_dir: Path | None = None  # None: default_library_dir() at call time


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
    """Check batch and pause ranges.

    Raises:
        ConfigError: If ``batch`` or ``pause`` is outside its allowed range.
    """
    if not MIN_BATCH <= opts.batch <= MAX_BATCH:
        raise ConfigError(MSG_BAD_BATCH.format(low=MIN_BATCH, high=MAX_BATCH, value=opts.batch))
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


def chunk(paths: Sequence[Path], size: int) -> list[list[Path]]:
    """Split ``paths`` into consecutive batches of at most ``size``."""
    return [list(paths[i : i + size]) for i in range(0, len(paths), size)]


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


def _import_batch(
    batch: Sequence[Path],
    plan: BooksPlan,
    state: dict[str, FileStamp],
    state_path: Path,
    runner: Runner,
) -> bool:
    """Import one batch; on success record and persist its files."""
    ok = runner([*OPEN_COMMAND, *(str(p) for p in batch)])
    extra = {"count": len(batch), "ok": ok, FILE_NAME_KEY: batch[0].name}
    if ok:
        _log.info("books_batch", extra=extra)
        state.update({str(p): plan.stamps[p] for p in batch})
        save_state(state_path, state)
    else:
        _log.warning("books_batch", extra=extra)
    return ok


def _import_all(
    plan: BooksPlan,
    state: dict[str, FileStamp],
    state_path: Path,
    opts: BooksOptions,
    deps: BooksDeps,
) -> BooksReport:
    done: list[Path] = []
    failed: list[Path] = []
    for index, batch in enumerate(chunk(plan.to_import, opts.batch)):
        if index and opts.pause > 0:
            deps.sleep(opts.pause)
        ok = _import_batch(batch, plan, state, state_path, deps.runner)
        (done if ok else failed).extend(batch)
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
        ConfigError: If batch or pause is out of range.
        BooksPlatformError: If not a dry run and the platform is not macOS.
        BooksStateError: If the state file is corrupt.
    """
    deps = deps or BooksDeps()
    validate_options(opts)
    if not opts.dry_run and deps.platform != DARWIN:
        raise BooksPlatformError(MSG_PLATFORM)
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
