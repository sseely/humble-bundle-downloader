"""Tests for humble_dl.lib.logging."""

import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from humble_dl.lib.logging import (
    FILE_NAME_KEY,
    LOG_LEVEL_ENV,
    SERVICE_NAME,
    JsonFormatter,
    get_logger,
    redact_gamekey,
    redact_url,
)

FIXED_NOW = datetime(2026, 11, 18, 12, 30, 45, tzinfo=UTC)
FIXED_TS = "2026-11-18T12:30:45+00:00"


def _fixed_clock() -> datetime:
    return FIXED_NOW


@pytest.fixture
def logger_name() -> Iterator[str]:
    name = f"humble_dl.test.{uuid.uuid4().hex}"
    yield name
    logger = logging.getLogger(name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)


def _lines(captured: str) -> list[str]:
    return [line for line in captured.splitlines() if line]


def test_redact_url_strips_query_and_fragment() -> None:
    url = "https://dl.humble.com/x/book.pdf?gamekey=ABC&ttl=1&t=tok#frag"
    assert redact_url(url) == "https://dl.humble.com/x/book.pdf"


def test_redact_url_keeps_port_and_drops_userinfo() -> None:
    assert redact_url("http://user:pw@host.example:8080/a?q=1") == "http://host.example:8080/a"


def test_redact_url_without_query_is_unchanged() -> None:
    assert redact_url("https://dl.humble.com/a/b.epub") == "https://dl.humble.com/a/b.epub"


def test_redact_gamekey_keeps_first_four_chars() -> None:
    assert redact_gamekey("abcdefgh") == "abcd…"


def test_redact_gamekey_short_key_never_fully_revealed() -> None:
    assert redact_gamekey("abc") == "a…"
    assert redact_gamekey("") == "…"


def test_logger_emits_single_json_line(
    logger_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    logger = get_logger(logger_name, clock=_fixed_clock)
    logger.info("fetched", extra={"bundle": "B"})
    lines = _lines(capsys.readouterr().err)
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "level": "info",
        "msg": "fetched",
        "ts": FIXED_TS,
        "service": SERVICE_NAME,
        "bundle": "B",
    }


def test_logger_does_not_write_to_stdout(
    logger_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    get_logger(logger_name, clock=_fixed_clock).info("hello")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["msg"] == "hello"


@pytest.mark.parametrize(
    ("method", "expected"),
    [("debug", "debug"), ("info", "info"), ("warning", "warn"), ("error", "error")],
)
def test_level_names_are_mapped(
    logger_name: str,
    capsys: pytest.CaptureFixture[str],
    method: str,
    expected: str,
) -> None:
    logger = get_logger(logger_name, level="debug", clock=_fixed_clock)
    getattr(logger, method)("m")
    assert json.loads(capsys.readouterr().err)["level"] == expected


def test_critical_maps_to_error(logger_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    get_logger(logger_name, clock=_fixed_clock).critical("boom")
    assert json.loads(capsys.readouterr().err)["level"] == "error"


def test_get_logger_is_idempotent(logger_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    first = get_logger(logger_name, clock=_fixed_clock)
    second = get_logger(logger_name, clock=_fixed_clock)
    second.info("once")
    assert first is second
    assert len(first.handlers) == 1
    assert len(_lines(capsys.readouterr().err)) == 1


def test_default_level_is_info(
    logger_name: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(LOG_LEVEL_ENV, raising=False)
    logger = get_logger(logger_name, clock=_fixed_clock)
    logger.debug("hidden")
    logger.info("shown")
    lines = _lines(capsys.readouterr().err)
    assert [json.loads(line)["msg"] for line in lines] == ["shown"]


def test_env_level_is_respected(
    logger_name: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "ERROR")
    logger = get_logger(logger_name, clock=_fixed_clock)
    logger.warning("hidden")
    logger.error("shown")
    lines = _lines(capsys.readouterr().err)
    assert [json.loads(line)["msg"] for line in lines] == ["shown"]


def test_explicit_level_overrides_env(
    logger_name: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "error")
    logger = get_logger(logger_name, level="debug", clock=_fixed_clock)
    logger.debug("shown")
    assert json.loads(capsys.readouterr().err)["msg"] == "shown"


def test_invalid_env_level_falls_back_to_info(
    logger_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(LOG_LEVEL_ENV, "nonsense")
    assert get_logger(logger_name).level == logging.INFO


def test_logger_does_not_propagate(logger_name: str) -> None:
    assert get_logger(logger_name).propagate is False


def test_extra_fields_exclude_standard_record_attributes(
    logger_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    get_logger(logger_name, clock=_fixed_clock).info(
        "e", extra={"item": "I", FILE_NAME_KEY: "a.pdf", "event": "skip"}
    )
    payload = json.loads(capsys.readouterr().err)
    assert set(payload) == {"level", "msg", "ts", "service", "item", "event"} | {"filename"}
    assert payload["item"] == "I"


def test_msg_is_formatted_with_args(logger_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    get_logger(logger_name, clock=_fixed_clock).info("got %d of %s", 3, "x")
    assert json.loads(capsys.readouterr().err)["msg"] == "got 3 of x"


def test_non_serializable_extra_is_stringified(
    logger_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    get_logger(logger_name, clock=_fixed_clock).info("m", extra={"when": FIXED_NOW})
    assert json.loads(capsys.readouterr().err)["when"] == str(FIXED_NOW)


def test_exception_info_is_included(logger_name: str, capsys: pytest.CaptureFixture[str]) -> None:
    logger = get_logger(logger_name, clock=_fixed_clock)
    try:
        raise ValueError("bad")
    except ValueError:
        logger.error("failed", exc_info=True)
    payload = json.loads(capsys.readouterr().err)
    assert payload["exc_info"].startswith("Traceback")
    assert payload["exc_info"].endswith("ValueError: bad")


def test_formatter_default_clock_emits_utc_iso() -> None:
    record = logging.LogRecord("n", logging.INFO, "p", 1, "m", (), None)
    ts = json.loads(JsonFormatter().format(record))["ts"]
    assert datetime.fromisoformat(ts).utcoffset() == FIXED_NOW.utcoffset()


def test_file_name_extra_is_emitted_as_filename(
    logger_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    get_logger(logger_name, clock=_fixed_clock).info("m", extra={FILE_NAME_KEY: "book.pdf"})
    payload = json.loads(capsys.readouterr().err)
    assert payload["filename"] == "book.pdf"
    assert "file_name" not in payload


def test_log_record_source_filename_never_emitted(
    logger_name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    get_logger(logger_name, clock=_fixed_clock).info("m")
    assert "filename" not in json.loads(capsys.readouterr().err)
