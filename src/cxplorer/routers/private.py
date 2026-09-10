"""Routes that require an authenticated session."""

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import FormData

from cxplorer.auth.dependencies import (
    get_optional_user,
    get_or_create_csrf_token,
    require_user,
    validate_csrf_token,
)
from cxplorer.auth.models import AuthenticatedUser
from cxplorer.insights.cache import MAX_BROWSER_CACHE_BYTES, ReportCache
from cxplorer.insights.errors import CacheError, InsightError
from cxplorer.insights.jobs import JobManager
from cxplorer.insights.schemas import AUDIENCE_LABELS, InsightRequest
from cxplorer.templating import templates

web_router = APIRouter(tags=["private"])
api_router = APIRouter(prefix="/api/private", tags=["private"])
REPORT_ID = re.compile(r"^[a-f0-9]{32}$")
FORM_FIELDS = (
    "homepage_url",
    "about_url",
    "products_url",
    "source_url_4",
    "source_url_5",
    "source_url_6",
    "source_purpose_4",
    "source_purpose_5",
    "source_purpose_6",
    "seller_context",
)


def _page_user(request: Request) -> AuthenticatedUser | RedirectResponse:
    user = get_optional_user(request)
    if user is None:
        login_url = request.url_for("login_page").include_query_params(next=request.url.path)
        return RedirectResponse(str(login_url), status_code=303)
    return user


def _common(request: Request, user: AuthenticatedUser) -> dict[str, object]:
    return {
        "user": user,
        "csrf_token": get_or_create_csrf_token(request),
        "cache_namespace": user.cache_namespace,
        "audience_labels": AUDIENCE_LABELS,
        "cache_max_bytes": MAX_BROWSER_CACHE_BYTES,
        "cached_list_url": request.url_for("cached_report_list"),
    }


def _workspace(
    request: Request,
    user: AuthenticatedUser,
    *,
    values: dict[str, str] | None = None,
    audiences: list[str] | None = None,
    errors: list[str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **_common(request, user),
            "form_values": values or dict.fromkeys(FORM_FIELDS, ""),
            "selected_audiences": (
                request.app.state.insights_default_audiences if audiences is None else audiences
            ),
            "submission_id": secrets.token_urlsafe(24),
            "errors": errors or [],
        },
        status_code=status_code,
    )


def _string(form: FormData, name: str) -> str:
    value = form.get(name, "")
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="File uploads are not accepted in this form.")
    return value.strip()


def _check_id(report_id: str) -> None:
    if not REPORT_ID.fullmatch(report_id):
        raise HTTPException(status_code=404, detail="Report not found.")


def _manager(request: Request) -> JobManager:
    return request.app.state.insights_jobs


def _validation_messages(error: ValidationError) -> list[str]:
    messages = []
    for issue in error.errors(include_input=False, include_url=False):
        location = issue["loc"]
        prefix = ""
        if len(location) > 1 and location[0] == "seeds" and isinstance(location[1], int):
            prefix = f"Source {location[1] + 1}: "
        messages.append(prefix + issue["msg"].removeprefix("Value error, "))
    return messages[:12]


@web_router.get(
    "/dashboard",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard",
)
def dashboard(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the private company research workspace."""
    user = _page_user(request)
    return user if isinstance(user, RedirectResponse) else _workspace(request, user)


@api_router.get("/me", response_model=AuthenticatedUser)
def current_user(
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> AuthenticatedUser:
    """Return the current authenticated identity."""
    return user


@web_router.post("/insights", response_model=None, name="create_insight")
async def create_insight(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    validate_csrf_token(request, form.get("csrf_token"))
    values = {name: _string(form, name) for name in FORM_FIELDS}
    raw_audiences = form.getlist("audiences")
    if any(not isinstance(value, str) for value in raw_audiences):
        raise HTTPException(status_code=400, detail="Invalid audience selection.")
    audiences = [value for value in raw_audiences if isinstance(value, str)]
    seeds = [{"url": values["homepage_url"], "purpose": "homepage"}]
    for name, purpose in (("about_url", "about"), ("products_url", "products")):
        if values[name]:
            seeds.append({"url": values[name], "purpose": purpose})
    for index in range(4, 7):
        if values[f"source_url_{index}"]:
            seeds.append(
                {
                    "url": values[f"source_url_{index}"],
                    "purpose": values[f"source_purpose_{index}"] or "other",
                }
            )
    try:
        submission = InsightRequest.model_validate(
            {
                "seeds": seeds,
                "audiences": audiences,
                "seller_context": values["seller_context"],
            }
        )
    except ValidationError as error:
        return _workspace(
            request,
            user,
            values=values,
            audiences=audiences,
            errors=_validation_messages(error),
            status_code=422,
        )
    if len(submission.seeds) > request.app.state.insights_max_seed_urls:
        return _workspace(
            request,
            user,
            values=values,
            audiences=audiences,
            errors=[
                f"This deployment accepts at most {request.app.state.insights_max_seed_urls} source URLs."
            ],
            status_code=422,
        )
    if not request.app.state.ai_enabled:
        return _workspace(
            request,
            user,
            values=values,
            audiences=audiences,
            errors=["No AI vendor is configured. Ask the administrator to configure AI access."],
            status_code=503,
        )
    submission_id = _string(form, "submission_id")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", submission_id):
        return _workspace(
            request,
            user,
            values=values,
            audiences=audiences,
            errors=["This workspace form has expired. Submit the refreshed form."],
            status_code=422,
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "request": submission.model_dump(mode="json"),
                "config": request.app.state.insights_config_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    try:
        job = _manager(request).submit(user.cache_namespace, submission, submission_id, fingerprint)
    except InsightError as error:
        return _workspace(
            request,
            user,
            values=values,
            audiences=audiences,
            errors=[error.public_message],
            status_code=409,
        )
    return RedirectResponse(
        str(request.url_for("insight_report", report_id=job.id)), status_code=303
    )


@web_router.get("/insights/{report_id}", response_model=None, name="insight_report")
async def insight_report(request: Request, report_id: str) -> HTMLResponse | RedirectResponse:
    _check_id(report_id)
    user = _page_user(request)
    if isinstance(user, RedirectResponse):
        return user
    manager = _manager(request)
    stored = manager.get_report(report_id, user.cache_namespace)
    context = {
        **_common(request, user),
        "report_id": report_id,
        "status_url": request.url_for("insight_status", report_id=report_id),
        "restore_url": request.url_for("restore_insight"),
    }
    if stored is not None:
        report = stored.report
        return templates.TemplateResponse(
            request=request,
            name="insights_report.html",
            context={
                **context,
                "report": report,
                "opportunities_by_id": {item.id: item for item in report.opportunities},
                "facts_by_id": {item.id: item for item in report.facts},
                "sources_by_id": {item.id: item for item in report.sources},
                "cache_payload_url": request.url_for("insight_cache_payload", report_id=report_id),
                "download_url": request.url_for("download_insight", report_id=report_id),
            },
        )
    job = manager.get_job(report_id, user.cache_namespace)
    if job is not None and job.state not in {"completed", "partial"}:
        return templates.TemplateResponse(
            request=request,
            name="insights_progress.html",
            context={
                **context,
                "job": job,
                "can_retry": manager.retry_available(report_id, user.cache_namespace),
            },
        )
    return templates.TemplateResponse(
        request=request,
        name="insights_restore.html",
        context={**context, "invalid_cache": False, "error": None},
    )


@api_router.get("/insights/{report_id}/status", name="insight_status")
async def insight_status(
    request: Request,
    report_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> dict[str, object]:
    _check_id(report_id)
    job = _manager(request).get_job(report_id, user.cache_namespace)
    stored = _manager(request).get_report(report_id, user.cache_namespace)
    if job is None and stored is None:
        raise HTTPException(status_code=404, detail="This temporary job is missing or expired.")
    return {
        "state": stored.report.status if stored is not None else job.state,
        "stage": "complete" if stored is not None else job.stage,
        "message": "Your report is ready." if job is None else job.message,
        "error": None if job is None else job.error,
        "report_url": str(request.url_for("insight_report", report_id=report_id)),
    }


@api_router.get("/insights/{report_id}/cache", name="insight_cache_payload")
async def insight_cache_payload(
    request: Request,
    report_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> dict[str, str]:
    _check_id(report_id)
    stored = _manager(request).get_report(report_id, user.cache_namespace)
    if stored is None:
        raise HTTPException(status_code=404, detail="The temporary report is no longer available.")
    codec: ReportCache = request.app.state.report_cache
    try:
        return codec.encode(stored.report, user.cache_namespace, stored.input_fingerprint)
    except CacheError as error:
        status_code = 413 if error.code == "cache_too_large" else 410
        raise HTTPException(status_code=status_code, detail=error.public_message) from error


@web_router.post("/insights/restore", response_model=None, name="restore_insight")
async def restore_insight(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    validate_csrf_token(request, form.get("csrf_token"))
    blob = _string(form, "cache_blob")
    report_id = _string(form, "report_id")
    codec: ReportCache = request.app.state.report_cache
    try:
        cached = codec.decode(blob, user.cache_namespace)
    except CacheError as error:
        return templates.TemplateResponse(
            request=request,
            name="insights_restore.html",
            status_code=400,
            context={
                **_common(request, user),
                "report_id": report_id if REPORT_ID.fullmatch(report_id) else "",
                "restore_url": request.url_for("restore_insight"),
                "invalid_cache": True,
                "error": error.public_message,
            },
        )
    try:
        stored = _manager(request).restore(cached)
    except InsightError as error:
        raise HTTPException(status_code=503, detail=error.public_message) from error
    return RedirectResponse(
        str(request.url_for("insight_report", report_id=stored.report.report_id)), status_code=303
    )


@web_router.get("/insights/{report_id}/download", name="download_insight")
async def download_insight(
    request: Request,
    report_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> Response:
    _check_id(report_id)
    stored = _manager(request).get_report(report_id, user.cache_namespace)
    if stored is None:
        raise HTTPException(status_code=404, detail="Restore the saved report before downloading.")
    return Response(
        stored.report.model_dump_json(indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="cxplorer-{report_id}.json"'},
    )


@web_router.post("/insights/{report_id}/cancel", name="cancel_insight")
async def cancel_insight(
    request: Request,
    report_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> RedirectResponse:
    _check_id(report_id)
    validate_csrf_token(request, (await request.form()).get("csrf_token"))
    if not await _manager(request).cancel(report_id, user.cache_namespace):
        raise HTTPException(status_code=404, detail="Job not found.")
    return RedirectResponse(
        str(request.url_for("insight_report", report_id=report_id)), status_code=303
    )


@web_router.post("/insights/{report_id}/retry", name="retry_insight")
async def retry_insight(
    request: Request,
    report_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> RedirectResponse:
    _check_id(report_id)
    validate_csrf_token(request, (await request.form()).get("csrf_token"))
    try:
        _manager(request).retry(report_id, user.cache_namespace)
    except InsightError as error:
        raise HTTPException(status_code=409, detail=error.public_message) from error
    return RedirectResponse(
        str(request.url_for("insight_report", report_id=report_id)), status_code=303
    )


@web_router.post("/insights/{report_id}/delete", name="delete_insight")
async def delete_insight(
    request: Request,
    report_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> RedirectResponse:
    _check_id(report_id)
    validate_csrf_token(request, (await request.form()).get("csrf_token"))
    if not await _manager(request).delete(report_id, user.cache_namespace):
        raise HTTPException(status_code=404, detail="The temporary report is already unavailable.")
    return RedirectResponse(str(request.url_for("dashboard")), status_code=303)


class CachedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    company_name: str = Field(min_length=1, max_length=240)
    generated_at: datetime
    expires_at: datetime


class CachedEntries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[CachedEntry] = Field(max_length=50)


@api_router.post("/insights/cached-list", response_class=HTMLResponse, name="cached_report_list")
async def cached_report_list(
    request: Request,
    _user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> HTMLResponse:
    validate_csrf_token(request, request.headers.get("x-csrf-token"))
    try:
        entries = CachedEntries.model_validate_json(await request.body()).entries
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="Saved report metadata is invalid.") from error
    now = datetime.now(UTC)
    valid_entries = [
        entry
        for entry in entries
        if entry.generated_at.tzinfo is not None
        and entry.expires_at.tzinfo is not None
        and entry.expires_at > now
        and entry.generated_at <= now
    ]
    return templates.TemplateResponse(
        request=request,
        name="_saved_reports.html",
        context={
            "cached_entries": sorted(
                valid_entries, key=lambda item: item.generated_at, reverse=True
            )
        },
    )
