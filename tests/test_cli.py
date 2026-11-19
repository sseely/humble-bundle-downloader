"""Tests for the CLI shell: exit codes, messages, injection seams."""

import json
import sys
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from playwright.async_api import Error as PlaywrightError
from typer.testing import CliRunner, Result

from humble_dl import cli
from humble_dl.api.capture import CaptureContext, CaptureError, CaptureLeakError
from humble_dl.api.client import LibraryApiError
from humble_dl.auth.session import (
    BotChallengeError,
    CookieJar,
    SessionCheckError,
    SessionContext,
    SessionExpiredError,
)
from humble_dl.config import ConfigError, Settings
from humble_dl.models.library import SchemaDriftError
from humble_dl.services import books as books_service
from humble_dl.services.books import BooksDeps
from humble_dl.services.planner import ArchiveThenFetch, Fetch, FileKey, Skip
from humble_dl.services.sync import (
    InsufficientSpaceError,
    LibraryLike,
    Summary,
    SyncDeps,
    SyncOptions,
)

ENV_CONCURRENCY = "HUMBLE_DL_CONCURRENCY"
ENV_PROFILE = "HUMBLE_DL_PROFILE_DIR"
FAKE_CTX = cast("cli.BrowserLike", object())
SECRET_URL = "https://www.humblebundle.com/api/v1/order/abc?gamekey=SECRETKEY"  # noqa: S105


class Recorder:
    """Fake collaborators that record calls instead of launching a browser."""

    def __init__(self, error: Exception | None = None, written: int = 2) -> None:
        self.error = error
        self.written = written
        self.headed: list[bool] = []
        self.login_ctx: list[object] = []
        self.capture_out: list[Path] = []
        self.events: list[str] = []
        self.persist_ctx: list[object] = []
        self.persist_error: Exception | None = None

    @asynccontextmanager
    async def opener(self, settings: Settings, *, headed: bool) -> AsyncGenerator[cli.BrowserLike]:
        self.headed.append(headed)
        self.events.append("open")
        try:
            yield FAKE_CTX
        finally:
            self.events.append("close")

    async def login_fn(self, ctx: SessionContext, settings: Settings) -> None:
        self.login_ctx.append(ctx)
        self.events.append("login")
        if self.error:
            raise self.error

    async def persist_fn(self, ctx: CookieJar, settings: Settings) -> int:
        self.persist_ctx.append(ctx)
        self.events.append("persist")
        if self.persist_error:
            raise self.persist_error
        return 1

    async def capture_fn(self, ctx: CaptureContext, settings: Settings, out: Path) -> list[Path]:
        self.capture_out.append(out)
        if self.error:
            raise self.error
        return [out / f"{n}.json" for n in range(self.written)]


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(ENV_CONCURRENCY, raising=False)
    monkeypatch.delenv(ENV_PROFILE, raising=False)
    return home


def install(monkeypatch: pytest.MonkeyPatch, rec: Recorder) -> None:
    monkeypatch.setattr(cli, "OPEN_CONTEXT", rec.opener)
    monkeypatch.setattr(cli, "INTERACTIVE_LOGIN", rec.login_fn)
    monkeypatch.setattr(cli, "RUN_CAPTURE", rec.capture_fn)
    monkeypatch.setattr(cli, "PERSIST_COOKIES", rec.persist_fn)


def invoke(*args: str) -> Result:
    return CliRunner().invoke(cli.app, list(args))


def log_events(result: Result) -> list[dict[str, object]]:
    lines = [ln for ln in result.stderr.splitlines() if ln.startswith("{")]
    return [json.loads(ln) for ln in lines]


def test_exit_code_constants() -> None:
    assert (cli.EXIT_OK, cli.EXIT_CONFIG, cli.EXIT_SESSION) == (0, 1, 2)
    assert (cli.EXIT_SCHEMA, cli.EXIT_FILES) == (3, 4)


def test_help_lists_commands() -> None:
    result = invoke("--help")
    assert result.exit_code == 0
    assert "login" in result.stdout
    assert "capture" in result.stdout


@pytest.mark.parametrize("command", ["login", "capture"])
def test_bad_concurrency_exits_1_naming_variable(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    install(monkeypatch, Recorder())
    monkeypatch.setenv(ENV_CONCURRENCY, "0")
    result = invoke(command)
    assert result.exit_code == cli.EXIT_CONFIG
    assert ENV_CONCURRENCY in result.stderr


def test_login_success_uses_headed_context(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    install(monkeypatch, rec)
    result = invoke("login")
    assert result.exit_code == cli.EXIT_OK
    assert rec.headed == [True]
    assert rec.login_ctx == [FAKE_CTX]
    assert cli.MSG_LOGIN_DONE in result.stderr


def test_login_persists_cookies_after_login_before_context_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = Recorder()
    install(monkeypatch, rec)
    result = invoke("login")
    assert result.exit_code == cli.EXIT_OK
    assert rec.events == ["open", "login", "persist", "close"]
    assert rec.persist_ctx == [FAKE_CTX]


def test_login_persist_session_expired_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    rec.persist_error = SessionExpiredError("x")
    install(monkeypatch, rec)
    result = invoke("login")
    assert result.exit_code == cli.EXIT_SESSION
    assert cli.MSG_SESSION_EXPIRED in result.stderr
    assert cli.MSG_LOGIN_DONE not in result.stderr


def test_failed_login_does_not_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(error=SessionExpiredError("x"))
    install(monkeypatch, rec)
    invoke("login")
    assert rec.persist_ctx == []


def test_capture_never_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    install(monkeypatch, rec)
    invoke("capture")
    assert rec.persist_ctx == []


def test_capture_success_reports_and_uses_headed_browser(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = Recorder(written=3)
    install(monkeypatch, rec)
    out = tmp_path / "fixtures"
    result = invoke("capture", "--out", str(out))
    assert result.exit_code == cli.EXIT_OK
    # Cloudflare blocks server-action POSTs from headless Chromium (signed URLs).
    assert rec.headed == [True]
    assert rec.capture_out == [out]
    assert f"Wrote 3 files to {out}." in result.stderr
    assert "Review the captured files" in result.stderr


def test_capture_default_out_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    install(monkeypatch, rec)
    invoke("capture")
    assert rec.capture_out == [cli.DEFAULT_CAPTURE_DIR]


def test_sync_unsigned_files_exit_4_and_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = Summary(1, 0, 1, 0, 0, 10, 1.0, [], unsigned_files=["Bundle/lost.epub"])
    install_sync(monkeypatch, Recorder(), SyncRecorder(summary))
    result = invoke("sync")
    assert result.exit_code == cli.EXIT_FILES
    assert "1 file(s) had no download link:" in result.stderr
    assert "Bundle/lost.epub" in result.stderr


def test_sync_failed_bundle_exits_4_and_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = Summary(1, 0, 1, 0, 0, 10, 1.0, [], failed_bundles=["Humble Comics Bundle: X"])
    install_sync(monkeypatch, Recorder(), SyncRecorder(summary))
    result = invoke("sync")
    assert result.exit_code == cli.EXIT_FILES
    assert "1 bundle(s) could not be loaded:" in result.stderr
    assert "Humble Comics Bundle: X" in result.stderr


@pytest.mark.parametrize(
    ("error", "code", "fragment"),
    [
        (BotChallengeError("x"), 2, "bot/captcha challenge"),
        (SessionExpiredError("x"), 2, "Run `humble-dl login`."),
        (ConfigError("bad setting FOO"), 1, "bad setting FOO"),
        (CaptureLeakError(SECRET_URL), 1, "nothing was written"),
        (SessionCheckError(SECRET_URL), 1, "session check failed"),
        (CaptureError(SECRET_URL), 1, "Capture failed"),
    ],
)
@pytest.mark.parametrize("command", ["login", "capture"])
def test_error_mapping(
    monkeypatch: pytest.MonkeyPatch, command: str, error: Exception, code: int, fragment: str
) -> None:
    install(monkeypatch, Recorder(error=error))
    result = invoke(command)
    assert result.exit_code == code
    assert fragment in result.stderr
    assert "SECRETKEY" not in result.stderr
    assert "gamekey=" not in result.stderr


def test_bot_challenge_message_differs_from_expired() -> None:
    assert cli.map_error(BotChallengeError("x")) == (2, cli.MSG_BOT_CHALLENGE)
    assert cli.map_error(SessionExpiredError("x")) == (2, cli.MSG_SESSION_EXPIRED)
    assert cli.MSG_BOT_CHALLENGE != cli.MSG_SESSION_EXPIRED


def test_map_error_unexpected_is_none() -> None:
    assert cli.map_error(RuntimeError("boom")) is None


def test_unexpected_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, Recorder(error=RuntimeError("boom")))
    result = invoke("capture")
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "boom"


def test_logs_started_and_finished_with_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, Recorder(error=SessionExpiredError("x")))
    result = invoke("capture")
    events = {str(e["msg"]): e for e in log_events(result)}
    assert events["command_started"]["command"] == "capture"
    assert events["command_finished"]["command"] == "capture"
    assert events["command_finished"]["exit_code"] == cli.EXIT_SESSION


def test_logs_success_exit_code_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, Recorder())
    result = invoke("login")
    finished = [e for e in log_events(result) if e["msg"] == "command_finished"]
    assert [e["exit_code"] for e in finished] == [cli.EXIT_OK]


def test_no_real_home_paths_created(monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> None:
    install(monkeypatch, Recorder())
    invoke("login")
    assert list(isolated_home.iterdir()) == []


OUT_DIR = Path("/out")
FETCH_A = Fetch(FileKey("k", "m", "a.epub"), OUT_DIR / "Bundle" / "Item" / "a.epub", "u", None, 100)
FETCH_B = Fetch(FileKey("k", "m", "b.pdf"), OUT_DIR / "Bundle" / "Item" / "b.pdf", "u", None, None)
ARCHIVE = ArchiveThenFetch(Path("old"), Path("older"), FETCH_B)


class SyncRecorder:
    """Fakes for the sync seams; records what the command passed in."""

    def __init__(self, summary: Summary | None = None, error: Exception | None = None) -> None:
        self.summary = summary or Summary(2, 1, 1, 0, 0, 50, 1.25, [])
        self.error = error
        self.calls: list[tuple[Settings, SyncOptions, SyncDeps]] = []
        self.checked: list[object] = []
        self.library = cast("LibraryLike", object())

    async def run(self, settings: Settings, opts: SyncOptions, deps: SyncDeps) -> Summary:
        self.calls.append((settings, opts, deps))
        if self.error:
            raise self.error
        return self.summary

    async def assert_logged_in(self, ctx: SessionContext, settings: Settings) -> None:
        self.checked.append(ctx)

    async def make_library(self, ctx: cli.BrowserLike, settings: Settings) -> LibraryLike:
        return self.library


def install_sync(monkeypatch: pytest.MonkeyPatch, rec: Recorder, sync: SyncRecorder) -> None:
    install(monkeypatch, rec)
    monkeypatch.setattr(cli, "RUN_SYNC", sync.run)
    monkeypatch.setattr(cli, "ASSERT_LOGGED_IN", sync.assert_logged_in)
    monkeypatch.setattr(cli, "MAKE_LIBRARY", sync.make_library)


def test_sync_success_wires_everything_headed(
    monkeypatch: pytest.MonkeyPatch, isolated_home: Path
) -> None:
    rec, sync = Recorder(), SyncRecorder()
    install_sync(monkeypatch, rec, sync)
    result = invoke("sync", "--only", "python")
    assert result.exit_code == cli.EXIT_OK
    assert rec.headed == [True]
    assert sync.checked == [FAKE_CTX]
    settings, opts, deps = sync.calls[0]
    assert settings.output_dir == isolated_home / "HumbleBundle"
    assert opts == SyncOptions(dry_run=False, only="python")
    assert deps.library is sync.library
    assert deps.free_bytes(isolated_home) > 0
    assert "planned 2, skipped 1, downloaded 1, failed 0, archived 0, 50 bytes in 1.2s" in (
        result.stdout
    )


def test_sync_output_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sync = SyncRecorder()
    install_sync(monkeypatch, Recorder(), sync)
    out = tmp_path / "elsewhere"
    assert invoke("sync", "--output", str(out)).exit_code == cli.EXIT_OK
    assert sync.calls[0][0].output_dir == out


def test_sync_dry_run_lists_files_and_totals(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = Summary(3, 1, 0, 0, 0, 0, 0.1, [], [FETCH_A, ARCHIVE, Skip(Path("s"))])
    sync = SyncRecorder(summary)
    install_sync(monkeypatch, Recorder(), sync)
    out = FETCH_A.path.parents[2]
    result = invoke("sync", "--dry-run", "--output", str(out))
    assert result.exit_code == cli.EXIT_OK
    assert sync.calls[0][1].dry_run is True
    lines = result.stdout.splitlines()
    assert "fetch Bundle/Item/a.epub (100 bytes)" in lines
    assert "archive+fetch Bundle/Item/b.pdf (size unknown)" in lines
    assert "Dry run: 2 files to fetch, 100 known bytes." in lines
    assert len(lines) == 4


def test_sync_failed_file_exits_4_and_prints_path(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = Path("/out/Bundle/Item/bad.epub")
    sync = SyncRecorder(Summary(2, 0, 1, 1, 0, 10, 1.0, [bad]))
    install_sync(monkeypatch, Recorder(), sync)
    result = invoke("sync")
    assert result.exit_code == cli.EXIT_FILES
    assert "1 file(s) failed:" in result.stderr
    assert str(bad) in result.stderr
    assert "failed 1" in result.stdout


@pytest.mark.parametrize(
    ("error", "code", "fragment"),
    [
        (InsufficientSpaceError("need 20 bytes, only 5 bytes free"), 1, "need 20 bytes, only 5"),
        (SchemaDriftError("a.b", "bad"), 3, "no longer matches"),
        (LibraryApiError("GET https://h/p failed with status 500"), 1, "status 500"),
        (PlaywrightError("Target closed /secret/path"), 1, "Browser error"),
        (SessionExpiredError("x"), 2, "humble-dl login"),
        (BotChallengeError("x"), 2, "bot/captcha"),
    ],
)
def test_sync_error_mapping(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: int, fragment: str
) -> None:
    install_sync(monkeypatch, Recorder(), SyncRecorder(error=error))
    result = invoke("sync")
    assert result.exit_code == code
    assert fragment in result.stderr
    assert "/secret/path" not in result.stderr


def test_sync_help_listed() -> None:
    assert "sync" in invoke("--help").stdout


class _FakeCtx:
    def __init__(self) -> None:
        self.pages = 0

    async def new_page(self) -> object:
        self.pages += 1
        return object()


async def test_make_library_builds_client_on_new_page(tmp_path: Path) -> None:
    ctx = _FakeCtx()
    settings = cli.load_settings({}, tmp_path)
    library = await cli._make_library(cast("cli.BrowserLike", ctx), settings)  # pyright: ignore[reportPrivateUsage]
    assert ctx.pages == 1
    assert type(library).__name__ == "LibraryClient"


class BooksRecorder:
    """Fake Books runner; records argv lists and never launches anything."""

    def __init__(self, fail_on: frozenset[int] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.sleeps: list[float] = []
        self.fail_on = fail_on

    def __call__(self, argv: Sequence[str]) -> bool:
        self.calls.append(list(argv))
        return len(self.calls) not in self.fail_on

    def deps(self, platform: str = "darwin", library: Path | None = None) -> BooksDeps:
        return BooksDeps(
            runner=self, sleep=self.sleeps.append, platform=platform, library_dir=library
        )


def install_books(
    monkeypatch: pytest.MonkeyPatch,
    rec: BooksRecorder,
    platform: str = "darwin",
    library: Path | None = None,
) -> None:
    monkeypatch.setattr(cli, "BOOKS_DEPS", lambda: rec.deps(platform, library))


def seed_books(out: Path, count: int) -> list[Path]:
    paths = [out / "Bundle" / f"Item{n}" / f"book{n}.epub" for n in range(count)]
    for path in paths:
        path.parent.mkdir(parents=True)
        path.write_text("x")
    return paths


def test_books_imports_then_skips(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec = BooksRecorder()
    install_books(monkeypatch, rec)
    out = tmp_path / "lib"
    seed_books(out, 3)
    first = invoke("books", "--output", str(out), "--batch", "2", "--pause", "1")
    assert first.exit_code == cli.EXIT_OK
    assert "selected 3, already imported 0, imported 3, changed 0, failed 0" in first.stdout
    assert len(rec.calls) == 2
    assert rec.sleeps == [1.0]
    second = invoke("books", "--output", str(out))
    assert second.exit_code == cli.EXIT_OK
    assert "selected 3, already imported 3, imported 0, changed 0, failed 0" in second.stdout
    assert len(rec.calls) == 2


def test_books_failed_batch_exits_4_and_lists_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = BooksRecorder(fail_on=frozenset({2}))
    install_books(monkeypatch, rec)
    out = tmp_path / "lib"
    files = [p.resolve() for p in seed_books(out, 3)]
    result = invoke("books", "--output", str(out), "--batch", "1", "--pause", "0")
    assert result.exit_code == cli.EXIT_FILES
    assert "1 file(s) failed to import into Books:" in result.stderr
    assert str(files[1]) in result.stderr
    assert str(files[0]) not in result.stderr
    assert "failed 1" in result.stdout


def test_books_corrupt_state_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_home: Path
) -> None:
    rec = BooksRecorder()
    install_books(monkeypatch, rec)
    out = tmp_path / "lib"
    seed_books(out, 1)
    state = isolated_home / ".humble-dl" / "books-imported.json"
    state.parent.mkdir()
    state.write_text("garbage")
    result = invoke("books", "--output", str(out))
    assert result.exit_code == cli.EXIT_CONFIG
    assert "corrupt" in result.stderr
    assert rec.calls == []


def test_books_non_darwin_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec = BooksRecorder()
    install_books(monkeypatch, rec, platform="linux")
    out = tmp_path / "lib"
    seed_books(out, 1)
    result = invoke("books", "--output", str(out))
    assert result.exit_code == cli.EXIT_CONFIG
    assert "Apple Books import requires macOS." in result.stderr
    assert rec.calls == []


def test_books_dry_run_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_home: Path
) -> None:
    rec = BooksRecorder()
    install_books(monkeypatch, rec, platform="linux")
    out = tmp_path / "lib"
    seed_books(out, 2)
    result = invoke("books", "--output", str(out), "--dry-run")
    assert result.exit_code == cli.EXIT_OK
    assert result.stdout.splitlines() == [
        "import Bundle/Item0/book0.epub",
        "import Bundle/Item1/book1.epub",
        "Dry run: 2 files to import.",
    ]
    assert rec.calls == []
    assert not (isolated_home / ".humble-dl").exists()


@pytest.mark.parametrize("args", [["--batch", "0"], ["--batch", "51"], ["--pause", "-1"]])
def test_books_invalid_options_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: list[str]
) -> None:
    rec = BooksRecorder()
    install_books(monkeypatch, rec)
    result = invoke("books", "--output", str(tmp_path), *args)
    assert result.exit_code == cli.EXIT_CONFIG
    assert rec.calls == []


def test_books_only_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rec = BooksRecorder()
    install_books(monkeypatch, rec)
    out = tmp_path / "lib"
    seed_books(out, 1)
    result = invoke("books", "--output", str(out), "--only", "nomatch")
    assert result.exit_code == cli.EXIT_OK
    assert "selected 0" in result.stdout
    assert rec.calls == []


def test_books_default_deps_are_real() -> None:
    deps = cli.BOOKS_DEPS()
    assert deps.runner is books_service.run_open
    assert deps.platform == sys.platform


def test_books_summary_has_skip_counts_and_dry_run_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out, library = tmp_path / "lib", tmp_path / "bks"
    seed_books(out, 1)
    other = out / "Other" / "Item" / "book0.pdf"
    other.parent.mkdir(parents=True)
    other.write_text("x")
    (out / "Bundle" / "Item1" / "x.epub").parent.mkdir(parents=True)
    (out / "Bundle" / "Item1" / "x.epub").write_text("x")
    (library / "x.epub").mkdir(parents=True)
    rec = BooksRecorder()
    install_books(monkeypatch, rec, library=library)
    dry = invoke("books", "--output", str(out), "--dry-run")
    assert dry.stdout.splitlines() == [
        "import Bundle/Item0/book0.epub",
        "skip-duplicate Other/Item/book0.pdf",
        "skip-in-books Bundle/Item1/x.epub",
        "Dry run: 1 files to import.",
    ]
    result = invoke("books", "--output", str(out), "--pause", "0")
    assert result.exit_code == cli.EXIT_OK
    assert (
        "selected 3, already imported 0, imported 1, changed 0, failed 0, "
        "duplicate in another bundle 1, already in Books 1"
    ) in result.stdout
    assert len(rec.calls) == 1


def test_books_duplicates_command_reports_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, isolated_home: Path
) -> None:
    library = tmp_path / "bks"
    (library / "x.epub").mkdir(parents=True)
    (library / "x_1234567890.pdf").write_text("x")
    rec = BooksRecorder()
    install_books(monkeypatch, rec, library=library)
    result = invoke("books-duplicates")
    assert result.exit_code == cli.EXIT_OK
    assert result.stdout.splitlines() == [
        "x.epub | x_1234567890.pdf",
        "1 book(s) with more than one copy in Books.",
    ]
    assert rec.calls == []
    assert not (isolated_home / ".humble-dl").exists()
