"""Tests for humble_dl.api.client using a scripted fake page."""

import dataclasses
import json
import logging
import stat
from pathlib import Path
from typing import Any

import pytest

from humble_dl.api import client as client_mod
from humble_dl.api.client import (
    ACTION_NOT_FOUND_MSG,
    JS_CALL_ACTION,
    JS_FETCH_TEXT,
    JS_LIST_CHUNKS,
    ActionVerdict,
    LibraryApiError,
    LibraryClient,
    backoff_delay,
    batched,
    classify_action,
    describe_missing,
    find_action_id,
)
from humble_dl.auth.session import BotChallengeError, SessionExpiredError
from humble_dl.config import Settings, load_settings
from humble_dl.models.library import (
    Download,
    FileVersion,
    Order,
    SchemaDriftError,
    Subproduct,
    ebook_items,
)

FIXTURES = Path(__file__).parent.parent / "helpers" / "fixtures" / "library"
BUNDLE_ID = "00000000-0000-4000-8000-000000000054"
ACTION_ID = "70f766da4d704c8423b78459588aa3e4a17c4b8a99"
CHUNK_URL = "https://www.humblebundle.com/_next/static/chunks/a.js"
CHUNK_TEXT = (
    f'x.createServerReference)("{ACTION_ID}",x.callServer,void 0,'
    'x.findSourceMapURL,"getSignedUrlsForEntitlements")'
)
LOGIN_URL = "https://auth.humblebundle.com/self-service/login/browser?return_to=/x"
CF_BODY = "<html><title>Attention Required! | Cloudflare</title></html>"


def _no_ebooks(order: Order) -> list[tuple[Subproduct, list[FileVersion]]]:
    return []


def html_of(flight: str) -> str:
    return f"<script>self.__next_f.push([1,{json.dumps(flight)}])</script>"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status


class FakePage:
    def __init__(self, html: str = "", status: int = 200) -> None:
        self.url = ""
        self.html = html
        self.status = status
        self.final_url: str | None = None
        self.gotos: list[tuple[str, float | None]] = []
        self.chunks: dict[str, str] = {CHUNK_URL: CHUNK_TEXT}
        self.action_responses: list[tuple[int, str]] = []
        self.action_calls: list[list[Any]] = []
        self.chunk_fetches = 0

    async def goto(self, url: str, *, timeout: float | None = None) -> FakeResponse:
        self.gotos.append((url, timeout))
        self.url = self.final_url or url
        return FakeResponse(self.status)

    async def content(self) -> str:
        return self.html

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        if expression == JS_LIST_CHUNKS:
            return list(self.chunks)
        if expression == JS_FETCH_TEXT:
            self.chunk_fetches += 1
            return self.chunks[arg]
        assert expression == JS_CALL_ACTION
        self.action_calls.append(arg)
        if len(self.action_responses) > 1:
            return list(self.action_responses.pop(0))
        return list(self.action_responses[0])


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return dataclasses.replace(load_settings(env={}, home=tmp_path), api_rps=1e9)


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


def make(page: FakePage, settings: Settings, ft: FakeTime) -> LibraryClient:
    return LibraryClient(page, settings, clock=ft.clock, sleep=ft.sleep)


def bundle_page() -> FakePage:
    page = FakePage(html_of(fixture("bundle.rsc.txt")))
    page.action_responses = [(200, fixture("signed_urls.rsc.txt"))]
    return page


async def test_list_bundles(settings: Settings, fake_time: FakeTime) -> None:
    page = FakePage(html_of(fixture("list.rsc.txt")))
    bundles = await make(page, settings, fake_time).list_bundles()
    assert len(bundles) == 12
    assert bundles[0].id == BUNDLE_ID
    assert page.gotos == [
        ("https://www.humblebundle.com/home/library", settings.api_timeout_s * 1000)
    ]


async def test_login_redirect_is_session_expired(settings: Settings, fake_time: FakeTime) -> None:
    page = FakePage("")
    page.final_url = LOGIN_URL
    with pytest.raises(SessionExpiredError):
        await make(page, settings, fake_time).list_bundles()


async def test_login_path_on_main_host_is_session_expired(
    settings: Settings, fake_time: FakeTime
) -> None:
    page = FakePage("")
    page.final_url = "https://www.humblebundle.com/login?x=1"
    with pytest.raises(SessionExpiredError):
        await make(page, settings, fake_time).list_bundles()


async def test_cloudflare_block_is_bot_challenge(settings: Settings, fake_time: FakeTime) -> None:
    page = FakePage(CF_BODY, status=403)
    with pytest.raises(BotChallengeError):
        await make(page, settings, fake_time).list_bundles()


async def test_non_2xx_navigation_is_api_error(settings: Settings, fake_time: FakeTime) -> None:
    page = FakePage("nope", status=404)
    with pytest.raises(LibraryApiError, match="status 404") as info:
        await make(page, settings, fake_time).list_bundles()
    assert "?" not in str(info.value)


async def test_get_order_applies_signed_urls(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    order, missing = await make(page, settings, fake_time).get_order(BUNDLE_ID)
    kaiju = next(s for s in order.subproducts if s.machine_name.startswith("kaiju"))
    urls = [f.url for d in kaiju.downloads for f in d.files]
    assert "https://cdn.example.invalid/kaiju.epub?sig=1" in urls
    assert len(missing) == 20
    assert len(page.action_calls) == 1
    url, action_id, args = page.action_calls[0]
    assert url == f"https://www.humblebundle.com/home/library/{BUNDLE_ID}"
    assert action_id == ACTION_ID
    assert len(args) == 1
    assert len(args[0]) == 22
    assert len(set(args[0])) == 22


async def test_batching_splits_calls(
    settings: Settings, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client_mod, "SIGNED_URL_BATCH", 10)
    page = bundle_page()
    await make(page, settings, fake_time).get_order(BUNDLE_ID)
    assert [len(call[2][0]) for call in page.action_calls] == [10, 10, 2]


async def test_no_ebooks_makes_no_action_call(
    settings: Settings, fake_time: FakeTime, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = bundle_page()
    monkeypatch.setattr(client_mod, "ebook_items", _no_ebooks)
    order, missing = await make(page, settings, fake_time).get_order(BUNDLE_ID)
    assert page.action_calls == []
    assert page.chunk_fetches == 0
    assert order.gamekey == BUNDLE_ID
    assert len(missing) == 22


async def test_get_bundle_parses_without_signing(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    order = await make(page, settings, fake_time).get_bundle(BUNDLE_ID)
    titles = {s.human_name for s in order.subproducts}
    assert "The Kaiju Preservation Society" in titles
    assert order.human_name.startswith("Humble Book Bundle: The John Scalzi Collection")
    assert page.action_calls == []
    assert page.chunk_fetches == 0


async def test_refresh_returns_order(settings: Settings, fake_time: FakeTime) -> None:
    order = await make(bundle_page(), settings, fake_time).refresh(BUNDLE_ID)
    assert order.gamekey == BUNDLE_ID


async def test_action_not_found(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    page.chunks = {CHUNK_URL: "nothing here"}
    with pytest.raises(LibraryApiError, match=ACTION_NOT_FOUND_MSG):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)


async def test_action_id_discovered_once(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    cl = make(page, settings, fake_time)
    await cl.get_order(BUNDLE_ID)
    await cl.get_order(BUNDLE_ID)
    assert page.chunk_fetches == 1
    assert len(page.action_calls) == 2


async def test_retries_429_with_backoff(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    ok = (200, fixture("signed_urls.rsc.txt"))
    page.action_responses = [(429, ""), (429, ""), ok]
    await make(page, settings, fake_time).get_order(BUNDLE_ID)
    assert len(page.action_calls) == 3
    assert [d for d in fake_time.sleeps if d >= 1] == [1, 2]


async def test_retries_exhausted(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    page.action_responses = [(502, "")]
    with pytest.raises(LibraryApiError, match="status 502"):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)
    assert len(page.action_calls) == 5
    assert [d for d in fake_time.sleeps if d >= 1] == [1, 2, 4, 8]


UPSTREAM_ERROR = '0:{"a":"$@1"}\n1:{"ok":false,"code":"upstream_error","retryable":true}\n'
FORBIDDEN_ERROR = '0:{"a":"$@1"}\n1:{"ok":false,"code":"forbidden","retryable":false}\n'


async def test_retryable_signed_url_error_is_retried(
    settings: Settings, fake_time: FakeTime
) -> None:
    # Observed live: HTTP 200 with {"ok":false,"code":"upstream_error","retryable":true}.
    page = bundle_page()
    ok = (200, fixture("signed_urls.rsc.txt"))
    page.action_responses = [(200, UPSTREAM_ERROR), (200, UPSTREAM_ERROR), ok]

    order, _ = await make(page, settings, fake_time).get_order(BUNDLE_ID)

    assert len(page.action_calls) == 3
    assert [d for d in fake_time.sleeps if d >= 1] == [1, 2]
    assert any(
        f.url.startswith("https://cdn.example.invalid/") for _, fs in ebook_items(order) for f in fs
    )
    assert list(settings.debug_dir.glob("*")) == []


async def test_retryable_signed_url_error_exhausted(
    settings: Settings, fake_time: FakeTime
) -> None:
    page = bundle_page()
    page.action_responses = [(200, UPSTREAM_ERROR)]

    with pytest.raises(LibraryApiError, match="upstream_error"):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)

    assert len(page.action_calls) == 5
    assert [d for d in fake_time.sleeps if d >= 1] == [1, 2, 4, 8]


async def test_non_retryable_signed_url_error_fails_immediately(
    settings: Settings, fake_time: FakeTime
) -> None:
    page = bundle_page()
    page.action_responses = [(200, FORBIDDEN_ERROR)]

    with pytest.raises(LibraryApiError, match="forbidden"):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)

    assert len(page.action_calls) == 1


async def test_action_challenge(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    page.action_responses = [(403, CF_BODY)]
    with pytest.raises(BotChallengeError):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)


async def test_action_other_status_fails_immediately(
    settings: Settings, fake_time: FakeTime
) -> None:
    page = bundle_page()
    page.action_responses = [(400, "")]
    with pytest.raises(LibraryApiError, match="status 400"):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)
    assert len(page.action_calls) == 1


async def test_rate_limit(settings: Settings, fake_time: FakeTime) -> None:
    page = FakePage(html_of(fixture("list.rsc.txt")))
    cl = make(page, dataclasses.replace(settings, api_rps=2.0), fake_time)
    for _ in range(10):
        await cl.list_bundles()
    assert fake_time.now >= 4.5


async def test_drift_writes_debug_file(settings: Settings, fake_time: FakeTime) -> None:
    flight = '1:{"nothing":true}\n'
    page = FakePage(html_of(flight))
    with pytest.raises(SchemaDriftError):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)
    files = list(settings.debug_dir.glob("bundle-*.txt"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert files[0].read_text(encoding="utf-8") == flight


async def test_signed_drift_dumps_body(settings: Settings, fake_time: FakeTime) -> None:
    page = bundle_page()
    page.action_responses = [(200, "garbage")]
    with pytest.raises(SchemaDriftError):
        await make(page, settings, fake_time).get_order(BUNDLE_ID)
    assert list(settings.debug_dir.glob("signed-urls-*.txt"))


async def test_logs_have_no_query_or_signature(
    settings: Settings, fake_time: FakeTime, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    page = bundle_page()
    page.action_responses = [(429, ""), (200, fixture("signed_urls.rsc.txt"))]
    logger = logging.getLogger(client_mod.__name__)
    logger.addHandler(caplog.handler)
    try:
        await make(page, settings, fake_time).get_order(BUNDLE_ID)
    finally:
        logger.removeHandler(caplog.handler)
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "api_request" in text
    assert "api_backoff" in text
    assert "signed_urls_missing" in text
    assert "cdn.example.invalid" not in text
    assert "Signature" not in text


def test_find_action_id() -> None:
    assert find_action_id(CHUNK_TEXT) == ACTION_ID
    assert find_action_id('createServerReference)("abc",x,"other")') is None


def test_batched() -> None:
    assert list(batched(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]
    assert list(batched([], 2)) == []


@pytest.mark.parametrize(
    ("status", "body", "verdict"),
    [
        (200, "", ActionVerdict.OK),
        (403, CF_BODY, ActionVerdict.CHALLENGE),
        (403, "forbidden", ActionVerdict.FAIL),
        (429, "", ActionVerdict.RETRY),
        (500, "", ActionVerdict.RETRY),
        (404, "", ActionVerdict.FAIL),
    ],
)
def test_classify_action(status: int, body: str, verdict: ActionVerdict) -> None:
    assert classify_action(status, body) is verdict


def test_backoff_delay() -> None:
    assert [backoff_delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]


def test_describe_missing_names_keys_and_available_files() -> None:
    files = [
        FileVersion(label="EPUB", url="a.epub", md5=None, file_size=None),
        FileVersion(label="PDF", url="a.pdf", md5=None, file_size=None),
    ]
    sub = Subproduct(
        machine_name="a",
        human_name="A",
        downloads=[Download(platform="ebook", key="a_ebook", files=files)],
        entitlement_id="e1",
    )
    order = Order(gamekey="b", human_name="B", subproducts=[sub])
    signed = {"a_ebook": {"a.epub": "https://x/a.epub", "a_v2.pdf": "https://x/a_v2.pdf"}}

    assert describe_missing(order, signed) == [
        {"key": "a_ebook", "file": "a.pdf", "available": ["a.epub", "a_v2.pdf"]}
    ]
