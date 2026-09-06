"""Public pages and APIs."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from cxplorer.auth.dependencies import get_optional_user
from cxplorer.auth.redirects import safe_local_path
from cxplorer.config import IdentityVendorSettings
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
    identity_settings: IdentityVendorSettings = request.app.state.identity_settings
    destination = safe_local_path(next_path)
    error_message = {
        "authentication_failed": "We couldn't complete your sign-in. Please try again.",
        "invalid_identity": (
            "Your account provider didn't return the details needed to sign in. "
            "Please try another account."
        ),
        "not_configured": (
            "Sign-in is not available in this environment yet. Please contact the administrator."
        ),
    }.get(error)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "user": get_optional_user(request),
            "login_providers": [
                {
                    "name": "Microsoft Entra ID",
                    "url": request.url_for("microsoft_login").include_query_params(
                        next=destination
                    ),
                    "enabled": identity_settings.microsoft_auth_enabled,
                }
            ],
            "error_message": error_message,
        },
    )


@router.get("/api/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """Report whether the web process can serve requests."""
    return HealthResponse()
