"""Public pages and APIs."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from cxplorer.auth.dependencies import get_optional_user
from cxplorer.auth.redirects import safe_local_path
from cxplorer.config import Settings
from cxplorer.templating import templates

router = APIRouter(tags=["public"])


class HealthResponse(BaseModel):
    """Public health response."""

    status: Literal["ok"] = "ok"


@router.get("/", response_class=HTMLResponse, name="landing_page")
def landing_page(request: Request) -> HTMLResponse:
    """Render the public landing page."""
    return templates.TemplateResponse(
        request=request,
        name="landing.html",
        context={"user": get_optional_user(request)},
    )


@router.get("/login", response_class=HTMLResponse, name="login_page")
def login_page(
    request: Request,
    error: str | None = None,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> HTMLResponse:
    """Render the public login page."""
    settings: Settings = request.app.state.settings
    destination = safe_local_path(next_path)
    microsoft_login_url = request.url_for("microsoft_login").include_query_params(next=destination)
    error_message = {
        "authentication_failed": "Microsoft could not verify this sign-in. Please try again.",
        "invalid_identity": "Microsoft returned an incomplete identity. Please try another account.",
        "not_configured": "Microsoft sign-in has not been configured for this environment.",
    }.get(error)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "user": get_optional_user(request),
            "microsoft_enabled": settings.microsoft_auth_enabled,
            "microsoft_login_url": microsoft_login_url,
            "error_message": error_message,
        },
    )


@router.get("/api/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """Report whether the web process can serve requests."""
    return HealthResponse()
