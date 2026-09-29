"""Landing page and drag-drop ingest.

``static/index.html`` is a single self-contained file with no build step, so
serving it is one route. ``POST /upload`` is the other half of the same story:
a file dropped on that page becomes a table in the ``uploads`` schema, readable
by every TAP client pointed at this service.

Uploads live in a temporary directory that is removed when the process exits,
which keeps the RDD.md promise that an upload is as temporary as the service.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from tapdrop.discovery import discover
from tapdrop.errors import InvalidParameterError, UploadTooLargeError

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

    from tapdrop.config import Settings
    from tapdrop.registry import Registry

__all__ = ["STATIC_DIR", "UploadArea", "router"]

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

#: Everything dropped on the page lands in one schema, so a client can tell an
#: uploaded table from a served one at a glance.
UPLOAD_SCHEMA = "uploads"

router = APIRouter()


class UploadArea:
    """The temp directory uploads are written to, and its lifetime.

    One per app. ``cleanup`` is wired to the ASGI shutdown event, so the files
    outlive a request but never the process.
    """

    def __init__(self) -> None:
        self._root: Path | None = None

    @property
    def path(self) -> Path:
        if self._root is None:
            # The last path element becomes the TAP schema name, so it is fixed
            # rather than the random part of the temp directory's name.
            self._root = Path(tempfile.mkdtemp(prefix="tapdrop-")) / UPLOAD_SCHEMA
            self._root.mkdir()
        return self._root

    def cleanup(self) -> None:
        if self._root is not None:
            shutil.rmtree(self._root.parent, ignore_errors=True)
            self._root = None


@router.get("/", include_in_schema=False)
async def landing_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")


@router.post("/upload")
async def upload(request: Request, file: list[UploadFile] = File(...)) -> JSONResponse:
    settings: Settings = request.app.state.settings
    registry: Registry = request.app.state.registry
    con: duckdb.DuckDBPyConnection = request.app.state.con
    area: UploadArea = request.app.state.uploads

    if not settings.allow_upload:
        raise InvalidParameterError("upload", "not enabled; start the server with --allow-upload")

    # The field name is "file", singular, because that is what the landing page
    # sends, once per dropped file; FastAPI collects the repeats into a list.
    saved = [await _save(upload_file, area.path, settings.upload_max_mb) for upload_file in file]
    added = await run_in_threadpool(_register, saved, registry, con)
    return JSONResponse({"tables": added})


async def _save(upload_file: UploadFile, destination: Path, max_mb: int) -> Path:
    """Stream one upload to disk, refusing it as soon as it exceeds the cap.

    Checking while streaming rather than after: a 10 GB upload must not be
    written to disk in full just to be rejected.
    """
    name = Path(upload_file.filename or "upload").name  # strip any path the client sent
    target = destination / name
    limit = max_mb * 1024 * 1024
    written = 0
    with target.open("wb") as sink:
        while chunk := await upload_file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                sink.close()
                target.unlink(missing_ok=True)
                raise UploadTooLargeError(max_mb)
            sink.write(chunk)
    return target


def _register(paths: list[Path], registry: Registry, con: duckdb.DuckDBPyConnection) -> list[str]:
    """Discover the uploaded files and make them queryable, keeping what is already served."""
    discovered = discover([str(path) for path in paths])
    if not discovered.tables:
        reasons = "; ".join(f"{s.uri}: {s.reason}" for s in discovered.skipped)
        raise InvalidParameterError("upload", reasons or "no table could be read from the upload")

    registry.tables.update(discovered.tables)
    registry.attach(con)
    registry.populate_tap_schema(con)
    return sorted(discovered.tables)
