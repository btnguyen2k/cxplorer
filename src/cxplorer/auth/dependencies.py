"""Authentication dependencies and session helpers."""

import logging
import secrets

from fastapi import HTTPException, Request, status
from pydantic import ValidationError

from cxplorer.auth.models import AuthenticatedUser

logger = logging.getLogger(__name__)

SESSION_USER_KEY = "user"
CSRF_TOKEN_KEY = "csrf_token"


def get_optional_user(request: Request) -> AuthenticatedUser | None:
    """Return the current session identity when it is present and valid."""
    raw_user = request.session.get(SESSION_USER_KEY)
    if raw_user is None:
        return None

    try:
        return AuthenticatedUser.model_validate(raw_user)
    except ValidationError:
        logger.warning("Discarding invalid authenticated session data")
        request.session.pop(SESSION_USER_KEY, None)
        request.session.pop(CSRF_TOKEN_KEY, None)
        return None


def require_user(request: Request) -> AuthenticatedUser:
    """Require a valid authenticated session for private API routes."""
    user = get_optional_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Session"},
        )
    return user


def get_or_create_csrf_token(request: Request) -> str:
    """Return the session CSRF token, creating one when needed."""
    token = request.session.get(CSRF_TOKEN_KEY)
    if isinstance(token, str) and token:
        return token

    token = secrets.token_urlsafe(32)
    request.session[CSRF_TOKEN_KEY] = token
    return token
