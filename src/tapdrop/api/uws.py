"""TAP async endpoint: UWS 1.1 job resources.

Mirrors ``api/tap.py``'s shape (parse parameters, reuse the shared
``JobManager``, serialise) but every handler is a thin wrapper around
``tapdrop.uws.JobManager`` — the state machine and the runner live there, not
here. Job state and results are read from ``request.app.state.uws``, the same
way ``_handle_sync`` reads ``request.app.state.con``.

Everything under ``/tap/async`` is UWS 1.1 XML in the
``http://www.ivoa.net/xml/UWS/v1.0`` namespace. URLs embedded in that XML are
always built from :func:`_job_url`/:func:`_list_url`, which respect
``settings.public_url`` (for services behind a tunnel or proxy) and
``settings.root_path`` (the ``/t/<token>`` prefix) — a client follows the URL
this service prints, not the one it meant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from tapdrop.api import dali_timestamp
from tapdrop.api.params import parse_tap_request
from tapdrop.errors import InvalidParameterError, TapdropError
from tapdrop.output import content_type, votable_error
from tapdrop.uws import Job, JobManager, Phase

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from tapdrop.config import Settings

router = APIRouter()

_UWS_NS = 'xmlns:uws="http://www.ivoa.net/xml/UWS/v1.0"'
_XLINK_NS = 'xmlns:xlink="http://www.w3.org/1999/xlink"'
_XSI_NS = 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


@router.get("/tap/async")
async def async_list(request: Request) -> Response:
    manager: JobManager = request.app.state.uws
    settings: Settings = request.app.state.settings
    jobs = await run_in_threadpool(manager.list_jobs)
    body = _jobs_xml(jobs, request, settings)
    return Response(content=body, media_type="text/xml")


@router.post("/tap/async")
async def async_create(request: Request) -> Response:
    form = await request.form()
    params = [(name, str(value)) for name, value in form.multi_items()]
    params.extend(request.query_params.multi_items())
    return await _handle_create(request, params)


async def _handle_create(request: Request, params: list[tuple[str, str]]) -> Response:
    settings: Settings = request.app.state.settings
    manager: JobManager = request.app.state.uws
    try:
        tap_request = parse_tap_request(params)
        if tap_request.uploads and not settings.allow_upload:
            raise InvalidParameterError(
                "UPLOAD", "not enabled; start the server with --allow-upload"
            )
    except TapdropError as exc:
        return _error_response(exc)

    job = await run_in_threadpool(manager.create_job, tap_request)
    # TAP 1.1: a job creation request may include PHASE=RUN to start the job
    # immediately instead of requiring a separate POST to .../phase.
    if _first_param(params, "phase") == "RUN":
        await run_in_threadpool(manager.run_job, job.job_id)
    return Response(status_code=303, headers={"Location": _job_url(request, settings, job.job_id)})


@router.get("/tap/async/{job_id}")
async def async_get(request: Request, job_id: str) -> Response:
    settings: Settings = request.app.state.settings
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    return Response(content=_job_xml(job, request, settings), media_type="text/xml")


@router.delete("/tap/async/{job_id}")
async def async_delete(request: Request, job_id: str) -> Response:
    settings: Settings = request.app.state.settings
    manager: JobManager = request.app.state.uws
    try:
        await run_in_threadpool(manager.destroy_job, job_id)
    except KeyError:
        return _not_found()
    return Response(status_code=303, headers={"Location": _list_url(request, settings)})


@router.get("/tap/async/{job_id}/phase")
async def phase_get(request: Request, job_id: str) -> Response:
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    return PlainTextResponse(job.phase.value)


@router.post("/tap/async/{job_id}/phase")
async def phase_post(request: Request, job_id: str) -> Response:
    settings: Settings = request.app.state.settings
    manager: JobManager = request.app.state.uws
    form = await request.form()
    params = [(name, str(value)) for name, value in form.multi_items()]
    params.extend(request.query_params.multi_items())
    action = _first_param(params, "phase") or ""

    try:
        if action == "RUN":
            await run_in_threadpool(manager.run_job, job_id)
        elif action == "ABORT":
            await run_in_threadpool(manager.abort_job, job_id)
        else:
            raise InvalidParameterError("PHASE", f"{action!r} must be RUN or ABORT")
    except KeyError:
        return _not_found()
    except TapdropError as exc:
        return _error_response(exc)
    return Response(status_code=303, headers={"Location": _job_url(request, settings, job_id)})


@router.get("/tap/async/{job_id}/quote")
async def quote_get(request: Request, job_id: str) -> Response:
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    return PlainTextResponse(dali_timestamp(job.quote) if job.quote else "")


@router.get("/tap/async/{job_id}/executionduration")
async def execution_duration_get(request: Request, job_id: str) -> Response:
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    return PlainTextResponse(str(int(job.execution_duration)))


@router.get("/tap/async/{job_id}/destruction")
async def destruction_get(request: Request, job_id: str) -> Response:
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    return PlainTextResponse(dali_timestamp(job.destruction))


@router.get("/tap/async/{job_id}/error")
async def error_get(request: Request, job_id: str) -> Response:
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    if job.phase is not Phase.ERROR or not job.error_message:
        return _not_found()
    return Response(
        content=votable_error(job.error_message), media_type="application/x-votable+xml"
    )


@router.get("/tap/async/{job_id}/parameters")
async def parameters_get(request: Request, job_id: str) -> Response:
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    return Response(content=_parameters_xml(job).encode("utf-8"), media_type="text/xml")


@router.get("/tap/async/{job_id}/results")
async def results_get(request: Request, job_id: str) -> Response:
    settings: Settings = request.app.state.settings
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    href = f"{_job_url(request, settings, job_id)}/results/result"
    return Response(content=_results_xml(job, href).encode("utf-8"), media_type="text/xml")


@router.get("/tap/async/{job_id}/results/result")
async def result_get(request: Request, job_id: str) -> Response:
    try:
        job = await _require_job(request, job_id)
    except KeyError:
        return _not_found()
    if job.phase is not Phase.COMPLETED or job.result_path is None:
        return _not_found()
    body = await run_in_threadpool(job.result_path.read_bytes)
    return Response(content=body, media_type=content_type(job.request.fmt))


async def _require_job(request: Request, job_id: str) -> Job:
    manager: JobManager = request.app.state.uws
    return await run_in_threadpool(manager.get_job, job_id)


def _first_param(params: list[tuple[str, str]], name: str) -> str | None:
    for key, value in params:
        if key.strip().lower() == name:
            return value.strip().upper()
    return None


def _not_found() -> Response:
    return PlainTextResponse("Not found.", status_code=404)


def _error_response(exc: TapdropError) -> Response:
    # A small, deliberate duplicate of `api.tap._error_response`: importing it
    # here at module level would make `api.tap` and `api.uws` import each
    # other (tap.py mounts this router), so this stays a local copy rather
    # than a shared import.
    headers = {"Retry-After": "30"} if exc.retry else None
    return Response(
        content=votable_error(exc.message),
        status_code=exc.http_status,
        media_type="application/x-votable+xml",
        headers=headers,
    )


def _service_root(request: Request, settings: Settings) -> str:
    if settings.public_url:
        base = settings.public_url.rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
    return f"{base}{settings.root_path}"


def _list_url(request: Request, settings: Settings) -> str:
    return f"{_service_root(request, settings)}/tap/async"


def _job_url(request: Request, settings: Settings, job_id: str) -> str:
    return f"{_list_url(request, settings)}/{job_id}"


def _jobs_xml(jobs: list[Job], request: Request, settings: Settings) -> bytes:
    items = "".join(
        f'<uws:jobref id="{escape(job.job_id)}" '
        f'xlink:href="{escape(_job_url(request, settings, job.job_id))}">'
        f"<uws:phase>{job.phase.value}</uws:phase></uws:jobref>"
        for job in jobs
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><uws:jobs {_UWS_NS} {_XLINK_NS}>{items}</uws:jobs>'
    ).encode()


def _job_xml(job: Job, request: Request, settings: Settings) -> bytes:
    href = f"{_job_url(request, settings, job.job_id)}/results/result"
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<uws:job {_UWS_NS} {_XLINK_NS} {_XSI_NS}>",
        f"<uws:jobId>{escape(job.job_id)}</uws:jobId>",
    ]
    if job.request.runid:
        parts.append(f"<uws:runId>{escape(job.request.runid)}</uws:runId>")
    parts.append('<uws:ownerId xsi:nil="true"/>')
    parts.append(f"<uws:phase>{job.phase.value}</uws:phase>")
    parts.append(_nillable_time_xml("quote", job.quote))
    parts.append(f"<uws:creationTime>{dali_timestamp(job.creation_time)}</uws:creationTime>")
    parts.append(_nillable_time_xml("startTime", job.start_time))
    parts.append(_nillable_time_xml("endTime", job.end_time))
    parts.append(f"<uws:executionDuration>{int(job.execution_duration)}</uws:executionDuration>")
    parts.append(f"<uws:destruction>{dali_timestamp(job.destruction)}</uws:destruction>")
    parts.append(_parameters_xml(job))
    parts.append(_results_xml(job, href))
    parts.append(_error_summary_xml(job))
    parts.append("</uws:job>")
    return "".join(parts).encode("utf-8")


def _nillable_time_xml(tag: str, value: datetime | None) -> str:
    if value is None:
        return f'<uws:{tag} xsi:nil="true"/>'
    return f"<uws:{tag}>{dali_timestamp(value)}</uws:{tag}>"


def _parameters_xml(job: Job) -> str:
    request = job.request
    values: list[tuple[str, str | None]] = [
        ("query", request.query),
        ("lang", "ADQL"),
        ("format", request.fmt),
        ("maxrec", str(request.maxrec) if request.maxrec is not None else None),
        ("runid", request.runid),
    ]
    items = "".join(
        f'<uws:parameter id="{name}">{escape(value)}</uws:parameter>'
        for name, value in values
        if value is not None
    )
    return f"<uws:parameters {_UWS_NS}>{items}</uws:parameters>"


def _results_xml(job: Job, href: str) -> str:
    if job.phase is not Phase.COMPLETED:
        return f"<uws:results {_UWS_NS}/>"
    return (
        f"<uws:results {_UWS_NS} {_XLINK_NS}>"
        f'<uws:result id="result" xlink:href="{escape(href)}"/>'
        "</uws:results>"
    )


def _error_summary_xml(job: Job) -> str:
    if job.phase is not Phase.ERROR or not job.error_message:
        return ""
    return (
        '<uws:errorSummary type="fatal" hasDetail="false">'
        f"<uws:message>{escape(job.error_message)}</uws:message>"
        "</uws:errorSummary>"
    )
