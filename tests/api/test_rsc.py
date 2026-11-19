"""Tests for humble_dl.api.rsc (pure flight parsing and bundle selection)."""

import json

import pytest

from humble_dl.api.rsc import (
    KIND_FLIGHT,
    KIND_JSON,
    KIND_RAW,
    KIND_RSC,
    KIND_TEXT,
    FlightRow,
    classify_response,
    extract_flight_from_html,
    is_humble_host,
    library_detail_path,
    parse_flight,
    render_flight,
    select_bundle_id,
)

LIBRARY_URL = "https://www.humblebundle.com/home/library"
BUNDLE_ID = "c7a551c0-0000-4000-8000-00000000abcd"


def push(kind: int, text: str) -> str:
    return f"<script>self.__next_f.push([{kind},{json.dumps(text)}])</script>"


def test_extract_flight_decodes_and_concatenates_pushes_in_order() -> None:
    parts = ['0:["$","div",null,{"t":"a \\"q\\" & b"}]\n', "1:T5,he\nllo", '2:"x"\n']
    html = (
        "<html><body>"
        + push(1, parts[0])
        + "<script>self.__next_f.push([0])</script>"
        + push(1, parts[1])
        + '<script>self.__next_f.push([2,"skipped"])</script>'
        + push(1, parts[2])
        + "</body></html>"
    )

    assert extract_flight_from_html(html) == "".join(parts)


def test_extract_flight_survives_closing_tokens_inside_strings_and_junk() -> None:
    html = 'self.__next_f.push( [1,"a])b"] ) self.__next_f.push(nonsense) self.__next_f.push([1,5])'

    assert extract_flight_from_html(html) == "a])b"


def test_extract_flight_without_pushes_is_empty() -> None:
    assert extract_flight_from_html("<html></html>") == ""


def test_parse_flight_json_raw_and_text_rows() -> None:
    text = '0:{"a":1}\n1:HL["/x.css","style"]\n2:T7,he\nl\nlo3:"tail"\nnot a row\n\n'

    rows = parse_flight(text)

    assert rows == [
        FlightRow("0", KIND_JSON, {"a": 1}),
        FlightRow("1", KIND_RAW, 'HL["/x.css","style"]'),
        FlightRow("2", KIND_TEXT, "he\nl\nlo"),
        FlightRow("3", KIND_JSON, "tail"),
        FlightRow("", KIND_RAW, "not a row"),
    ]


def test_parse_flight_text_row_counts_utf8_bytes_and_literals_are_json() -> None:
    rows = parse_flight("a:Td,héllo wörld\nb:null\nc:12\nd:[1,\n")

    assert rows == [
        FlightRow("a", KIND_TEXT, "héllo wörld"),
        FlightRow("b", KIND_JSON, None),
        FlightRow("c", KIND_JSON, 12),
        FlightRow("d", KIND_RAW, "[1,"),
    ]


def test_parse_flight_truncated_text_row_never_raises() -> None:
    assert parse_flight("f:Tff,short") == [FlightRow("f", KIND_TEXT, "short")]
    assert parse_flight("") == []


def test_render_flight_roundtrips_and_recomputes_text_length() -> None:
    rows = [
        FlightRow("0", KIND_JSON, {"k": "é"}),
        FlightRow("1", KIND_TEXT, "héllo"),
        FlightRow("2", KIND_RAW, "I[1,2]"),
        FlightRow("", KIND_RAW, "loose"),
    ]

    text = render_flight(rows)

    assert text == '0:{"k":"é"}\n1:T6,héllo\n2:I[1,2]\nloose\n'
    assert parse_flight(text) == rows


@pytest.mark.parametrize(
    ("url", "ctype", "expected"),
    [
        (LIBRARY_URL, "text/html; charset=utf-8", KIND_FLIGHT),
        (LIBRARY_URL + "/", "TEXT/HTML", KIND_FLIGHT),
        (f"{LIBRARY_URL}/{BUNDLE_ID}", "text/html", KIND_FLIGHT),
        ("https://www.humblebundle.com/home/libraryx", "text/html", None),
        ("https://www.humblebundle.com/other", "text/html", None),
        ("https://www.humblebundle.com/x", "application/json", KIND_JSON),
        ("https://www.humblebundle.com/x?_rsc=1", "text/x-component", KIND_RSC),
        ("https://www.humblebundle.com/x.css", "text/css", None),
        ("https://www.humblebundle.com/x", "", None),
    ],
)
def test_classify_response(url: str, ctype: str, expected: str | None) -> None:
    assert classify_response(url, ctype) == expected


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


def test_select_bundle_id_reads_escaped_inline_flight_text() -> None:
    script = push(1, f'11:{{"productEntitlementId":"{BUNDLE_ID}","title":"T"}}\n')
    texts = [
        "",
        "var x = 1;",
        script,
        'productEntitlementId":"c7a551c0-1111-4000-8000-000000000000"',
    ]

    assert select_bundle_id(texts) == BUNDLE_ID


def test_select_bundle_id_accepts_unescaped_text_and_none_when_absent() -> None:
    raw = f'{{"productEntitlementId":"{BUNDLE_ID.upper()}"}}'

    assert select_bundle_id([raw]) == BUNDLE_ID.upper()
    assert select_bundle_id(['"productEntitlementId":"not-a-uuid"']) is None
    assert select_bundle_id([]) is None


def test_library_detail_path() -> None:
    assert library_detail_path(BUNDLE_ID) == f"/home/library/{BUNDLE_ID}"
