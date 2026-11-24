"""Writer lookups: Comic Vine (needs an API key) and Open Library (no key).

Every request goes through ``JsonFetcher``: one request per ``min_interval_s``,
retries on 5xx/429/network errors, and a JSON cache keyed by URL and non-secret
parameters, so re-runs make no requests and the API key is never stored.
"""

import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, Field, ValidationError

from humble_dl.lib.logging import get_logger
from humble_dl.services.book_info import CvCredit, CvIssue, CvVolume, OlDoc

CV_BASE = "https://comicvine.gamespot.com/api"
OL_SEARCH = "https://openlibrary.org/search.json"
USER_AGENT = "humble-dl-book-lookup/0.1"
CV_OK = 1  # Comic Vine status_code for success
CV_PAGE_SIZE = 100  # Comic Vine's maximum for list resources
CV_MAX_PAGES = 3
CV_FILTER_SEPARATOR = ","  # separates filters, so a name is cut before one
OL_SEARCH_LIMIT = "5"
CV_VOLUME_FIELDS = "id,name,publisher,start_year,count_of_issues"
OL_FIELDS = "title,author_name,publisher"
DEFAULT_TIMEOUT_S = 20.0
DEFAULT_INTERVAL_S = 1.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
DEFAULT_RETRY_AFTER_S = 1.0
HTTP_TOO_MANY = 429
HTTP_SERVER_ERROR = 500

Clock = Callable[[], float]
Sleep = Callable[[float], None]

_log = get_logger(__name__)


class LookupFailedError(Exception):
    """A lookup request failed for good; the message names the URL, never the key."""


@dataclass(frozen=True)
class Pacing:
    """Minimum gap between requests, and the clock/sleep used to keep it."""

    min_interval_s: float = DEFAULT_INTERVAL_S
    clock: Clock = time.monotonic
    sleep: Sleep = time.sleep


class JsonFetcher:
    """Cached, throttled JSON GETs.

    Concurrency: single-threaded use only; the cache and throttle are unguarded.
    """

    def __init__(
        self, client: httpx.Client, cache: MutableMapping[str, Any], pacing: Pacing = Pacing()
    ) -> None:
        self._client = client
        self._cache = cache
        self._interval = pacing.min_interval_s
        self._clock = pacing.clock
        self._sleep = pacing.sleep
        self._last: float | None = None

    def get(
        self, url: str, params: Mapping[str, str], secret: Mapping[str, str] | None = None
    ) -> Any:
        """GET ``url`` with ``params`` (cached) plus ``secret`` params (never cached)."""
        key = f"{url}?{urlencode(sorted(params.items()))}"
        if key not in self._cache:
            self._cache[key] = self._fetch(url, {**params, **(secret or {})}, key)
        return self._cache[key]

    def _wait(self) -> None:
        if self._last is not None:
            delay = self._interval - (self._clock() - self._last)
            if delay > 0:
                self._sleep(delay)
        self._last = self._clock()

    def _attempt(self, url: str, params: Mapping[str, str]) -> httpx.Response | None:
        """The response, or None after a network error."""
        self._wait()
        try:
            return self._client.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT_S
            )
        except httpx.TransportError:
            return None

    def _fetch(self, url: str, params: Mapping[str, str], key: str) -> Any:
        problem = "no response"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self._attempt(url, params)
            if response is not None and response.is_success:
                return response.json()
            if response is not None:
                problem = f"status {response.status_code}"
                if not _retryable(response.status_code):
                    break
            _log.warning("lookup_retry", extra={"url": key, "attempt": attempt, "problem": problem})
            if attempt < MAX_ATTEMPTS:
                self._sleep(_retry_delay(response, attempt))
        raise LookupFailedError(f"GET {key} failed: {problem}")


def _retryable(status: int) -> bool:
    return status == HTTP_TOO_MANY or status >= HTTP_SERVER_ERROR


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None and response.status_code == HTTP_TOO_MANY:
        header = response.headers.get("Retry-After", "")
        return float(header) if header.isdigit() else DEFAULT_RETRY_AFTER_S
    return BACKOFF_BASE_S * 2 ** (attempt - 1)


# --- Comic Vine ---------------------------------------------------------------


class _CvPublisher(BaseModel):
    name: str


class _CvVolume(BaseModel):
    id: int
    name: str
    publisher: _CvPublisher | None = None
    start_year: str | None = None
    count_of_issues: int = 0


class _CvIssue(BaseModel):
    id: int
    name: str | None = None


class _CvCredit(BaseModel):
    name: str
    role: str = ""


class _CvIssueDetail(BaseModel):
    person_credits: list[_CvCredit] = Field(default_factory=lambda: list[_CvCredit]())


def _year(text: str | None) -> int | None:
    return int(text) if text and text.isdigit() else None


class ComicVine:
    """Comic Vine volumes, issues and credits."""

    def __init__(self, fetcher: JsonFetcher, api_key: str) -> None:
        self._fetcher = fetcher
        self._secret = {"api_key": api_key}

    def _body(self, path: str, params: Mapping[str, str]) -> Any:
        url = f"{CV_BASE}/{path}/"
        body = self._fetcher.get(url, {**params, "format": "json"}, secret=self._secret)
        if body.get("status_code") != CV_OK:
            raise LookupFailedError(f"Comic Vine {path}: {body.get('error')}")
        return body

    def _results(self, path: str, params: Mapping[str, str]) -> Any:
        return self._body(path, params)["results"]

    def volumes(self, series: str) -> list[CvVolume]:
        """Volumes whose name contains ``series`` (up to ``CV_MAX_PAGES`` pages)."""
        name = series.split(CV_FILTER_SEPARATOR)[0]
        params = {"filter": f"name:{name}", "limit": str(CV_PAGE_SIZE)}
        found: list[CvVolume] = []
        for page in range(CV_MAX_PAGES):
            offset = page * CV_PAGE_SIZE
            body = self._body(
                "volumes", {**params, "offset": str(offset), "field_list": CV_VOLUME_FIELDS}
            )
            found.extend(_to_volume(_parse(_CvVolume, item)) for item in body["results"])
            if offset + CV_PAGE_SIZE >= body.get("number_of_total_results", 0):
                break
        return found

    def issue(self, volume_id: int, number: int) -> CvIssue | None:
        """Issue ``number`` of a volume, if Comic Vine lists it."""
        params = {"filter": f"volume:{volume_id},issue_number:{number}", "field_list": "id,name"}
        found = self._results("issues", params)
        if not found:
            return None
        raw = _parse(_CvIssue, found[0])
        return CvIssue(raw.id, raw.name)

    def credits(self, issue_id: int) -> list[CvCredit]:
        """People credited on an issue, with their roles."""
        raw = self._results(f"issue/4000-{issue_id}", {"field_list": "person_credits"})
        detail = _parse(_CvIssueDetail, raw)
        return [CvCredit(c.name, c.role) for c in detail.person_credits]


def _to_volume(raw: _CvVolume) -> CvVolume:
    publisher = raw.publisher.name if raw.publisher else None
    return CvVolume(
        id=raw.id,
        name=raw.name,
        publisher=publisher,
        start_year=_year(raw.start_year),
        issues=raw.count_of_issues,
    )


def _parse[T: BaseModel](model: type[T], data: object) -> T:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise LookupFailedError(
            f"unexpected {model.__name__} shape: {exc.error_count()} errors"
        ) from exc


# --- Open Library -------------------------------------------------------------


class _OlDoc(BaseModel):
    title: str
    author_name: list[str] = Field(default_factory=lambda: list[str]())
    publisher: list[str] = Field(default_factory=lambda: list[str]())


class _OlSearch(BaseModel):
    docs: list[_OlDoc] = Field(default_factory=lambda: list[_OlDoc]())


class OpenLibrary:
    """Open Library title search."""

    def __init__(self, fetcher: JsonFetcher) -> None:
        self._fetcher = fetcher

    def search(self, title: str) -> list[OlDoc]:
        """Books whose title matches ``title``."""
        params = {"title": title, "limit": OL_SEARCH_LIMIT, "fields": OL_FIELDS}
        found = _parse(_OlSearch, self._fetcher.get(OL_SEARCH, params))
        return [OlDoc(d.title, d.author_name, d.publisher) for d in found.docs]
