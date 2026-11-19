"""Tests for the CLI shell: exit codes, messages, injection seams."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner, Result

from humble_dl import cli
from humble_dl.api.capture import CaptureContext, CaptureError, CaptureLeakError
from humble_dl.auth.session import (
    BotChallengeError,
    CookieJar,
    SessionCheckError,
    SessionContext,
    SessionExpiredError,
)
from humble_dl.config import ConfigError, Settings

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


def test_capture_success_reports_and_uses_headless(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = Recorder(written=3)
    install(monkeypatch, rec)
    out = tmp_path / "fixtures"
    result = invoke("capture", "--out", str(out))
    assert result.exit_code == cli.EXIT_OK
    assert rec.headed == [False]
    assert rec.capture_out == [out]
    assert f"Wrote 3 files to {out}." in result.stderr
    assert "Review the captured files" in result.stderr


def test_capture_default_out_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    install(monkeypatch, rec)
    invoke("capture")
    assert rec.capture_out == [cli.DEFAULT_CAPTURE_DIR]


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
