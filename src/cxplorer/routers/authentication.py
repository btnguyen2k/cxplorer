"""Microsoft OpenID Connect login and local logout routes."""

import logging
import secrets
from collections.abc import Mapping
from typing import Annotated

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from httpx import HTTPError
from pydantic import ValidationError

from cxplorer.auth.dependencies import (
    CSRF_TOKEN_KEY,
    SESSION_USER_KEY,
    require_user,
)
from cxplorer.auth.models import AuthenticatedUser, AuthenticationClaimsError
from cxplorer.auth.redirects import safe_local_path
from cxplorer.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])
POST_AUTH_REDIRECT_KEY = "post_auth_redirect"


def _login_error_response(request: Request, error: str) -> RedirectResponse:
    login_url = request.url_for("login_page").include_query_params(error=error)
    return RedirectResponse(url=str(login_url), status_code=303)


def _microsoft_client(request: Request):
    client = request.app.state.oauth.create_client("microsoft")
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Microsoft authentication is unavailable",
        )
    return client


@router.get("/microsoft/login", name="microsoft_login")
async def microsoft_login(
    request: Request,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    """Start Microsoft OpenID Connect authorization."""
    settings: Settings = request.app.state.settings
    if not settings.microsoft_auth_enabled:
        return _login_error_response(request, "not_configured")

    request.session[POST_AUTH_REDIRECT_KEY] = safe_local_path(next_path)
    redirect_uri = str(request.url_for("microsoft_callback"))
    client = _microsoft_client(request)
    return await client.authorize_redirect(
        request,
        redirect_uri,
        prompt="select_account",
    )


@router.get("/microsoft/callback", name="microsoft_callback")
async def microsoft_callback(request: Request) -> RedirectResponse:
    """Validate Microsoft's callback and establish the local session."""
    settings: Settings = request.app.state.settings
    if not settings.microsoft_auth_enabled:
        return _login_error_response(request, "not_configured")

    client = _microsoft_client(request)
    try:
        token = await client.authorize_access_token(request)
        claims = token.get("userinfo")
        if not isinstance(claims, Mapping):
            claims = await client.userinfo(token=token)
    except (HTTPError, OAuthError) as error:
        logger.warning("Microsoft OAuth callback failed: %s", type(error).__name__)
        request.session.clear()
        return _login_error_response(request, "authentication_failed")

    try:
        if not isinstance(claims, Mapping):
            raise AuthenticationClaimsError("Microsoft did not return identity claims")
        user = AuthenticatedUser.from_microsoft_claims(claims)
    except (AuthenticationClaimsError, ValidationError):
        logger.warning("Microsoft returned invalid identity claims")
        request.session.clear()
        return _login_error_response(request, "invalid_identity")

    destination = safe_local_path(request.session.get(POST_AUTH_REDIRECT_KEY))
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.model_dump(mode="json")
    request.session[CSRF_TOKEN_KEY] = secrets.token_urlsafe(32)
    return RedirectResponse(url=destination, status_code=303)


@router.post("/logout", name="logout")
def logout(
    request: Request,
    csrf_token: Annotated[str, Form()],
    _user: Annotated[AuthenticatedUser, Depends(require_user)],
) -> RedirectResponse:
    """Clear the local session after validating the anti-CSRF token."""
    expected_token = request.session.get(CSRF_TOKEN_KEY)
    if (
        not isinstance(expected_token, str)
        or not expected_token
        or not secrets.compare_digest(csrf_token, expected_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token",
        )

    request.session.clear()
    return RedirectResponse(url=str(request.url_for("landing_page")), status_code=303)
