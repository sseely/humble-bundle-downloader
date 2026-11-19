"""Tests for humble_dl.auth.session using fake contexts (no real browser)."""

import stat
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from playwright._impl._api_structures import SetCookieParam
from playwright.async_api import BrowserContext, Cookie
from playwright.async_api import Error as PlaywrightError

from humble_dl.auth import session
from humble_dl.auth.session import (
    AUTH_BASE,
    HUMBLE_BASE,
    SESSION_PROBE_URL,
    BotChallengeError,
    CookieJar,
    ProbeVerdict,
    SessionCheckError,
    SessionContext,
    SessionExpiredError,
    assert_logged_in,
    classify_probe,
    cookies_to_persist,
    fetch_identity_secrets,
    interactive_login,
    open_context,
    parse_session_expiry,
    persist_session_cookies,
    prepare_profile_dir,
)
from humble_dl.config import Settings

LOGIN_TIMEOUT_S = 10.0
API_TIMEOUT_S = 7.0
HTTP_TIMEOUT_S = 20.0
LOGIN_REDIRECT = "/login?goto=%2Fhome"
KRATOS_REDIRECT = f"{AUTH_BASE}/self-service/login/browser?return_to=x"
EMAIL = "user@example.com"
NAME = "Jane Doe"
IDENTITY_ID = "id-identity-1"
SESSION_ID = "id-session-2"
WHOAMI_BODY = {
    "id": SESSION_ID,
    "identity": {
        "id": IDENTITY_ID,
        "traits": {"email": EMAIL, "name": {"first": "Jane", "last": "Doe"}, "tags": [NAME, ""]},
    },
}
CHALLENGE_BODY = "<html>Just a moment... cf-chl</html>"
LEAK_BODY = "SECRET-BODY-TEXT"
LEAK_COOKIE = "session=abc123"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=tmp_path / "out",
        profile_dir=tmp_path / "profile",
        debug_dir=tmp_path / "debug",
        hash_cache_path=tmp_path / "hc.json",
        concurrency=1,
        api_rps=1.0,
        http_timeout_s=HTTP_TIMEOUT_S,
        api_timeout_s=API_TIMEOUT_S,
        login_timeout_s=LOGIN_TIMEOUT_S,
    )


@dataclass
class FakeResponse:
    status: int
    headers: dict[str, str] = field(default_factory=lambda: {})
    body: str = ""
    json_body: Any = None

    async def text(self) -> str:
        return self.body

    async def json(self) -> Any:
        if isinstance(self.json_body, Exception):
            raise self.json_body
        return self.json_body


class FakeRequester:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, float | None, int | None]] = []

    async def get(
        self, url: str, *, timeout: float | None = None, max_redirects: int | None = None
    ) -> FakeResponse:
        self.calls.append((url, timeout, max_redirects))
        item = self._responses[min(len(self.calls), len(self._responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item


class FakePage:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.visited: list[tuple[str, float | None]] = []

    async def goto(self, url: str, *, timeout: float | None = None) -> None:
        if self.error:
            raise self.error
        self.visited.append((url, timeout))


class FakeContext:
    def __init__(
        self, responses: list[FakeResponse | Exception], page_error: Exception | None = None
    ) -> None:
        self.request = FakeRequester(responses)
        self.page = FakePage(page_error)

    async def new_page(self) -> FakePage:
        return self.page


def ctx_of(*statuses: int) -> FakeContext:
    return FakeContext([FakeResponse(s) for s in statuses])


def as_session(ctx: FakeContext) -> SessionContext:
    return ctx


def real_context_as_session(real: BrowserContext) -> SessionContext:
    return real  # static check: pyright fails if BrowserContext is not structural match


@pytest.mark.parametrize(
    ("status", "location", "body", "verdict"),
    [
        (200, None, "", ProbeVerdict.OK),
        (401, None, "", ProbeVerdict.EXPIRED),
        (403, None, "forbidden", ProbeVerdict.EXPIRED),
        (403, None, "Please solve the CAPTCHA", ProbeVerdict.CHALLENGE),
        (503, None, "/cdn-cgi/challenge-platform/x", ProbeVerdict.CHALLENGE),
        (503, None, "maintenance", ProbeVerdict.UNEXPECTED),
        (401, None, CHALLENGE_BODY, ProbeVerdict.EXPIRED),
        (302, LOGIN_REDIRECT, "", ProbeVerdict.EXPIRED),
        (302, f"{HUMBLE_BASE}/login", "", ProbeVerdict.EXPIRED),
        (307, KRATOS_REDIRECT, "", ProbeVerdict.EXPIRED),
        (307, "/self-service/login/browser", "", ProbeVerdict.EXPIRED),
        (302, "/home", "", ProbeVerdict.UNEXPECTED),
        (302, None, "", ProbeVerdict.UNEXPECTED),
        (500, None, "", ProbeVerdict.UNEXPECTED),
    ],
)
def test_classify_probe(
    status: int, location: str | None, body: str, verdict: ProbeVerdict
) -> None:
    assert classify_probe(status, location, body) is verdict


def test_bot_challenge_is_session_expired_subclass() -> None:
    assert issubclass(BotChallengeError, SessionExpiredError)


async def test_assert_logged_in_ok_uses_probe_url_timeout_and_no_redirects(tmp_path: Path) -> None:
    ctx = ctx_of(200)
    await assert_logged_in(as_session(ctx), make_settings(tmp_path))
    assert ctx.request.calls == [(SESSION_PROBE_URL, API_TIMEOUT_S * 1000, 0)]


def test_probe_url_is_kratos_whoami() -> None:
    assert SESSION_PROBE_URL == "https://auth.humblebundle.com/sessions/whoami"


async def test_assert_logged_in_403_without_markers_is_expired(tmp_path: Path) -> None:
    with pytest.raises(SessionExpiredError) as info:
        await assert_logged_in(as_session(ctx_of(403)), make_settings(tmp_path))
    assert type(info.value) is SessionExpiredError


async def test_assert_logged_in_kratos_redirect_raises_expired(tmp_path: Path) -> None:
    ctx = FakeContext([FakeResponse(307, {"location": KRATOS_REDIRECT})])
    with pytest.raises(SessionExpiredError):
        await assert_logged_in(as_session(ctx), make_settings(tmp_path))


async def test_assert_logged_in_401_raises_expired(tmp_path: Path) -> None:
    with pytest.raises(SessionExpiredError) as info:
        await assert_logged_in(as_session(ctx_of(401)), make_settings(tmp_path))
    assert type(info.value) is SessionExpiredError


async def test_assert_logged_in_login_redirect_raises_expired(tmp_path: Path) -> None:
    ctx = FakeContext([FakeResponse(302, {"location": LOGIN_REDIRECT})])
    with pytest.raises(SessionExpiredError):
        await assert_logged_in(as_session(ctx), make_settings(tmp_path))


async def test_assert_logged_in_challenge_raises_bot_challenge(tmp_path: Path) -> None:
    ctx = FakeContext([FakeResponse(403, body=CHALLENGE_BODY)])
    with pytest.raises(BotChallengeError):
        await assert_logged_in(as_session(ctx), make_settings(tmp_path))


async def test_assert_logged_in_unexpected_status_raises_check_error(tmp_path: Path) -> None:
    with pytest.raises(SessionCheckError, match="unexpected"):
        await assert_logged_in(as_session(ctx_of(500)), make_settings(tmp_path))


async def test_assert_logged_in_wraps_playwright_error(tmp_path: Path) -> None:
    original = PlaywrightError("boom")
    ctx = FakeContext([original])
    with pytest.raises(SessionCheckError) as info:
        await assert_logged_in(as_session(ctx), make_settings(tmp_path))
    assert info.value.__cause__ is original


async def test_session_check_logs_status_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = FakeContext([FakeResponse(403, {"set-cookie": LEAK_COOKIE}, LEAK_BODY + CHALLENGE_BODY)])
    with pytest.raises(BotChallengeError):
        await assert_logged_in(as_session(ctx), make_settings(tmp_path))
    err = capsys.readouterr().err
    assert '"msg": "session_check"' in err
    assert '"status": 403' in err
    assert LEAK_COOKIE not in err
    assert LEAK_BODY not in err


@pytest.mark.parametrize("initial_mode", [0o755, 0o777, 0o700])
def test_prepare_profile_dir_forces_0700_on_existing(tmp_path: Path, initial_mode: int) -> None:
    target = tmp_path / "p"
    target.mkdir()
    target.chmod(initial_mode)
    prepare_profile_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_prepare_profile_dir_creates_parents_with_0700(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "profile"
    prepare_profile_dir(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


class Recorder:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.now = 0.0
        self.step = 0.0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        value = self.now
        self.now += self.step
        return value


async def test_interactive_login_returns_after_third_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = ctx_of(401, 401, 200)
    rec = Recorder()
    await interactive_login(
        as_session(ctx), make_settings(tmp_path), poll_s=1.5, sleep=rec.sleep, clock=rec.clock
    )
    assert len(ctx.request.calls) == 3
    assert rec.sleeps == [1.5, 1.5]
    assert ctx.page.visited == [(HUMBLE_BASE + "/home/library", HTTP_TIMEOUT_S * 1000)]
    captured = capsys.readouterr()
    assert session.LOGIN_INSTRUCTION in captured.err
    assert captured.out == ""
    assert '"msg": "login_started"' in captured.err
    assert '"msg": "login_succeeded"' in captured.err


async def test_interactive_login_times_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = ctx_of(401)
    rec = Recorder()
    rec.step = 4.0
    with pytest.raises(SessionExpiredError, match="timed out"):
        await interactive_login(
            as_session(ctx), make_settings(tmp_path), poll_s=2.0, sleep=rec.sleep, clock=rec.clock
        )
    assert len(ctx.request.calls) == 2
    assert rec.sleeps == [2.0]
    assert '"msg": "login_timed_out"' in capsys.readouterr().err


async def test_interactive_login_survives_unexpected_probes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = FakeContext(
        [
            FakeResponse(404, {"set-cookie": LEAK_COOKIE}, LEAK_BODY),
            FakeResponse(404),
            FakeResponse(200),
        ]
    )
    rec = Recorder()
    await interactive_login(
        as_session(ctx), make_settings(tmp_path), poll_s=1.0, sleep=rec.sleep, clock=rec.clock
    )
    assert len(ctx.request.calls) == 3
    err = capsys.readouterr().err
    assert err.count('"msg": "session_check_unexpected"') == 2
    assert LEAK_BODY not in err
    assert LEAK_COOKIE not in err


async def test_interactive_login_unexpected_forever_times_out(tmp_path: Path) -> None:
    ctx = FakeContext([FakeResponse(404), PlaywrightError("net")])
    rec = Recorder()
    rec.step = 4.0
    with pytest.raises(SessionExpiredError, match="timed out"):
        await interactive_login(
            as_session(ctx), make_settings(tmp_path), poll_s=2.0, sleep=rec.sleep, clock=rec.clock
        )
    assert len(ctx.request.calls) == 2


async def test_fetch_identity_secrets_returns_traits_and_ids(tmp_path: Path) -> None:
    ctx = FakeContext([FakeResponse(200, json_body=WHOAMI_BODY)])
    result = await fetch_identity_secrets(as_session(ctx), make_settings(tmp_path))
    assert result == [EMAIL, "Jane", "Doe", NAME, IDENTITY_ID, SESSION_ID]
    assert ctx.request.calls == [(SESSION_PROBE_URL, API_TIMEOUT_S * 1000, 0)]


async def test_fetch_identity_secrets_deduplicates(tmp_path: Path) -> None:
    body = {"id": "x", "identity": {"id": "x", "traits": {"a": "x", "b": ["x", 5]}}}
    ctx = FakeContext([FakeResponse(200, json_body=body)])
    assert await fetch_identity_secrets(as_session(ctx), make_settings(tmp_path)) == ["x"]


@pytest.mark.parametrize("body", [[], {"identity": "str"}, {}])
async def test_fetch_identity_secrets_tolerates_odd_shapes(tmp_path: Path, body: Any) -> None:
    ctx = FakeContext([FakeResponse(200, json_body=body)])
    assert await fetch_identity_secrets(as_session(ctx), make_settings(tmp_path)) == []


async def test_fetch_identity_secrets_empty_on_401(tmp_path: Path) -> None:
    ctx = FakeContext([FakeResponse(401, json_body=WHOAMI_BODY)])
    assert await fetch_identity_secrets(as_session(ctx), make_settings(tmp_path)) == []


async def test_fetch_identity_secrets_wraps_playwright_error(tmp_path: Path) -> None:
    original = PlaywrightError("bad json")
    ctx = FakeContext([FakeResponse(200, json_body=original)])
    with pytest.raises(SessionCheckError) as info:
        await fetch_identity_secrets(as_session(ctx), make_settings(tmp_path))
    assert info.value.__cause__ is original


async def test_identity_values_never_logged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = FakeContext([FakeResponse(200, json_body=WHOAMI_BODY)])
    settings = make_settings(tmp_path)
    await assert_logged_in(as_session(ctx), settings)
    await fetch_identity_secrets(as_session(ctx), settings)
    err = capsys.readouterr().err
    for value in (EMAIL, NAME, IDENTITY_ID, SESSION_ID):
        assert value not in err


async def test_interactive_login_propagates_bot_challenge(tmp_path: Path) -> None:
    ctx = FakeContext([FakeResponse(403, body=CHALLENGE_BODY)])
    rec = Recorder()
    with pytest.raises(BotChallengeError):
        await interactive_login(
            as_session(ctx), make_settings(tmp_path), sleep=rec.sleep, clock=rec.clock
        )
    assert rec.sleeps == []


async def test_interactive_login_wraps_page_error(tmp_path: Path) -> None:
    original = PlaywrightError("nav failed")
    ctx = FakeContext([FakeResponse(200)], page_error=original)
    with pytest.raises(SessionCheckError) as info:
        await interactive_login(as_session(ctx), make_settings(tmp_path))
    assert info.value.__cause__ is original
    assert ctx.request.calls == []


class FakeBrowserContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeBrowserContext) -> None:
        self.context = context
        self.launches: list[tuple[str, bool]] = []

    async def launch_persistent_context(
        self, user_data_dir: str, *, headless: bool
    ) -> FakeBrowserContext:
        self.launches.append((user_data_dir, headless))
        return self.context


@pytest.mark.parametrize("headed", [True, False])
async def test_open_context_prepares_profile_launches_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, headed: bool
) -> None:
    context = FakeBrowserContext()
    chromium = FakeChromium(context)

    class FakePlaywright:
        pass

    fake = FakePlaywright()
    fake.chromium = chromium  # type: ignore[attr-defined]

    @asynccontextmanager
    async def fake_async_playwright() -> AsyncGenerator[Any]:
        yield fake

    monkeypatch.setattr(session, "async_playwright", fake_async_playwright)
    settings = make_settings(tmp_path)
    settings.profile_dir.mkdir()
    settings.profile_dir.chmod(0o755)
    async with open_context(settings, headed=headed) as ctx:
        assert ctx is context
        assert context.closed is False
        assert stat.S_IMODE(settings.profile_dir.stat().st_mode) == 0o700
    assert context.closed is True
    assert chromium.launches == [(str(settings.profile_dir), not headed)]


@pytest.mark.live
async def test_live_open_context_headless(tmp_path: Path) -> None:
    async with open_context(make_settings(tmp_path), headed=False) as ctx:
        assert ctx.pages is not None


EXPIRES_ISO = "2026-11-19T18:44:57.699884632Z"
EXPIRES_POSIX = 1795113897.699884
NOW = 1_000_000.0
FALLBACK_S = 86400
SECRET_VALUE = "SECRET-COOKIE-VALUE-xyz"  # noqa: S105
SESSION_NAME = "_kratos_session"


def real_context_as_jar(real: BrowserContext) -> CookieJar:
    return real  # static check: pyright fails if BrowserContext is not structural match


def make_cookie(
    name: str = SESSION_NAME,
    domain: str = ".humblebundle.com",
    expires: float = -1,
    value: str = SECRET_VALUE,
) -> Cookie:
    return Cookie(
        name=name,
        value=value,
        domain=domain,
        path="/",
        expires=expires,
        httpOnly=True,
        secure=True,
        sameSite="Lax",
    )


class FakeJarContext(FakeContext):
    def __init__(
        self,
        responses: list[FakeResponse | Exception],
        cookies: list[Cookie] | None = None,
        cookie_error: Exception | None = None,
    ) -> None:
        super().__init__(responses)
        self.stored = cookies or []
        self.cookie_error = cookie_error
        self.added: list[list[SetCookieParam]] = []

    async def cookies(self) -> list[Cookie]:
        if self.cookie_error:
            raise self.cookie_error
        return self.stored

    async def add_cookies(self, cookies: Any) -> None:
        self.added.append(list(cookies))


def as_jar(ctx: FakeJarContext) -> CookieJar:
    return ctx


def test_parse_session_expiry_handles_nine_digit_fraction_and_z() -> None:
    assert parse_session_expiry({"expires_at": EXPIRES_ISO}) == pytest.approx(EXPIRES_POSIX)


def test_parse_session_expiry_treats_naive_as_utc() -> None:
    assert parse_session_expiry({"expires_at": "1970-01-02T00:00:00"}) == 86400.0


@pytest.mark.parametrize(
    "body", [{}, {"expires_at": "garbage"}, {"expires_at": 5}, [], "x", None, {"id": "x"}]
)
def test_parse_session_expiry_none_for_missing_or_garbage(body: object) -> None:
    assert parse_session_expiry(body) is None


def test_cookies_to_persist_keeps_attributes_and_sets_expiry() -> None:
    result = cookies_to_persist([make_cookie()], 123.0)
    assert result == [
        {
            "name": SESSION_NAME,
            "value": SECRET_VALUE,
            "domain": ".humblebundle.com",
            "path": "/",
            "expires": 123.0,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }
    ]


def test_cookies_to_persist_omits_absent_optional_attributes() -> None:
    bare = Cookie(name="a", value="b", domain="www.humblebundle.com", path="/p", expires=0)
    assert cookies_to_persist([bare], 5.0) == [
        {"name": "a", "value": "b", "domain": "www.humblebundle.com", "path": "/p", "expires": 5.0}
    ]


def test_cookies_to_persist_filters_by_domain_and_expiry() -> None:
    cookies = [
        make_cookie("keep1", ".auth.humblebundle.com"),
        make_cookie("keep2", "www.humblebundle.com"),
        make_cookie("keep3", "humblebundle.com", expires=0),
        make_cookie("persistent", ".humblebundle.com", expires=999.0),
        make_cookie("other", ".example.com"),
        make_cookie("lookalike", ".nothumblebundle.com"),
    ]
    assert [c.get("name") for c in cookies_to_persist(cookies, 1.0)] == ["keep1", "keep2", "keep3"]


async def test_persist_adds_only_session_cookies_with_parsed_expiry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cookies = [
        make_cookie("s1"),
        make_cookie("cf", expires=NOW + 5),
        make_cookie("s2", ".auth.humblebundle.com"),
        make_cookie("x", ".example.com"),
    ]
    expiry = NOW + 1000
    body = {"expires_at": "1970-01-12T14:03:20Z"}  # NOW + 1000 s
    ctx = FakeJarContext([FakeResponse(200, json_body=body)], cookies)
    count = await persist_session_cookies(as_jar(ctx), make_settings(tmp_path), now=lambda: NOW)
    assert count == 2
    assert len(ctx.added) == 1
    assert [(p.get("name"), p.get("expires")) for p in ctx.added[0]] == [
        ("s1", expiry),
        ("s2", expiry),
    ]
    assert ctx.request.calls == [(SESSION_PROBE_URL, API_TIMEOUT_S * 1000, 0)]
    err = capsys.readouterr().err
    assert '"msg": "session_cookies_persisted"' in err
    assert '"count": 2' in err
    assert '"names": ["s1", "s2"]' in err
    assert SECRET_VALUE not in err
    assert "session_expiry_unknown" not in err


@pytest.mark.parametrize("body", [{}, {"expires_at": "1970-01-01T00:00:01Z"}])
async def test_persist_falls_back_when_expiry_unknown_or_past(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: object
) -> None:
    ctx = FakeJarContext([FakeResponse(200, json_body=body)], [make_cookie()])
    count = await persist_session_cookies(as_jar(ctx), make_settings(tmp_path), now=lambda: NOW)
    assert count == 1
    assert ctx.added[0][0].get("expires") == NOW + FALLBACK_S
    err = capsys.readouterr().err
    assert '"msg": "session_expiry_unknown"' in err
    assert SECRET_VALUE not in err


async def test_persist_warns_when_no_session_cookie_found(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = FakeJarContext([FakeResponse(200, json_body={"expires_at": EXPIRES_ISO})], [])
    assert await persist_session_cookies(as_jar(ctx), make_settings(tmp_path)) == 0
    assert ctx.added == [[]]
    assert '"msg": "session_cookie_not_found"' in capsys.readouterr().err


async def test_persist_401_raises_expired_without_touching_cookies(tmp_path: Path) -> None:
    ctx = FakeJarContext([FakeResponse(401)], [make_cookie()])
    with pytest.raises(SessionExpiredError):
        await persist_session_cookies(as_jar(ctx), make_settings(tmp_path))
    assert ctx.added == []


async def test_persist_wraps_request_error(tmp_path: Path) -> None:
    original = PlaywrightError("net")
    ctx = FakeJarContext([original])
    with pytest.raises(SessionCheckError) as info:
        await persist_session_cookies(as_jar(ctx), make_settings(tmp_path))
    assert info.value.__cause__ is original


async def test_persist_wraps_cookie_error(tmp_path: Path) -> None:
    original = PlaywrightError("closed")
    ctx = FakeJarContext([FakeResponse(200, json_body={})], cookie_error=original)
    with pytest.raises(SessionCheckError) as info:
        await persist_session_cookies(as_jar(ctx), make_settings(tmp_path))
    assert info.value.__cause__ is original
