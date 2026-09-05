"""Routes that require an authenticated session."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from cxplorer.auth.dependencies import (
    get_optional_user,
    get_or_create_csrf_token,
    require_user,
)
from cxplorer.auth.models import AuthenticatedUser
from cxplorer.templating import templates

web_router = APIRouter(tags=["private"])
api_router = APIRouter(prefix="/api/private", tags=["private"])


@web_router.get(
    "/dashboard",
    response_class=HTMLResponse,
    response_model=None,
    name="dashboard",
)
def dashboard(request: Request) -> HTMLResponse | RedirectResponse:
    """Render the private application shell."""
    user = get_optional_user(request)
    if user is None:
        login_url = request.url_for("login_page").include_query_params(next=request.url.path)
        return RedirectResponse(url=str(login_url), status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "user": user,
            "csrf_token": get_or_create_csrf_token(request),
        },
    )


@api_router.get("/me", response_model=AuthenticatedUser)
def current_user(
    user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> AuthenticatedUser:
    """Return the current authenticated identity."""
    return user
