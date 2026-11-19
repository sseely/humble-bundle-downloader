"""Tests for humble_dl.api.capture using fake contexts (no real browser)."""

import dataclasses
import functools
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
    MAX_BODY_CHARS,
    MAX_ORDERS,
    MAX_PAGE_RESPONSES,
    CaptureContext,
    CaptureError,
    CaptureLeakError,
    Record,
    build_outputs,
    run_capture,
)
from humble_dl.api.capture_page import DISABLE_WINDOW_OPEN_JS, FORMAT_BUTTON_SELECTOR
from humble_dl.auth.session import (
    HUMBLE_BASE,
    BotChallengeError,
    SessionContext,
    SessionExpiredError,
)
from humble_dl.config import Settings

RSC_HEADERS = {"content-type": "text/x-component"}
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
TEXT_HEADERS = {"content-type": "text/plain"}
USER_ORDER_URL = f"{HUMBLE_BASE}/api/v1/user/order"
KEY_A = "AbC123xyZ789"
KEY_B = "QwErTy456uIoP"
EMAIL = "reader@example.com"
HEADER_LEAK = "HEADER-SECRET-VALUE"
LIBRARY_URL = f"{HUMBLE_BASE}/home/library"
DL_URL = "https://dl.humble.com/book.pdf?ttl=1700000000&t=SIGNEDVALUE"
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
    json_error: Exception | None = None

    @property
    def request(self) -> FakeRequest:
        return FakeRequest(self.method)

    async def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return json.loads(self.body)

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
    """Models Playwright: bodies of responses from an earlier page are evicted on goto."""

    def __init__(
        self,
        responses: list[FakeResponse],
        goto_error: Exception | None = None,
        idle_error: Exception | None = None,
        scripts: list[Any] | None = None,
        detail_responses: list[FakeResponse] | None = None,
        detail_error: Exception | None = None,
        click_responses: list[FakeResponse] | None = None,
        click_error: Exception | None = None,
    ) -> None:
        self.mouse = FakeMouse()
        self.scripts = scripts or []
        self.detail_responses = detail_responses or []
        self.detail_error = detail_error
        self.click_responses = click_responses or []
        self.click_error = click_error
        self.responses = responses
        self.goto_error = goto_error
        self.idle_error = idle_error
        self.handlers: list[Callable[[Any], None]] = []
        self.visited: list[str] = []
        self.evaluated: list[str] = []
        self.clicked: list[str] = []
        self.emitted: list[FakeResponse] = []

    def on(self, event: Literal["response"], f: Callable[[Any], None]) -> None:
        self.handlers.append(f)

    def _emit(self, responses: list[FakeResponse]) -> None:
        for response in responses:
            self.emitted.append(response)
            for handler in self.handlers:
                handler(response)

    async def goto(self, url: str, *, timeout: float | None = None) -> object:
        self.visited.append(url)
        first = len(self.visited) == 1
        error = self.goto_error if first else self.detail_error
        if error is not None:
            raise error
        for old in self.emitted:
            old.text_error = old.text_error or PlaywrightError("body evicted by navigation")
        self._emit(self.responses if first else self.detail_responses)
        return None

    async def wait_for_load_state(
        self, state: Literal["networkidle"] | None = None, *, timeout: float | None = None
    ) -> None:
        if self.idle_error is not None:
            raise self.idle_error

    async def eval_on_selector_all(self, selector: str, expression: str) -> Any:
        return self.scripts

    async def evaluate(self, expression: str) -> Any:
        self.evaluated.append(expression)
        return None

    async def dispatch_event(
        self, selector: str, type: str, *, timeout: float | None = None
    ) -> None:
        self.clicked.append(f"{type}:{selector}")
        if self.click_error is not None:
            raise self.click_error
        self._emit(self.click_responses)


class FakeContext:
    def __init__(
        self,
        page: FakePage,
        request: FakeRequester,
        cookies: list[dict[str, object]] | None = None,
    ) -> None:
        self.page = page
        self.request = request
        self._cookies = cookies or []

    async def cookies(self) -> Sequence[Mapping[str, object]]:
        return self._cookies

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
    cookies: list[dict[str, object]] | None = None,
    **page_kwargs: Any,
) -> FakeContext:
    page = FakePage(page_responses or [], **page_kwargs)
    return FakeContext(page, requester or FakeRequester(), cookies)


def identity_of(*values: str) -> Callable[[SessionContext, Settings], Awaitable[list[str]]]:
    async def fetch(ctx: SessionContext, settings: Settings) -> list[str]:
        return list(values)

    return fetch


capture_run = functools.partial(run_capture, fetch_identity=identity_of())


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def all_output_text(out_dir: Path) -> str:
    return "\n".join(p.name + "\n" + p.read_text(encoding="utf-8") for p in out_dir.iterdir())


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

    written = await capture_run(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

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
    assert f"| 01 | GET | 200 | `{USER_ORDER_URL}` | 01-api-v1-user-order.json | json |" in index
    assert (
        "| 03 | GET | 200 | `https://www.humblebundle.com/styles.css` | - | index-only |" in index
    )
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

    written = await capture_run(
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

    written = await capture_run(
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

    written = await capture_run(
        make_ctx(responses), make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep()
    )

    assert len(written) == MAX_PAGE_RESPONSES + 1
    assert (tmp_path / "o" / f"{MAX_PAGE_RESPONSES}-api-v1-n{MAX_PAGE_RESPONSES - 1}.json").exists()


async def test_run_capture_unreadable_or_invalid_bodies_are_index_only(tmp_path: Path) -> None:
    responses = [
        FakeResponse(url=f"{HUMBLE_BASE}/a", text_error=PlaywrightError("evicted")),
        FakeResponse(url=f"{HUMBLE_BASE}/b", body="{not json"),
    ]

    written = await capture_run(
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

    written = await capture_run(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

    assert [p.name for p in written] == ["01-a.json", INDEX_NAME]


async def test_run_capture_logs_redacted_paths_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = make_ctx([json_response(order_url(KEY_A), {"gamekey": KEY_A})])

    await capture_run(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

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
        await capture_run(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

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
        await capture_run(
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
        await capture_run(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

    assert not out_dir.exists()


@pytest.mark.parametrize(("rps", "expected"), [(2.0, 0.5), (4.0, 0.25)])
async def test_run_capture_legacy_interval_derives_from_api_rps(
    tmp_path: Path, rps: float, expected: float
) -> None:
    routes = {USER_ORDER_URL: json_response(USER_ORDER_URL, [KEY_A])}
    sleep = FakeSleep()
    settings = dataclasses.replace(make_settings(tmp_path), api_rps=rps)

    await capture_run(
        make_ctx(requester=FakeRequester(routes)), settings, tmp_path / "o", sleep=sleep
    )

    assert sleep.calls == [expected]


# --- flight / rsc / identity / cookies ---------------------------------------------

REGEX_KEY = "RegexOnlyKey42"
FLIGHT_TEXT = (
    f'0:{{"dl":"{DL_URL}","email":"{EMAIL}","owner":"Hi Jane Doe","gamekey":"{KEY_A}"}}\n'
    f'1:I raw jane DOE row {{"gamekey":"{REGEX_KEY}"}}\n'
)


def library_html(flight: str) -> str:
    pushes = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(chunk)}])</script>"
        for chunk in (flight[:10], flight[10:])
    )
    return f"<html><body><h1>RAW-HTML-MARKER</h1>{pushes}</body></html>"


def flight_record(url: str, flight: str, kind: str = "rsc") -> Record:
    return Record(url, "GET", HTTP_OK, False, None, flight=flight, flight_kind=kind)


def test_build_outputs_sanitizes_flight_records_and_names_them() -> None:
    records = [
        flight_record(f"{HUMBLE_BASE}/x/Jane Doe?_rsc=1", FLIGHT_TEXT),
        flight_record(LIBRARY_URL, FLIGHT_TEXT, "flight"),
    ]

    files = build_outputs(records, identity=["Jane Doe"])

    assert list(files) == ["01-x-redacted.rsc.txt", "02-library-flight.rsc.txt", INDEX_NAME]
    out = files["02-library-flight.rsc.txt"]
    assert out.splitlines() == [
        '0:{"dl":"https://dl.humble.com/book.pdf","owner":"Hi REDACTED","gamekey":"GAMEKEY_0001"}',
        '1:I raw REDACTED row {"gamekey":"GAMEKEY_0002"}',
    ]
    everything = "\n".join(files.values())
    for leaked in (KEY_A, REGEX_KEY, EMAIL, "SIGNEDVALUE", "ttl=", "jane"):
        assert leaked.lower() not in everything.lower()
    assert "| 01 | GET | 200 | `https://www.humblebundle.com/x/REDACTED` |" in files[INDEX_NAME]
    assert "01-x-redacted.rsc.txt | rsc |" in files[INDEX_NAME]
    assert "02-library-flight.rsc.txt | flight |" in files[INDEX_NAME]


def test_build_outputs_collects_gamekeys_from_urls_before_queries_are_stripped() -> None:
    records = [
        rec(f"{HUMBLE_BASE}/downloads?key=UrlQueryKey99", {"link": "/gift/GiftPathKey77"}),
        flight_record(f"{HUMBLE_BASE}/a", 'a:"see /gift/claim/x and /downloads?key=UrlQueryKey99"'),
    ]

    files = build_outputs(records)

    everything = "\n".join(files.values())
    assert "UrlQueryKey99" not in everything
    assert "GiftPathKey77" not in everything
    assert "/gift/claim/x" in files["02-a.rsc.txt"]


def test_build_outputs_cookie_value_in_output_is_a_leak() -> None:
    records = [flight_record(f"{HUMBLE_BASE}/a", '1:"cookie-value-12345"')]

    with pytest.raises(CaptureLeakError) as info:
        build_outputs(records, extra_secrets=["COOKIE-value-12345"])

    assert "cookie-value" not in str(info.value)


def test_build_outputs_identity_in_a_dumped_json_file_is_redacted() -> None:
    files = build_outputs([rec(f"{HUMBLE_BASE}/me", {"greet": "hello JANE doe"})], ["Jane Doe"])

    assert json.loads(files["01-me.json"]) == {"greet": "hello REDACTED"}


def test_build_outputs_marks_oversized_bodies_in_index() -> None:
    big = Record(f"{HUMBLE_BASE}/big", "GET", HTTP_OK, False, None, note="too large")

    files = build_outputs([big])

    assert files[INDEX_NAME].endswith(
        "| `https://www.humblebundle.com/big` | - (too large) | index-only |\n"
    )


def test_record_kind_values() -> None:
    assert rec(f"{HUMBLE_BASE}/").kind == "json"
    assert flight_record(f"{HUMBLE_BASE}/", "0:1", "flight").kind == "flight"
    assert rec(f"{HUMBLE_BASE}/", has_json=False).kind == "index-only"


def test_capture_reexports_sanitizer_public_names() -> None:
    from humble_dl.api import sanitize as sanitize_module

    assert capture.sanitize is sanitize_module.sanitize
    assert capture.CaptureLeakError is sanitize_module.CaptureLeakError
    assert capture.SECRET_KEYS is sanitize_module.SECRET_KEYS


def test_loggable_path_redacts_gift_gamekeys() -> None:
    logged = capture._loggable_path(f"{HUMBLE_BASE}/gift/{KEY_A}?x=1")  # pyright: ignore[reportPrivateUsage]

    assert logged == "/gift/AbC1…"


async def test_run_capture_records_json_rsc_and_library_document(tmp_path: Path) -> None:
    out_dir = tmp_path / "fixtures"
    responses = [
        FakeResponse(url=f"{HUMBLE_BASE}/api/x", body=json.dumps({"ok": 1})),
        FakeResponse(
            url=f"{HUMBLE_BASE}/home/library?_rsc=abc",
            body=FLIGHT_TEXT,
            headers=RSC_HEADERS,
        ),
        FakeResponse(url=LIBRARY_URL, body=library_html(FLIGHT_TEXT), headers=HTML_HEADERS),
        FakeResponse(url=f"{HUMBLE_BASE}/other", body="<html>other</html>", headers=HTML_HEADERS),
    ]
    ctx = make_ctx(responses, cookies=[{"name": "s", "value": "short"}])
    fetch = identity_of("Jane Doe", "ab")

    written = await run_capture(ctx, make_settings(tmp_path), out_dir, fetch_identity=fetch)

    names = [p.name for p in written]
    assert (
        names
        == [
            "01-api-x.json",
            "02-home-library.rsc.txt",
            "03-library-flight.rsc.txt",
            INDEX_NAME,
        ][:3]
        + [INDEX_NAME]
        or True
    )
    assert names == [
        "01-api-x.json",
        "02-home-library.rsc.txt",
        "03-library-flight.rsc.txt",
        INDEX_NAME,
    ]
    index = (out_dir / INDEX_NAME).read_text(encoding="utf-8")
    assert "01-api-x.json | json |" in index
    assert "02-home-library.rsc.txt | rsc |" in index
    assert "03-library-flight.rsc.txt | flight |" in index
    assert "| 04 | GET | 200 | `https://www.humblebundle.com/other` | - | index-only |" in index
    flight_out = (out_dir / names[2]).read_text(encoding="utf-8")
    assert flight_out == (out_dir / names[1]).read_text(encoding="utf-8")
    assert "RAW-HTML-MARKER" not in all_output_text(out_dir)
    assert "Jane" not in all_output_text(out_dir)
    assert "Hi REDACTED" in flight_out


async def test_run_capture_cookie_value_in_output_writes_nothing(tmp_path: Path) -> None:
    leaky_cookie = "sessionvalue-ABCDEF"
    responses = [json_response(f"{HUMBLE_BASE}/api/x", {"note": leaky_cookie})]
    ctx = make_ctx(responses, cookies=[{"name": "s", "value": leaky_cookie}])
    out_dir = tmp_path / "o"

    with pytest.raises(CaptureLeakError):
        await capture_run(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

    assert not out_dir.exists()
    assert list(tmp_path.glob("**/*.json")) == []


async def test_run_capture_cookie_read_failure_is_wrapped(tmp_path: Path) -> None:
    class BrokenContext(FakeContext):
        async def cookies(self) -> Sequence[Mapping[str, object]]:
            raise PlaywrightError("closed")

    ctx = BrokenContext(FakePage([]), FakeRequester())

    with pytest.raises(CaptureError):
        await capture_run(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())


async def test_run_capture_fetches_identity_once_after_login_check(tmp_path: Path) -> None:
    calls: list[int] = []

    async def fetch(ctx: SessionContext, settings: Settings) -> list[str]:
        calls.append(len(ctx.request.__class__.__name__))
        return []

    ctx = make_ctx()

    await run_capture(
        ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep(), fetch_identity=fetch
    )

    assert len(calls) == 1


async def test_run_capture_skips_identity_when_session_expired(tmp_path: Path) -> None:
    called: list[bool] = []

    async def fetch(ctx: SessionContext, settings: Settings) -> list[str]:
        called.append(True)
        return []

    probe = FakeResponse(status=HTTP_UNAUTHORIZED, body="", headers=TEXT_HEADERS)
    ctx = make_ctx(requester=FakeRequester(probe=probe))

    with pytest.raises(SessionExpiredError):
        await run_capture(ctx, make_settings(tmp_path), tmp_path / "o", fetch_identity=fetch)

    assert called == []


BUNDLE_ID = "c7a551c0-0000-4000-8000-00000000abcd"
BUNDLE_SCRIPT = (
    'self.__next_f.push([1,"11:{\\"productEntitlementId\\":\\"' + BUNDLE_ID + '\\"}\\n"])'
)
DETAIL_URL = f"{LIBRARY_URL}/{BUNDLE_ID}"


async def test_run_capture_opens_first_bundle_detail_page_and_records_its_flight(
    tmp_path: Path,
) -> None:
    detail = FakeResponse(url=DETAIL_URL, body=library_html(FLIGHT_TEXT), headers=HTML_HEADERS)
    ctx = make_ctx(scripts=[None, 7, "var a = 1;", BUNDLE_SCRIPT], detail_responses=[detail])
    out_dir = tmp_path / "o"

    written = await capture_run(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

    assert ctx.page.visited == [LIBRARY_URL, DETAIL_URL]
    assert written[0].name == "01-library-detail-flight.rsc.txt"
    assert written[0].read_text(encoding="utf-8").startswith("0:")
    assert "01-library-detail-flight.rsc.txt | flight |" in (out_dir / INDEX_NAME).read_text(
        encoding="utf-8"
    )


async def test_run_capture_logs_when_no_bundle_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = make_ctx(scripts=["var a = 1;", '"productEntitlementId":"nope"'])

    await capture_run(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

    messages = [
        json.loads(line)["msg"]
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("{")
    ]
    assert "capture_no_detail_link" in messages
    assert ctx.page.visited == [LIBRARY_URL]


async def test_run_capture_continues_when_detail_navigation_fails(tmp_path: Path) -> None:
    ctx = make_ctx(scripts=[BUNDLE_SCRIPT], detail_error=PlaywrightError("net::ERR_ABORTED"))

    written = await capture_run(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

    assert [p.name for p in written] == [INDEX_NAME]
    assert ctx.page.visited == [LIBRARY_URL, DETAIL_URL]


SIGNED_RSC = (
    '0:{"a":"$@1"}\n'
    '1:{"ok":true,"data":{"signedUrls":{"m":{"k":{"web":'
    '"https://cdn.x.net/TOKEN9SECRET/k.epub?Signature=abc"}}},"expiresAt":"2026-11-20T00:00:00Z"}}\n'
)


async def test_run_capture_reads_list_bodies_before_opening_bundle(tmp_path: Path) -> None:
    listing = FakeResponse(url=LIBRARY_URL, body=library_html(FLIGHT_TEXT), headers=HTML_HEADERS)
    detail = FakeResponse(url=DETAIL_URL, body=library_html(FLIGHT_TEXT), headers=HTML_HEADERS)
    ctx = make_ctx([listing], scripts=[BUNDLE_SCRIPT], detail_responses=[detail])

    written = await capture_run(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

    names = [p.name for p in written]
    assert names[:2] == ["01-library-flight.rsc.txt", "02-library-detail-flight.rsc.txt"]


async def test_run_capture_requests_signed_urls_with_window_open_disabled(tmp_path: Path) -> None:
    signed = FakeResponse(url=DETAIL_URL, method="POST", body=SIGNED_RSC, headers=RSC_HEADERS)
    ctx = make_ctx(scripts=[BUNDLE_SCRIPT], click_responses=[signed])
    out_dir = tmp_path / "o"

    written = await capture_run(ctx, make_settings(tmp_path), out_dir, sleep=FakeSleep())

    assert ctx.page.evaluated == [DISABLE_WINDOW_OPEN_JS]
    assert ctx.page.clicked == [f"click:{FORMAT_BUTTON_SELECTOR}"]
    text = written[0].read_text(encoding="utf-8")
    assert '"web":"https://cdn.x.net/REDACTED/k.epub"' in text
    assert "TOKEN9SECRET" not in all_output_text(out_dir)
    assert "Signature" not in all_output_text(out_dir)


async def test_run_capture_logs_and_continues_when_format_click_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    detail = FakeResponse(url=DETAIL_URL, body=library_html(FLIGHT_TEXT), headers=HTML_HEADERS)
    ctx = make_ctx(
        scripts=[BUNDLE_SCRIPT], detail_responses=[detail], click_error=PlaywrightError("none")
    )

    written = await capture_run(ctx, make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep())

    assert written[0].name == "01-library-detail-flight.rsc.txt"
    logs = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")
    ]
    failed = [entry for entry in logs if entry["msg"] == "capture_format_click_failed"]
    assert [entry["reason"] for entry in failed] == ["none"]


async def test_run_capture_skips_oversized_body_and_empty_flight(tmp_path: Path) -> None:
    responses = [
        FakeResponse(url=f"{HUMBLE_BASE}/huge", body="x" * (MAX_BODY_CHARS + 1)),
        FakeResponse(url=LIBRARY_URL, body="<html>no flight</html>", headers=HTML_HEADERS),
    ]

    written = await capture_run(
        make_ctx(responses), make_settings(tmp_path), tmp_path / "o", sleep=FakeSleep()
    )

    assert [p.name for p in written] == [INDEX_NAME]
    index = written[0].read_text(encoding="utf-8")
    assert "`https://www.humblebundle.com/huge` | - (too large) | index-only |" in index
    assert "`https://www.humblebundle.com/home/library` | - | index-only |" in index
