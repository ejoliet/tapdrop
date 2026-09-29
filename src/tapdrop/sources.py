"""Resolve mixed sources (folder / glob / ``s3://`` / ``https://``) into file groups.

One code path covers every scheme because everything goes through ``fsspec``.
This module only *groups* files into candidate tables (discovery rule 1 in
``RDD.md``); it never opens a file to look inside it — that is
``discovery/catalog.py`` and ``discovery/hats.py``.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

import fsspec

# AIDEV-NOTE: keep this list in sync with the six v1 formats in RDD.md
# "Discovery rules (v1, catalogs)". ".fits.gz" is checked separately because
# splitext() only strips the last suffix.
_DATA_EXTENSIONS = {".parquet", ".fits", ".fit", ".csv", ".tsv", ".ecsv", ".vot", ".xml"}
_SIDECAR_SUFFIXES = (".meta.yaml", ".meta.yml")
_IDENT_RE = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True)
class SkippedFile:
    """A URI that discovery could not turn into a table, with the reason."""

    uri: str
    reason: str


@dataclass(frozen=True)
class SourceGroup:
    """One candidate table: a schema/table name and the files that back it."""

    schema_name: str
    table_name: str
    files: tuple[str, ...]
    is_hats: bool = False


def sanitize_identifier(name: str) -> str:
    """Turn a folder/file name into a legal ``[a-z_][a-z0-9_]*`` SQL identifier."""
    text = _IDENT_RE.sub("_", name.strip().lower()).strip("_")
    if not text:
        text = "x"
    if not re.match(r"^[a-z_]", text):
        text = f"t_{text}"
    return text


def _has_data_extension(name: str) -> bool:
    lower = name.lower()
    if lower.endswith(".fits.gz"):
        return True
    return posixpath.splitext(lower)[1] in _DATA_EXTENSIONS


def _is_sidecar_or_config(name: str) -> bool:
    lower = name.lower()
    return lower.endswith(_SIDECAR_SUFFIXES) or lower == "tapdrop.yaml"


def _strip_data_extension(name: str) -> str:
    if name.lower().endswith(".fits.gz"):
        return name[: -len(".fits.gz")]
    return posixpath.splitext(name)[0]


def _protocol_of(fs: fsspec.AbstractFileSystem) -> str:
    proto = fs.protocol
    return str(proto[0] if isinstance(proto, (list, tuple)) else proto)


def to_uri(fs: fsspec.AbstractFileSystem, path: str) -> str:
    if "://" in path:
        return path
    protocol = _protocol_of(fs)
    if protocol in ("file", "local"):
        return path
    return f"{protocol}://{path}"


def _basename(path: str) -> str:
    return posixpath.basename(path.rstrip("/"))


def _dirname(path: str) -> str:
    return posixpath.dirname(path.rstrip("/"))


def _glob_prefix(basename: str) -> str:
    """Wildcard prefix of a glob basename: ``gaia_*.parquet`` -> ``gaia``."""
    idx = min((basename.index(c) for c in "*?[" if c in basename), default=len(basename))
    return basename[:idx].rstrip("_-. ")


class _GroupAccumulator:
    """Merges candidate groups, giving collisions a deterministic suffix.

    Two different sources that sanitize to the same ``schema.table`` (e.g.
    ``Cats/`` and ``cats/``) must not silently overwrite one another. The
    first one wins the plain name; later ones get ``_2``, ``_3``, ... in the
    order they were encountered.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, SourceGroup] = {}

    def add(self, schema: str, table: str, files: list[str], *, is_hats: bool = False) -> None:
        if not files:
            return
        key = f"{schema}.{table}"
        existing = self._by_key.get(key)
        if existing is None:
            self._by_key[key] = SourceGroup(schema, table, tuple(files), is_hats)
            return
        if set(existing.files) == set(files):
            return  # same source seen twice (e.g. overlapping globs); ignore
        n = 2
        while f"{schema}.{table}_{n}" in self._by_key:
            n += 1
        new_table = f"{table}_{n}"
        new_group = SourceGroup(schema, new_table, tuple(files), is_hats)
        self._by_key[f"{schema}.{new_table}"] = new_group

    def groups(self) -> list[SourceGroup]:
        return list(self._by_key.values())


def resolve_sources(source_uris: list[str]) -> tuple[list[SourceGroup], list[SkippedFile]]:
    """Resolve mixed URIs into table-sized file groups, per discovery rule 1."""
    # Local import: breaks the sources -> discovery.hats -> catalog -> registry
    # -> sources module-load cycle (registry needs SkippedFile/SourceGroup).
    from tapdrop.discovery.hats import is_hats_dir

    acc = _GroupAccumulator()
    skipped: list[SkippedFile] = []

    for uri in source_uris:
        fs, root = fsspec.core.url_to_fs(uri)
        root = root.rstrip("/") or "/"
        is_glob = any(ch in root for ch in "*?[")

        if is_glob:
            matches = sorted(fs.glob(root))
            data_matches = [m for m in matches if _has_data_extension(m)]
            if not data_matches:
                skipped.append(SkippedFile(uri, "glob matched no recognized catalog files"))
                continue
            parent = _dirname(root.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0])
            schema = sanitize_identifier(_basename(parent)) if _basename(parent) else "data"
            table = sanitize_identifier(_glob_prefix(_basename(root))) or "data"
            files = [to_uri(fs, m) for m in data_matches]
            acc.add(schema, table, files)
            continue

        try:
            is_dir = fs.isdir(root)
        except OSError as exc:
            skipped.append(SkippedFile(uri, f"could not stat source: {exc}"))
            continue

        if is_dir:
            if is_hats_dir(fs, root):
                schema = sanitize_identifier(_basename(_dirname(root))) or "data"
                table = sanitize_identifier(_basename(root))
                acc.add(schema, table, [to_uri(fs, root)], is_hats=True)
                continue

            schema = sanitize_identifier(_basename(root)) or "data"
            try:
                entries = sorted(fs.ls(root, detail=False))
            except OSError as exc:
                skipped.append(SkippedFile(uri, f"could not list directory: {exc}"))
                continue
            for entry in entries:
                base = _basename(entry)
                if not base or _is_sidecar_or_config(base):
                    continue
                try:
                    entry_is_dir = fs.isdir(entry)
                except OSError as exc:
                    skipped.append(SkippedFile(to_uri(fs, entry), f"could not stat entry: {exc}"))
                    continue
                if entry_is_dir:
                    if is_hats_dir(fs, entry):
                        table = sanitize_identifier(base)
                        acc.add(schema, table, [to_uri(fs, entry)], is_hats=True)
                    # AIDEV-NOTE: plain (non-HATS) subdirectories are not recursed
                    # into in v1 - out of scope for M1, folders are one level deep.
                    continue
                if not _has_data_extension(base):
                    continue
                table = sanitize_identifier(_strip_data_extension(base))
                acc.add(schema, table, [to_uri(fs, entry)])
            continue

        # Single file.
        base = _basename(root)
        if not _has_data_extension(base):
            skipped.append(SkippedFile(uri, "not a recognized catalog format"))
            continue
        parent_base = _basename(_dirname(root))
        # DEVIATION: RDD Open Question "default schema for a bare URL" resolved
        # as "data" - see implementation-notes.md.
        schema = sanitize_identifier(parent_base) if parent_base else "data"
        table = sanitize_identifier(_strip_data_extension(base))
        acc.add(schema, table, [to_uri(fs, root)])

    return acc.groups(), skipped
