"""TAP parameter parsing tests."""

from __future__ import annotations

import pytest

from tapdrop.api.params import TapRequest, parse_tap_request
from tapdrop.config import Settings
from tapdrop.errors import InvalidParameterError, UnsupportedFormatError
from tapdrop.output import CSV, VOTABLE, VOTABLE_TD


def base(**overrides: str) -> dict[str, str]:
    params = {"REQUEST": "doQuery", "LANG": "ADQL", "QUERY": "SELECT 1"}
    params.update(overrides)
    return params


def test_minimal_request() -> None:
    request = parse_tap_request(base())

    assert request == TapRequest(query="SELECT 1", fmt=VOTABLE)


def test_parameter_names_are_case_insensitive() -> None:
    request = parse_tap_request(
        {"request": "doQuery", "Lang": "adql", "qUeRy": "SELECT 2", "ReSpOnSeFoRmAt": "csv"}
    )

    assert request.query == "SELECT 2"
    assert request.fmt == CSV


@pytest.mark.parametrize("lang", ["ADQL", "adql", "ADQL-2.0", "ADQL-2.1"])
def test_accepted_langs(lang: str) -> None:
    assert parse_tap_request(base(LANG=lang)).query == "SELECT 1"


def test_sql_lang_is_rejected() -> None:
    with pytest.raises(InvalidParameterError, match="LANG"):
        parse_tap_request(base(LANG="SQL"))


@pytest.mark.parametrize("missing", ["REQUEST", "LANG", "QUERY"])
def test_required_parameters(missing: str) -> None:
    params = base()
    del params[missing]

    with pytest.raises(InvalidParameterError, match=missing):
        parse_tap_request(params)


def test_request_must_be_doquery() -> None:
    with pytest.raises(InvalidParameterError, match="REQUEST"):
        parse_tap_request(base(REQUEST="getCapabilities"))


def test_blank_query_is_rejected() -> None:
    with pytest.raises(InvalidParameterError, match="QUERY"):
        parse_tap_request(base(QUERY="   "))


def test_responseformat_wins_over_format() -> None:
    request = parse_tap_request(base(FORMAT="csv", RESPONSEFORMAT="votable/td"))

    assert request.fmt == VOTABLE_TD


def test_legacy_format_parameter_is_honoured() -> None:
    assert parse_tap_request(base(FORMAT="csv")).fmt == CSV


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(UnsupportedFormatError):
        parse_tap_request(base(RESPONSEFORMAT="fits"))


def test_repeated_single_valued_parameter_is_rejected() -> None:
    with pytest.raises(InvalidParameterError, match="QUERY"):
        parse_tap_request(
            [("REQUEST", "doQuery"), ("LANG", "ADQL"), ("QUERY", "SELECT 1"), ("QUERY", "SELECT 2")]
        )


def test_maxrec_zero_is_preserved() -> None:
    request = parse_tap_request(base(MAXREC="0"))

    assert request.maxrec == 0
    assert request.effective_maxrec(Settings()) == 0


def test_maxrec_is_clamped_to_the_hard_ceiling() -> None:
    request = parse_tap_request(base(MAXREC="99999999999"))

    assert request.effective_maxrec(Settings(hard_max_rows=1000)) == 1000


def test_absent_maxrec_falls_back_to_the_default() -> None:
    assert parse_tap_request(base()).effective_maxrec(Settings(max_rows=42)) == 42


@pytest.mark.parametrize("value", ["-1", "many", "1.5"])
def test_bad_maxrec_is_rejected(value: str) -> None:
    with pytest.raises(InvalidParameterError, match="MAXREC"):
        parse_tap_request(base(MAXREC=value))


def test_upload_pairs_are_split() -> None:
    request = parse_tap_request(base(UPLOAD="mine,https://example.org/t.vot"))

    assert request.uploads == (("mine", "https://example.org/t.vot"),)


def test_several_uploads_in_one_value() -> None:
    request = parse_tap_request(base(UPLOAD="a,http://x/1.vot,b,http://x/2.vot"))

    assert request.uploads == (("a", "http://x/1.vot"), ("b", "http://x/2.vot"))


def test_repeated_upload_parameters_accumulate() -> None:
    request = parse_tap_request(
        [
            ("REQUEST", "doQuery"),
            ("LANG", "ADQL"),
            ("QUERY", "SELECT 1"),
            ("UPLOAD", "a,param:a"),
            ("UPLOAD", "b,param:b"),
        ]
    )

    assert request.uploads == (("a", "param:a"), ("b", "param:b"))


def test_malformed_upload_is_rejected() -> None:
    with pytest.raises(InvalidParameterError, match="UPLOAD"):
        parse_tap_request(base(UPLOAD="justaname"))


def test_unknown_parameters_are_ignored() -> None:
    request = parse_tap_request(base(SOMETHING="else", RUNID="abc"))

    assert request.runid == "abc"
