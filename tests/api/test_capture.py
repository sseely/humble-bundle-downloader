"""Tests for humble_dl.api.capture using fake contexts (no real browser)."""

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import pytest
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from humble_dl.api import capture
from humble_dl.api.capture import (
    EMAIL_PLACEHOLDER,
    INDEX_NAME,
    MAX_ORDERS,
    MAX_PAGE_RESPONSES,
    CaptureContext,
    CaptureError,
    CaptureLeakError,
    Record,
    assert_clean,
    build_outputs,
    is_humble_host,
    list_gamekeys,
    run_capture,
    sanitize,
)
from humble_dl.auth.session import HUMBLE_BASE, BotChallengeError, SessionExpiredError
from humble_dl.config import Settings

JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
TEXT_HEADERS = {"content-type": "text/plain"}
USER_ORDER_URL = f"{HUMBLE_BASE}/api/v1/user/order"
KEY_A = "AbC123xyZ789"
KEY_B = "QwErTy456uIoP"
EMAIL = "reader@example.com"
HEADER_LEAK = "HEADER-SECRET-VALUE"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403


def order_url(key: str) -> str:
    return f"{HUMBLE_BASE}/api/v1/order/{key}?all_tpkds=true"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        output_dir=tmp_path / "out",
        profile_dir=tmp_path / "profile",
        debug_dir=tmp_path / "debug",
        hash_cache_path=tmp_path / "hash.json",
        concurrency=1,
        api_rps=2.0,
        http_timeout_s=20.0,
        api_timeout_s=7.0,
        login_timeout_s=10.0,
    )


@dataclass
class FakeRequest:
    method: str


@dataclass
class FakeResponse:
    url: str = ""
    status: int = HTTP_OK
    method: str = "GET"
    body: str = "{}"
    headers: dict[str, str] = field(default_factory=lambda: dict(JSON_HEADERS))
    text_error: Exception | None = None

    @property
    def request(self) -> FakeRequest:
        return FakeRequest(self.method)

    async def text(self) -> str:
        if self.text_error is not None:
            raise self.text_error
        return self.body


def json_response(url: str, body: Any, status: int = HTTP_OK) -> FakeResponse:
    return FakeResponse(url=url, status=status, body=json.dumps(body))


class FakeRequester:
    def __init__(
        self,
        routes: dict[str, FakeResponse] | None = None,
        probe: FakeResponse | None = None,
        raises: dict[str, Exception] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.probe = probe or FakeResponse()
        self.raises = raises or {}
        self.calls: list[str] = []

    async def get(
        self, url: str, *, timeout: float | None = None, max_redirects: int | None = None
    ) -> FakeResponse:
        if max_redirects == 0:
            return self.probe
        self.calls.append(url)
        if url in self.raises:
            raise self.raises[url]
        default = FakeResponse(url=url, status=HTTP_NOT_FOUND, body="nope", headers=TEXT_HEADERS)
        return self.routes.get(url, default)


class FakeMouse:
    def __init__(self) -> None:
        self.wheels: list[tuple[float, float]] = []

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        self.wheels.append((delta_x, delta_y))


class FakePage:
    def __init__(
        self,
        responses: list[FakeResponse],
        goto_error: Exception | None = None,
        idle_error: Exception | None = None,
    ) -> None:
        self.mouse = FakeMouse()
        self.responses = responses
        self.goto_error = goto_error
        self.idle_error = idle_error
        self.handlers: list[Callable[[Any], None]] = []
        self.visited: list[str] = []

    def on(self, event: Literal["response"], f: Callable[[Any], None]) -> None:
        self.handlers.append(f)

    async def goto(self, url: str, *, timeout: float | None = None) -> object:
        self.visited.append(url)
        if self.goto_error is not None:
            raise self.goto_error
        for response in self.responses:
            for handler in self.handlers:
                handler(response)
        return None

    async def wait_for_load_state(
        self, state: Literal["networkidle"] | None = None, *, timeout: float | None = None
    ) -> None:
        if self.idle_error is not None:
            raise self.idle_error


class FakeContext:
    def __init__(self, page: FakePage, request: FakeRequester) -> None:
        self.page = page
        self.request = request

    async def new_page(self) -> FakePage:
        return self.page


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def real_context_satisfies_protocol(ctx: BrowserContext) -> CaptureContext:
    """Static check only: pyright fails if Playwright's BrowserContext drifts."""
    return ctx


def make_ctx(
    page_responses: list[FakeResponse] | None = None,
    requester: FakeRequester | None = None,
    **page_kwargs: Any,
) -> FakeContext:
    return FakeContext(FakePage(page_responses or [], **page_kwargs), requester or FakeRequester())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def all_output_text(out_dir: Path) -> str:
    return "\n".join(p.name + "\n" + p.read_text(encoding="utf-8") for p in out_dir.iterdir())


# --- sanitize ----------------------------------------------------------------


def test_sanitize_acceptance_payload() -> None:
    payload = {
        "orders": [{"gamekey": "AbC123", "dl": "https://dl.humble.com/f.pdf?gamekey=AbC123&t=x"}],
        "email": "a@b.com",
        "note": "contact a@b.com today",
    }
    gamekey_map: dict[str, str] = {}

    result = sanitize(payload, gamekey_map)

    assert gamekey_map == {"AbC123": "GAMEKEY_0001"}
    assert result == {
        "orders": [{"gamekey": "GAMEKEY_0001", "dl": "https://dl.humble.com/f.pdf"}],
        "note": f"contact {EMAIL_PLACEHOLDER} today",
    }
    dumped = json.dumps(result)
    for leaked in ("AbC123", "t=x", "a@b.com"):
        assert leaked not in dumped


def test_sanitize_shared_map_is_stable_across_payloads() -> None:
    gamekey_map: dict[str, str] = {}

    first = sanitize({"gamekey": "K1"}, gamekey_map)
    second = sanitize([{"gamekey": "K2"}, {"gamekey": "K1"}], gamekey_map)

    assert first == {"gamekey": "GAMEKEY_0001"}
    assert second == [{"gamekey": "GAMEKEY_0002"}, {"gamekey": "GAMEKEY_0001"}]


def test_sanitize_remaps_gamekey_in_url_path_and_dict_keys() -> None:
    gamekey_map = {"PathKey99": "GAMEKEY_0001"}

    result = sanitize(
        {"PathKey99": "https://www.humblebundle.com/api/v1/order/PathKey99?x=1"}, gamekey_map
    )

    assert result == {"GAMEKEY_0001": "https://www.humblebundle.com/api/v1/order/GAMEKEY_0001"}


@pytest.mark.parametrize(
    "key",
    ["Email", "X-CSRF-Token", "session_id", "api_key", "Cookie", "UID", "user_id", "ip", "card"]
    + ["Authorization", "auth", "billing", "payment", "phone", "address", "password_hash"]
    + ["username", "full_name", "First_Name", "last_name", "display_name"],
)
def test_sanitize_drops_secret_keys(key: str) -> None:
    assert sanitize({key: "v", "name": "keep"}, {}) == {"name": "keep"}


def test_sanitize_keeps_fields_needed_downstream() -> None:
    payload: dict[str, Any] = {
        "name": "n",
        "human_name": "h",
        "machine_name": "m",
        "url": {"web": "https://dl.humble.com/a.pdf?token=zzz"},
        "md5": "d41d8cd98f00b204e9800998ecf8427e",
        "sha1": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
        "file_size": 12,
        "platform": "ebook",
        "download_struct": [],
    }

    result = sanitize(payload, {})

    assert result == {**payload, "url": {"web": "https://dl.humble.com/a.pdf"}}


def test_sanitize_drops_non_string_gamekey_and_passes_scalars() -> None:
    assert sanitize({"gamekey": 5, "n": 1, "f": 1.5, "b": True, "z": None}, {}) == {
        "n": 1,
        "f": 1.5,
        "b": True,
        "z": None,
    }


def test_sanitize_replaces_longest_gamekey_first() -> None:
    gamekey_map = {"abc": "GAMEKEY_0001", "abcdef": "GAMEKEY_0002"}

    assert sanitize("x abcdef y", gamekey_map) == "x GAMEKEY_0002 y"


# --- assert_clean / helpers ---------------------------------------------------


def test_assert_clean_passes_when_no_secret_present() -> None:
    assert_clean("GAMEKEY_0001 user@example.invalid", [KEY_A, EMAIL, ""])


@pytest.mark.parametrize("text", [f"x {KEY_A} y", f"x {KEY_A.lower()} y", f"mail {EMAIL.upper()}"])
def test_assert_clean_raises_without_echoing_secret(text: str) -> None:
    with pytest.raises(CaptureLeakError) as info:
        assert_clean(text, [KEY_A, EMAIL])

    assert KEY_A not in str(info.value)
    assert EMAIL not in str(info.value)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.humblebundle.com/x", True),
        ("https://humblebundle.com/x", True),
        ("https://dl.humblebundle.com/x", True),
        ("https://evilhumblebundle.com/x", False),
        ("https://humblebundle.com.evil.net/x", False),
        ("https://cdn.example.com/x", False),
        ("not a url", False),
    ],
)
def test_is_humble_host(url: str, expected: bool) -> None:
    assert is_humble_host(url) is expected


def test_list_gamekeys_handles_strings_dicts_and_junk() -> None:
    body = ["StrKey1", {"gamekey": "DictKey2"}, {"other": 1}, 5, "", {"gamekey": 7}]

    assert list_gamekeys(body) == ["StrKey1", "DictKey2"]
    assert list_gamekeys({"gamekey": "x"}) == []


# --- build_outputs -------------------------------------------------------------


def rec(url: str, body: Any = None, *, status: int = HTTP_OK, has_json: bool = True) -> Record:
    return Record(url, "GET", status, has_json, body)


def test_build_outputs_remaps_gamekey_first_seen_only_in_url_path() -> None:
    records = [
        rec(order_url(KEY_A), {"product": {"human_name": "Bundle"}, "ref": f"see {KEY_A}"}),
        rec(f"{HUMBLE_BASE}/api/v1/other", {"link": f"{HUMBLE_BASE}/api/v1/order/{KEY_A}/x?q=1"}),
    ]

    files = build_outputs(records)

    assert list(files) == [
        "01-api-v1-order-gamekey-0001.json",
        "02-api-v1-other.json",
        INDEX_NAME,
    ]
    assert json.loads(files["01-api-v1-order-gamekey-0001.json"]) == {
        "product": {"human_name": "Bundle"},
        "ref": "see GAMEKEY_0001",
    }
    assert json.loads(files["02-api-v1-other.json"]) == {
        "link": f"{HUMBLE_BASE}/api/v1/order/GAMEKEY_0001/x"
    }
    assert f"`{HUMBLE_BASE}/api/v1/order/GAMEKEY_0001`" in files[INDEX_NAME]
    for name, text in files.items():
        assert KEY_A not in name + text


def test_build_outputs_collects_user_order_list_items_and_numbers_in_order() -> None:
    records = [
        rec(USER_ORDER_URL, [KEY_A, {"gamekey": KEY_B}]),
        rec(order_url(KEY_B), {"gamekey": KEY_B}),
    ]

    files = build_outputs(records)

    assert json.loads(files["01-api-v1-user-order.json"]) == [
        "GAMEKEY_0001",
        {"gamekey": "GAMEKEY_0002"},
    ]
    assert json.loads(files["02-api-v1-order-gamekey-0002.json"]) == {"gamekey": "GAMEKEY_0002"}


def test_build_outputs_index_lists_non_json_without_file_or_body() -> None:
    records = [
        rec(f"{HUMBLE_BASE}/api/v1/page?token=abc", status=HTTP_NOT_FOUND, has_json=False),
        rec(f"{HUMBLE_BASE}/", {"ok": True}),
    ]

    files = build_outputs(records)

    assert list(files) == ["02-root.json", INDEX_NAME]
    assert f"| 01 | GET | 404 | `{HUMBLE_BASE}/api/v1/page` | - |" in files[INDEX_NAME]
    assert "| 02 | GET | 200 |" in files[INDEX_NAME]
    assert "token" not in files[INDEX_NAME]


def test_build_outputs_slug_is_capped() -> None:
    files = build_outputs([rec(f"{HUMBLE_BASE}/{'a' * 300}", {})])

    assert list(files)[0] == f"01-{'a' * 80}.json"


def test_build_outputs_strips_captured_email_everywhere() -> None:
    records = [
        rec(
            f"{HUMBLE_BASE}/api/v1/me?email=reader%40example.com",
            {"email": EMAIL, "x": f"<{EMAIL}>"},
        )
    ]

    files = build_outputs(records)

    assert json.loads(files["01-api-v1-me.json"]) == {"x": f"<{EMAIL_PLACEHOLDER}>"}
    assert "reader" not in files[INDEX_NAME]


def test_build_outputs_remaps_case_variants_to_same_fake() -> None:
    records = [
        rec(order_url(KEY_A), {"gamekey": KEY_A, "note": f"{KEY_A.lower()} {KEY_A.upper()}"})
    ]

    files = build_outputs(records)

    assert json.loads(files["01-api-v1-order-gamekey-0001.json"]) == {
        "gamekey": "GAMEKEY_0001",
        "note": "GAMEKEY_0001 GAMEKEY_0001",
    }


def test_sanitize_prefix_key_cannot_partially_replace_longer_key() -> None:
    gamekey_map = {"abc": "GAMEKEY_0001", "ABCdef": "GAMEKEY_0002"}

    assert sanitize("abcDEF abc", gamekey_map) == "GAMEKEY_0002 GAMEKEY_0001"


def test_sanitize_escapes_regex_metacharacters_in_keys() -> None:
    assert sanitize("a+b a.b", {"a+b": "GAMEKEY_0001"}) == "GAMEKEY_0001 a.b"


# --- run_capture ---------------------------------------------------------------


async def test_run_capture_writes_sanitized_fixtures_and_index(tmp_path: Path) -> None:
    out_dir = tmp_path / "fixtures"
    page_responses = [
        FakeResponse(
            url=USER_ORDER_URL,
            body=json.dumps([{"gamekey": KEY_A}]),
            headers={**JSON_HEADERS, "x-secret": HEADER_LEAK},
        ),
        json_response(
            order_url(KEY_A), {"gamekey": KEY_A, "email": EMAIL, "product": {"human_name": "B"}}
        ),
        json_response("https://cdn.example.com/track.json", {"ignored": True}),
        FakeResponse(
            url=f"{HUMBLE_BASE}/styles.css", body="body{}", headers={"content-type": "text/css"}
        ),
    ]
    ctx = make_ctx(page_responses, FakeRequester())

    written = await run_capture(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

    names = [p.name for p in written]
    assert names == [
        "01-api-v1-user-order.json",
        "02-api-v1-order-gamekey-0001.json",
        INDEX_NAME,
    ]
    assert read_json(out_dir / names[0]) == [{"gamekey": "GAMEKEY_0001"}]
    assert read_json(out_dir / names[1]) == {
        "gamekey": "GAMEKEY_0001",
        "product": {"human_name": "B"},
    }
    index = (out_dir / INDEX_NAME).read_text(encoding="utf-8")
    assert f"| 01 | GET | 200 | `{USER_ORDER_URL}` | 01-api-v1-user-order.json |" in index
    assert "| 03 | GET | 200 | `https://www.humblebundle.com/styles.css` | - |" in index
    assert "| 04 | GET | 404 |" in index
    assert "cdn.example.com" not in index
    dumped = all_output_text(out_dir)
    for leaked in (KEY_A, EMAIL, HEADER_LEAK, "x-secret"):
        assert leaked not in dumped
    assert ctx.page.visited == [f"{HUMBLE_BASE}/library".replace("/library", "/home/library")]
    assert len(ctx.page.mouse.wheels) == 3


async def test_run_capture_legacy_calls_are_capped_and_rate_limited(tmp_path: Path) -> None:
    keys = [f"LegacyKey{i:02d}" for i in range(MAX_ORDERS + 2)]
    routes = {USER_ORDER_URL: json_response(USER_ORDER_URL, [{"gamekey": k} for k in keys])}
    routes.update({order_url(k): json_response(order_url(k), {"gamekey": k}) for k in keys})
    requester = FakeRequester(routes)
    sleep = FakeSleep()
    out_dir = tmp_path / "fixtures"

    written = await run_capture(
        make_ctx(requester=requester), make_settings(tmp_path), out_dir, sleep=sleep
    )

    assert requester.calls == [USER_ORDER_URL, *(order_url(k) for k in keys[:MAX_ORDERS])]
    assert sleep.calls == [0.5] * MAX_ORDERS
    assert len(written) == MAX_ORDERS + 2
    assert "GAMEKEY_0005" in (out_dir / INDEX_NAME).read_text(encoding="utf-8")
    assert "LegacyKey" not in all_output_text(out_dir)


async def test_run_capture_skips_orders_when_user_order_not_json(tmp_path: Path) -> None:
    requester = FakeRequester()
    sleep = FakeSleep()

    written = await run_capture(
        make_ctx(requester=requester), make_settings(tmp_path), tmp_path / "o", sleep=sleep
    )

    assert requester.calls == [USER_ORDER_URL]
    assert sleep.calls == []
    assert [p.name for p in written] == [INDEX_NAME]
    assert "| 01 | GET | 404 |" in written[0].read_text(encoding="utf-8")


async def test_run_capture_stops_recording_after_page_cap(tmp_path: Path) -> None:
    responses = [
        json_response(f"{HUMBLE_BASE}/api/v1/n{i}", {"i": i}) for i in range(MAX_PAGE_RESPONSES + 5)
    ]

    written = await run_capture(
        make_ctx(responses), make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep()
    )

    assert len(written) == MAX_PAGE_RESPONSES + 1
    assert (tmp_path / "o" / f"{MAX_PAGE_RESPONSES}-api-v1-n{MAX_PAGE_RESPONSES - 1}.json").exists()


async def test_run_capture_unreadable_or_invalid_bodies_are_index_only(tmp_path: Path) -> None:
    responses = [
        FakeResponse(url=f"{HUMBLE_BASE}/a", text_error=PlaywrightError("evicted")),
        FakeResponse(url=f"{HUMBLE_BASE}/b", body="{not json"),
    ]

    written = await run_capture(
        make_ctx(responses), make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep()
    )

    assert [p.name for p in written] == [INDEX_NAME]
    index = written[0].read_text(encoding="utf-8")
    assert "`https://www.humblebundle.com/a` | - |" in index
    assert "`https://www.humblebundle.com/b` | - |" in index


async def test_run_capture_tolerates_network_idle_timeout(tmp_path: Path) -> None:
    ctx = make_ctx(
        [json_response(f"{HUMBLE_BASE}/a", {"a": 1})], idle_error=PlaywrightTimeoutError("idle")
    )

    written = await run_capture(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

    assert [p.name for p in written] == ["01-a.json", INDEX_NAME]


async def test_run_capture_logs_redacted_paths_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = make_ctx([json_response(order_url(KEY_A), {"gamekey": KEY_A})])

    await run_capture(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

    lines = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    responses = [line for line in lines if line["msg"] == "capture_response"]
    complete = [line for line in lines if line["msg"] == "capture_complete"]
    assert responses[0] == {**responses[0], "path": "/api/v1/order/AbC1…", "status": 200}
    assert complete[0]["count"] == 2
    assert complete[0]["out_dir"] == str(tmp_path / "o")
    assert KEY_A not in json.dumps(lines)


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (HTTP_FORBIDDEN, "<html>captcha</html>", BotChallengeError),
        (HTTP_UNAUTHORIZED, "", SessionExpiredError),
    ],
)
async def test_run_capture_propagates_session_errors_and_writes_nothing(
    tmp_path: Path, status: int, body: str, error: type[Exception]
) -> None:
    probe = FakeResponse(status=status, body=body, headers=TEXT_HEADERS)
    ctx = make_ctx(requester=FakeRequester(probe=probe))
    out_dir = tmp_path / "o"

    with pytest.raises(error):
        await run_capture(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

    assert not out_dir.exists()
    assert ctx.page.visited == []


@pytest.mark.parametrize("failure", ["goto", "legacy"])
async def test_run_capture_wraps_browser_errors(tmp_path: Path, failure: str) -> None:
    requester = FakeRequester(
        raises={USER_ORDER_URL: PlaywrightError("boom")} if failure == "legacy" else None
    )
    goto_error = PlaywrightError("nav") if failure == "goto" else None
    out_dir = tmp_path / "o"

    with pytest.raises(CaptureError):
        await run_capture(
            make_ctx(requester=requester, goto_error=goto_error),
            make_settings(tmp_path),
            out_dir,
            sleep=FakeSleep(),
        )

    assert not out_dir.exists()


async def test_run_capture_writes_nothing_when_leak_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulate a sanitizer bug: the backstop must still stop the write.

    def identity(obj: Any, gamekey_map: dict[str, str]) -> Any:
        return obj

    monkeypatch.setattr(capture, "sanitize", identity)
    ctx = make_ctx([json_response(order_url(KEY_A), {"gamekey": KEY_A})])
    out_dir = tmp_path / "o"

    with pytest.raises(CaptureLeakError):
        await run_capture(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

    assert not out_dir.exists()


@pytest.mark.parametrize(("rps", "expected"), [(2.0, 0.5), (4.0, 0.25)])
async def test_run_capture_legacy_interval_derives_from_api_rps(
    tmp_path: Path, rps: float, expected: float
) -> None:
    routes = {USER_ORDER_URL: json_response(USER_ORDER_URL, [KEY_A])}
    sleep = FakeSleep()
    settings = dataclasses.replace(make_settings(tmp_path), api_rps=rps)

    await run_capture(
        make_ctx(requester=FakeRequester(routes)), settings, tmp_path / "o", sleep=sleep
    )

    assert sleep.calls == [expected]
