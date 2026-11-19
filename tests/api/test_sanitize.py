"""Tests for humble_dl.api.sanitize (pure sanitizer, identity redaction, flight rows)."""

import json
from typing import Any

import pytest

from humble_dl.api.rsc import KIND_RAW, KIND_TEXT, FlightRow, parse_flight, render_flight
from humble_dl.api.sanitize import (
    EMAIL_PLACEHOLDER,
    REDACTED,
    CaptureLeakError,
    assert_clean,
    flight_gamekeys,
    identity_pattern,
    identity_terms,
    list_gamekeys,
    redact_text,
    redact_value,
    sanitize,
    sanitize_flight,
    text_gamekeys,
)

KEY_A = "AbC123xyZ789"
EMAIL = "reader@example.com"
DL_URL = "https://dl.humble.com/f.pdf?ttl=1700000000&t=SIGNEDVALUE"


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


def test_list_gamekeys_handles_strings_dicts_and_junk() -> None:
    body = ["StrKey1", {"gamekey": "DictKey2"}, {"other": 1}, 5, "", {"gamekey": 7}]

    assert list_gamekeys(body) == ["StrKey1", "DictKey2"]
    assert list_gamekeys({"gamekey": "x"}) == []


def test_sanitize_prefix_key_cannot_partially_replace_longer_key() -> None:
    gamekey_map = {"abc": "GAMEKEY_0001", "ABCdef": "GAMEKEY_0002"}

    assert sanitize("abcDEF abc", gamekey_map) == "GAMEKEY_0002 GAMEKEY_0001"


def test_sanitize_escapes_regex_metacharacters_in_keys() -> None:
    assert sanitize("a+b a.b", {"a+b": "GAMEKEY_0001"}) == "GAMEKEY_0001 a.b"


# --- text gamekeys / identity ----------------------------------------------------


def test_text_gamekeys_finds_json_pairs_paths_and_queries_but_not_claim() -> None:
    text = (
        'x "gamekey" : "RegexKey123" y \\"gamekey\\":\\"Escaped4567\\" '
        "/gift/GiftKey12345 /gift/claim/ClaimTok1234 /downloads?key=QueryKey123&a=1 "
        "?gamekey=Other%2DKey99 ?key=short /gift/abc"
    )

    assert text_gamekeys(text) == [
        "RegexKey123",
        "Escaped4567",
        "GiftKey12345",
        "QueryKey123",
        "Other-Key99",
    ]


def test_identity_terms_filters_short_values_and_adds_url_forms() -> None:
    assert identity_terms([" Jane Doe ", "ab", "", "Jane Doe"]) == [
        "Jane Doe",
        "Jane%20Doe",
        "Jane+Doe",
    ]


def test_identity_redaction_is_case_insensitive_in_keys_and_values() -> None:
    pattern = identity_pattern(identity_terms(["Jane Doe"]))

    assert redact_text("by JANE doe and jane%20doe", pattern) == f"by {REDACTED} and {REDACTED}"
    assert redact_value({"Jane Doe": ["hi jane DOE", 3, None]}, pattern) == {
        REDACTED: [f"hi {REDACTED}", 3, None]
    }


def test_identity_redaction_without_terms_is_identity() -> None:
    assert identity_pattern([]) is None
    assert redact_text("Jane", None) == "Jane"
    assert redact_value({"a": "Jane"}, None) == {"a": "Jane"}


# --- flight --------------------------------------------------------------------


def test_sanitize_flight_json_rows_drop_secret_keys_and_strip_url_queries() -> None:
    rows = [
        FlightRow("0", "json", {"email": EMAIL, "u": {"url": DL_URL}, "n": "keep"}),
    ]

    out = sanitize_flight(render_flight(rows), {}, None)

    assert parse_flight(out) == [
        FlightRow("0", "json", {"u": {"url": "https://dl.humble.com/f.pdf"}, "n": "keep"})
    ]
    for leaked in (EMAIL, "SIGNEDVALUE", "ttl="):
        assert leaked not in out


def test_sanitize_flight_remaps_regex_found_gamekey_in_raw_row_and_text_row() -> None:
    gamekey_map = {"RegexKey123": "GAMEKEY_0001"}
    text = 'a:junk {"gamekey":"RegexKey123"} see /gift/regexkey123\nb:T1a,{"q":"RegexKey123 x"}\n'

    out = sanitize_flight(text, gamekey_map, None)

    assert "RegexKey123".lower() not in out.lower()
    assert out.splitlines()[0] == 'a:junk {"gamekey":"GAMEKEY_0001"} see /gift/GAMEKEY_0001'
    assert parse_flight(out)[1] == FlightRow("b", KIND_TEXT, '{"q":"GAMEKEY_0001 x"}')


def test_sanitize_flight_text_row_json_drops_secret_keys() -> None:
    out = sanitize_flight('a:T14,{"token":"x","ok":1}', {}, None)

    assert parse_flight(out) == [FlightRow("a", KIND_TEXT, '{"ok":1}')]


def test_sanitize_flight_redacts_identity_in_raw_text_and_json() -> None:
    pattern = identity_pattern(identity_terms(["Jane Doe"]))
    text = '1:{"greeting":"Hello jane DOE"}\n2:I raw JANE DOE row\n3:T9,Jane Doe!\n'

    out = sanitize_flight(text, {}, pattern)

    assert parse_flight(out) == [
        FlightRow("1", "json", {"greeting": f"Hello {REDACTED}"}),
        FlightRow("2", KIND_RAW, f"I raw {REDACTED} row"),
        FlightRow("3", KIND_TEXT, f"{REDACTED}!"),
    ]
    assert "jane" not in out.lower()


def test_sanitize_flight_replaces_emails_in_raw_rows() -> None:
    out = sanitize_flight(f"1:I mail {EMAIL} here\n", {}, None)

    assert out == f"1:I mail {EMAIL_PLACEHOLDER} here\n"


def test_flight_gamekeys_uses_rows_regex_and_url_patterns() -> None:
    text = (
        '0:{"orders":[{"gamekey":"RowKey12345"}]}\n'
        '1:T20,{"x":"/downloads?key=TextRowKey1"}\n'
        '2:raw "gamekey":"RawKey12345" /gift/GiftKey12345\n'
    )

    keys = flight_gamekeys(text)

    assert set(keys) == {"RowKey12345", "TextRowKey1", "RawKey12345", "GiftKey12345"}


def test_flight_payload_is_json_serializable_after_sanitize() -> None:
    payload: Any = {"a": [1, {"gamekey": "K"}]}

    assert json.loads(json.dumps(sanitize(payload, {}))) == {"a": [1, {"gamekey": "GAMEKEY_0001"}]}
