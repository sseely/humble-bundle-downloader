"""Tests for humble_dl.api.bookmeta: Comic Vine and Open Library lookups."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from humble_dl.api.bookmeta import (
    OL_SEARCH,
    ComicVine,
    JsonFetcher,
    LookupFailedError,
    OpenLibrary,
    Pacing,
)
from humble_dl.services.book_info import CvCredit, CvIssue, CvVolume, OlDoc

SECRET = "k" * 40
Handler = Callable[[httpx.Request], httpx.Response]


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def fetcher(handler: Handler, ft: FakeTime, cache: dict[str, Any] | None = None) -> JsonFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return JsonFetcher(client, {} if cache is None else cache, Pacing(1.0, ft.clock, ft.sleep))


def ok(body: object) -> httpx.Response:
    return httpx.Response(200, json=body)


# --- JsonFetcher --------------------------------------------------------------


def test_fetcher_caches_without_secret_params_and_throttles() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return ok({"n": len(seen)})

    ft = FakeTime()
    cache: dict[str, Any] = {}
    fetch = fetcher(handler, ft, cache)
    assert fetch.get("https://x.test/a", {"q": "1"}, secret={"api_key": SECRET}) == {"n": 1}
    assert fetch.get("https://x.test/a", {"q": "1"}, secret={"api_key": SECRET}) == {"n": 1}
    assert fetch.get("https://x.test/a", {"q": "2"}) == {"n": 2}
    assert len(seen) == 2
    assert seen[0].params["api_key"] == SECRET
    assert SECRET not in json.dumps(cache)
    assert list(cache) == ["https://x.test/a?q=1", "https://x.test/a?q=2"]
    assert ft.sleeps == [1.0]


def test_fetcher_retries_5xx_and_honours_retry_after() -> None:
    replies = [httpx.Response(503), httpx.Response(429, headers={"Retry-After": "7"}), ok([1])]

    def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0)

    ft = FakeTime()
    assert fetcher(handler, ft).get("https://x.test/a", {}) == [1]
    assert 7.0 in ft.sleeps
    assert len(ft.sleeps) == 2


def test_fetcher_gives_up_after_three_attempts_without_leaking_the_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(LookupFailedError) as err:
        fetcher(handler, FakeTime()).get("https://x.test/a", {"q": "1"}, secret={"k": SECRET})
    assert "status 500" in str(err.value)
    assert SECRET not in str(err.value)


def test_fetcher_does_not_retry_other_4xx_or_cache_failures() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401)

    cache: dict[str, Any] = {}
    with pytest.raises(LookupFailedError):
        fetcher(handler, FakeTime(), cache).get("https://x.test/a", {})
    assert calls == [1]
    assert cache == {}


def test_fetcher_retries_network_errors() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectTimeout("timed out", request=request)
        return ok({"done": True})

    assert fetcher(handler, FakeTime()).get("https://x.test/a", {}) == {"done": True}
    assert len(attempts) == 3


# --- Comic Vine ---------------------------------------------------------------


def cv_reply(results: object, error: str = "OK", code: int = 1) -> httpx.Response:
    return ok({"error": error, "status_code": code, "results": results})


def comic_vine(routes: dict[str, httpx.Response], seen: list[httpx.Request]) -> ComicVine:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return routes[request.url.path]

    return ComicVine(fetcher(handler, FakeTime()), SECRET)


PANTHA = {
    "id": 49543,
    "name": "Pantha",
    "start_year": "2012",
    "publisher": {"name": "Dynamite Entertainment"},
    "count_of_issues": 6,
}
ODD = {"id": 7, "name": "Odd", "start_year": None, "publisher": None, "count_of_issues": 0}


def cv_page(results: list[object], total: int, offset: int) -> httpx.Response:
    body = {"error": "OK", "status_code": 1, "results": results}
    return ok({**body, "number_of_total_results": total, "offset": offset})


def test_comic_vine_volumes_filters_by_name_and_pages() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        offset = int(request.url.params["offset"])
        return cv_page([PANTHA] if offset == 0 else [ODD], total=150, offset=offset)

    cv = ComicVine(fetcher(handler, FakeTime()), SECRET)
    assert cv.volumes("Pantha") == [
        CvVolume(
            id=49543, name="Pantha", publisher="Dynamite Entertainment", start_year=2012, issues=6
        ),
        CvVolume(id=7, name="Odd", publisher=None, start_year=None, issues=0),
    ]
    assert [r.url.params["offset"] for r in seen] == ["0", "100"]
    params = seen[0].url.params
    assert seen[0].url.path == "/api/volumes/"
    assert (params["filter"], params["limit"], params["format"]) == ("name:Pantha", "100", "json")
    assert params["api_key"] == SECRET
    assert seen[0].headers["User-Agent"].startswith("humble-dl")


def test_comic_vine_volumes_stops_at_the_page_cap_and_cuts_names_at_commas() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return cv_page([ODD], total=10_000, offset=int(request.url.params["offset"]))

    ComicVine(fetcher(handler, FakeTime()), SECRET).volumes("Rick and Morty: New Year, New Rick")
    assert len(seen) == 3
    assert seen[0].url.params["filter"] == "name:Rick and Morty: New Year"


def test_comic_vine_issue_and_credits() -> None:
    seen: list[httpx.Request] = []
    credits = {
        "person_credits": [
            {"name": "Tom Sniegoski", "role": "writer"},
            {"name": "Igor Lima", "role": "artist"},
        ]
    }
    cv = comic_vine(
        {
            "/api/issues/": cv_reply([{"id": 906228, "name": "Volume Two"}]),
            "/api/issue/4000-906228/": cv_reply(credits),
        },
        seen,
    )
    assert cv.issue(49543, 2) == CvIssue(906228, "Volume Two")
    assert seen[0].url.params["filter"] == "volume:49543,issue_number:2"
    assert cv.credits(906228) == [
        CvCredit("Tom Sniegoski", "writer"),
        CvCredit("Igor Lima", "artist"),
    ]


def test_comic_vine_missing_issue_is_none() -> None:
    cv = comic_vine({"/api/issues/": cv_reply([])}, [])
    assert cv.issue(1, 99) is None


def test_comic_vine_api_error_is_reported_without_the_key() -> None:
    cv = comic_vine({"/api/volumes/": cv_reply([], error="Invalid API Key", code=100)}, [])
    with pytest.raises(LookupFailedError) as err:
        cv.volumes("Pantha")
    assert "Invalid API Key" in str(err.value)
    assert SECRET not in str(err.value)


# --- Open Library -------------------------------------------------------------


def test_open_library_search_parses_docs() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        docs = [
            {
                "title": "Rick and Morty Presents Vol. 5",
                "author_name": ["Ivan Cohen"],
                "publisher": ["Oni Press"],
            },
            {"title": "Bare"},
        ]
        return ok({"docs": docs})

    ol = OpenLibrary(fetcher(handler, FakeTime()))
    assert ol.search("Rick and Morty Presents Vol. 5") == [
        OlDoc("Rick and Morty Presents Vol. 5", ["Ivan Cohen"], ["Oni Press"]),
        OlDoc("Bare", [], []),
    ]
    assert str(seen[0].url).startswith(OL_SEARCH)
    assert seen[0].url.params["title"] == "Rick and Morty Presents Vol. 5"


def test_unexpected_response_shape_is_a_lookup_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok({"docs": "not a list"})

    with pytest.raises(LookupFailedError, match="unexpected _OlSearch shape"):
        OpenLibrary(fetcher(handler, FakeTime())).search("x")
