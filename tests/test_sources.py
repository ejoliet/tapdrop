"""Source resolution: folder / glob / s3:// / https:// -> file groups (RDD.md rule 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tapdrop.sources import SourceGroup, resolve_sources, sanitize_identifier

DATA_DIR = Path(__file__).parent / "data"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Gaia DR3", "gaia_dr3"),
        ("123start", "t_123start"),
        ("already_ok", "already_ok"),
        ("", "x"),
        ("__--__", "x"),
        ("Cats/", "cats"),
    ],
)
def test_sanitize_identifier(name: str, expected: str) -> None:
    assert sanitize_identifier(name) == expected


def test_folder_scan_groups_one_file_per_table() -> None:
    groups, skipped = resolve_sources([str(DATA_DIR)])
    by_name = {g.table_name: g for g in groups}

    assert "gaia" in by_name
    assert by_name["gaia"].schema_name == "data"
    assert by_name["gaia"].files == (str((DATA_DIR / "gaia.parquet").resolve()),)
    assert by_name["gaia"].is_hats is False

    # tapdrop.yaml and .meta.yaml sidecars are not tables.
    assert "tapdrop" not in by_name
    assert "clusters_meta" not in by_name
    assert not any(s.uri.endswith("overrides.yaml") for s in skipped)


def test_hats_directory_is_detected_and_not_recursed_as_a_folder() -> None:
    groups, _ = resolve_sources([str(DATA_DIR)])
    hats_groups = [g for g in groups if g.table_name == "hats_catalog"]
    assert len(hats_groups) == 1
    assert hats_groups[0].is_hats is True
    assert hats_groups[0].files == (str((DATA_DIR / "hats_catalog").resolve()),)


def test_plain_subdirectory_is_not_recursed_into() -> None:
    # override_src/ and golden/ are plain (non-HATS) subdirectories of tests/data;
    # v1 discovery is one level deep and must not walk into them.
    groups, _ = resolve_sources([str(DATA_DIR)])
    assert not any(g.schema_name == "override_src" for g in groups)
    assert not any(g.schema_name == "golden" for g in groups)


def test_single_file_source() -> None:
    groups, skipped = resolve_sources([str(DATA_DIR / "gaia.parquet")])
    assert skipped == []
    assert len(groups) == 1
    assert groups[0].schema_name == "data"
    assert groups[0].table_name == "gaia"


def test_glob_source_groups_matches_by_shared_prefix(tmp_path: Path) -> None:
    (tmp_path / "gaia_north.parquet").write_bytes(b"")
    (tmp_path / "gaia_south.parquet").write_bytes(b"")
    (tmp_path / "other.csv").write_bytes(b"")

    groups, skipped = resolve_sources([str(tmp_path / "gaia_*.parquet")])
    assert skipped == []
    assert len(groups) == 1
    group = groups[0]
    assert group.table_name == "gaia"
    assert len(group.files) == 2


def test_glob_matching_nothing_is_skipped(tmp_path: Path) -> None:
    groups, skipped = resolve_sources([str(tmp_path / "nope_*.parquet")])
    assert groups == []
    assert len(skipped) == 1
    assert "no recognized catalog files" in skipped[0].reason


def test_unrecognized_single_file_is_skipped(tmp_path: Path) -> None:
    bogus = tmp_path / "notes.txt"
    bogus.write_text("hello")
    groups, skipped = resolve_sources([str(bogus)])
    assert groups == []
    assert len(skipped) == 1
    assert skipped[0].uri == str(bogus)


def test_missing_source_is_skipped_not_raised(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    groups, skipped = resolve_sources([str(missing)])
    assert groups == []
    assert len(skipped) == 1


def test_name_collision_gets_a_deterministic_suffix(tmp_path: Path) -> None:
    # "cats.v1" and "cats-v1" are distinct on disk but both sanitize to "cats_v1"
    # (case-insensitive filesystems already alias "Cats"/"cats" to one directory).
    (tmp_path / "cats.v1").mkdir()
    (tmp_path / "cats.v1" / "gaia.parquet").write_bytes(b"")
    (tmp_path / "cats-v1").mkdir()
    (tmp_path / "cats-v1" / "gaia.parquet").write_bytes(b"")

    groups, _ = resolve_sources([str(tmp_path / "cats.v1"), str(tmp_path / "cats-v1")])
    tables = sorted(g.table_name for g in groups)
    assert tables == ["gaia", "gaia_2"]


def test_source_group_is_frozen_and_hashable_fields() -> None:
    group = SourceGroup("s", "t", ("a", "b"))
    assert group.files == ("a", "b")
    assert group.is_hats is False
