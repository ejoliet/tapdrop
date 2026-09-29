"""Serialisation tests.

Every assertion here is about the wire format a VO client sees, so results are
read back with astropy/pyarrow rather than compared as strings.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import pyarrow as pa
import pytest
from astropy.io.votable import parse as parse_votable

from tapdrop.errors import UnsupportedFormatError
from tapdrop.output import (
    CSV,
    JSON,
    PARQUET,
    TSV,
    VOTABLE,
    VOTABLE_TD,
    content_type,
    normalize_format,
    serialize,
    votable_error,
)


@pytest.fixture
def result() -> pa.Table:
    return pa.table(
        {
            "source_id": pa.array([1, 2, None], type=pa.int64()),
            "ra": pa.array([10.5, None, 359.999], type=pa.float64()),
            "dec": pa.array([-45.25, 0.0, 89.9], type=pa.float64()),
            "name": pa.array(["alpha", "beta", None], type=pa.string()),
            "is_star": pa.array([True, None, False], type=pa.bool_()),
            "obs_time": pa.array(
                [datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), None, None],
                type=pa.timestamp("us", tz="UTC"),
            ),
        }
    )


class FakeColumnMeta:
    """Stands in for registry.ColumnMeta: serialisation only reads these five."""

    def __init__(
        self,
        unit: str | None = None,
        ucd: str | None = None,
        description: str = "",
        utype: str | None = None,
        xtype: str | None = None,
    ) -> None:
        self.unit = unit
        self.ucd = ucd
        self.description = description
        self.utype = utype
        self.xtype = xtype


@pytest.fixture
def columns() -> dict[str, FakeColumnMeta]:
    return {
        "ra": FakeColumnMeta(unit="deg", ucd="pos.eq.ra;meta.main", description="Right ascension"),
        "dec": FakeColumnMeta(unit="deg", ucd="pos.eq.dec;meta.main", description="Declination"),
    }


def read_votable(payload: bytes):
    return parse_votable(io.BytesIO(payload))


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        (None, VOTABLE),
        ("", VOTABLE),
        ("votable", VOTABLE),
        ("VOTable", VOTABLE),
        ("application/x-votable+xml", VOTABLE),
        ("application/x-votable+xml; serialization=BINARY2", VOTABLE),
        ("votable/td", VOTABLE_TD),
        ("application/x-votable+xml;serialization=TABLEDATA", VOTABLE_TD),
        ("csv", CSV),
        ("text/csv", CSV),
        ("tsv", TSV),
        ("parquet", PARQUET),
        ("json", JSON),
    ],
)
def test_format_aliases_resolve(requested: str | None, expected: str) -> None:
    assert normalize_format(requested) == expected


def test_unknown_format_is_a_400_that_lists_what_works() -> None:
    with pytest.raises(UnsupportedFormatError) as excinfo:
        normalize_format("fits")
    assert excinfo.value.http_status == 400
    assert "votable" in excinfo.value.message


def test_content_type_per_format() -> None:
    assert content_type(VOTABLE) == "application/x-votable+xml"
    assert content_type(CSV) == "text/csv;header=present"
    assert content_type(PARQUET) == "application/vnd.apache.parquet"


def test_votable_defaults_to_binary2(result: pa.Table) -> None:
    payload = serialize(result, VOTABLE)
    assert b"<BINARY2>" in payload
    assert b"<TABLEDATA>" not in payload


def test_votable_td_uses_tabledata(result: pa.Table) -> None:
    payload = serialize(result, VOTABLE_TD)
    assert b"<TABLEDATA>" in payload
    assert b"<BINARY2>" not in payload


@pytest.mark.parametrize("fmt", [VOTABLE, VOTABLE_TD])
def test_votable_round_trips_values_and_nulls(result: pa.Table, fmt: str) -> None:
    table = read_votable(serialize(result, fmt)).get_first_table().to_table(use_names_over_ids=True)

    assert list(table.colnames) == result.column_names
    assert table["source_id"][0] == 1
    assert table["source_id"].mask[2]
    assert table["ra"][2] == pytest.approx(359.999)
    assert table["ra"].mask[1]
    assert table["is_star"][0] is True or bool(table["is_star"][0])
    assert table["is_star"].mask[1]
    assert str(table["name"][0]) == "alpha"


def test_votable_keeps_integer_and_boolean_datatypes(result: pa.Table) -> None:
    fields = {f.name: f for f in read_votable(serialize(result, VOTABLE)).get_first_table().fields}

    assert fields["source_id"].datatype == "long"  # not widened to double by the null
    assert fields["is_star"].datatype == "boolean"  # not "bit"
    assert fields["name"].datatype == "char"
    assert fields["name"].arraysize == "*"
    assert fields["obs_time"].xtype == "timestamp"


def test_votable_carries_unit_ucd_and_description(
    result: pa.Table, columns: dict[str, FakeColumnMeta]
) -> None:
    fields = {
        f.name: f
        for f in read_votable(serialize(result, VOTABLE, columns)).get_first_table().fields
    }

    assert fields["ra"].unit == "deg"
    assert fields["ra"].ucd == "pos.eq.ra;meta.main"
    assert fields["ra"].description == "Right ascension"
    assert fields["source_id"].unit is None  # no metadata, no invention


def test_votable_carries_utype_and_xtype(result: pa.Table) -> None:
    """ObsCore declares both; a client matching on utype needs them on the FIELD."""
    columns = {
        "ra": FakeColumnMeta(
            utype="obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C1",
            xtype="adql:REGION",
        )
    }
    fields = {
        f.name: f
        for f in read_votable(serialize(result, VOTABLE, columns)).get_first_table().fields
    }

    assert fields["ra"].utype == (
        "obscore:Char.SpatialAxis.Coverage.Location.Coord.Position2D.Value2.C1"
    )
    assert fields["ra"].xtype == "adql:REGION"
    assert fields["source_id"].utype is None


def test_votable_declares_query_status_ok_first(result: pa.Table) -> None:
    payload = serialize(result, VOTABLE)
    resource = read_votable(payload).resources[0]

    assert resource.type == "results"
    assert resource.infos[0].name == "QUERY_STATUS"
    assert resource.infos[0].value == "OK"
    assert b"OVERFLOW" not in payload


def test_overflow_info_follows_the_table(result: pa.Table) -> None:
    payload = serialize(result, VOTABLE, overflow=True)

    assert payload.index(b'value="OVERFLOW"') > payload.index(b"</TABLE>")
    statuses = [
        i.value for i in read_votable(payload).resources[0].infos if i.name == "QUERY_STATUS"
    ]
    assert statuses == ["OK", "OVERFLOW"]


def test_empty_result_still_serialises(result: pa.Table) -> None:
    """MAXREC=0 means metadata only, so a zero-row VOTable must be valid."""
    table = read_votable(serialize(result.slice(0, 0), VOTABLE)).get_first_table()

    assert len(table.to_table()) == 0
    assert [f.name for f in table.fields] == result.column_names


def test_error_document_is_a_results_resource() -> None:
    payload = votable_error("Unknown table 'nope'. Did you mean: gaia?")
    resource = read_votable(payload).resources[0]

    assert resource.type == "results"
    assert resource.infos[0].name == "QUERY_STATUS"
    assert resource.infos[0].value == "ERROR"
    assert "Unknown table" in resource.infos[0].content


def test_error_document_escapes_the_message() -> None:
    payload = votable_error("bad <query> & 'quotes'")

    assert b"<query>" not in payload
    assert "bad <query> & 'quotes'" in read_votable(payload).resources[0].infos[0].content


def test_csv_has_a_header_and_empty_nulls(result: pa.Table) -> None:
    lines = serialize(result, CSV).decode().splitlines()

    assert lines[0].split(",")[:3] == ["source_id", "ra", "dec"]
    assert len(lines) == 4
    assert lines[2].split(",")[1] == ""  # null ra
    assert "2026-01-02T03:04:05" in lines[1]


def test_tsv_uses_tabs(result: pa.Table) -> None:
    lines = serialize(result, TSV).decode().splitlines()

    assert lines[0].split("\t")[:2] == ["source_id", "ra"]
    assert "," not in lines[0]


def test_parquet_round_trips(result: pa.Table) -> None:
    from pyarrow import parquet as pa_parquet

    back = pa_parquet.read_table(io.BytesIO(serialize(result, PARQUET)))

    assert back.equals(result)


def test_json_carries_metadata_and_rows(
    result: pa.Table, columns: dict[str, FakeColumnMeta]
) -> None:
    payload = json.loads(serialize(result, JSON, columns))

    by_name = {entry["name"]: entry for entry in payload["metadata"]}
    assert by_name["ra"]["unit"] == "deg"
    assert by_name["ra"]["ucd"] == "pos.eq.ra;meta.main"
    assert by_name["source_id"]["datatype"] == "long"
    assert by_name["is_star"]["datatype"] == "boolean"
    assert len(payload["data"]) == 3
    assert payload["data"][0][0] == 1
    assert payload["data"][2][0] is None
    assert payload["data"][0][5].startswith("2026-01-02T03:04:05")


def test_json_renders_nan_as_null() -> None:
    table = pa.table({"x": pa.array([float("nan"), 1.0], type=pa.float64())})

    payload = json.loads(serialize(table, JSON))

    assert payload["data"] == [[None], [1.0]]
