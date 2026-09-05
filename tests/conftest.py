"""Shared test fixtures."""

from collections.abc import Iterator

import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient

from cxplorer.auth.dependencies import CSRF_TOKEN_KEY, SESSION_USER_KEY
from cxplorer.config import Settings
from cxplorer.main import create_app

TEST_CSRF_TOKEN = "test-csrf-token"


@pytest.fixture
def settings() -> Settings:
    """Return isolated settings that are safe for the HTTP test client."""
    return Settings(
        _env_file=None,
        environment="test",
        session_secret="test-session-secret-with-at-least-32-characters",
        session_cookie_secure=False,
        allowed_hosts=["testserver"],
        docs_enabled=True,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """Return a client with a test-only helper for creating a signed session."""
    app = create_app(settings)

    @app.post("/_test/sign-in", include_in_schema=False)
    def test_sign_in(request: Request) -> Response:
        request.session[SESSION_USER_KEY] = {
            "provider": "microsoft",
            "subject": "test-subject",
            "display_name": "Ada Lovelace",
            "email": "ada@example.com",
        }
        request.session[CSRF_TOKEN_KEY] = TEST_CSRF_TOKEN
        return Response(status_code=204)

    with TestClient(app) as test_client:
        yield test_client
