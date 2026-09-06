"""Shared test fixtures."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import Request, Response
from fastapi.testclient import TestClient

from cxplorer.auth.dependencies import CSRF_TOKEN_KEY, SESSION_USER_KEY
from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.main import create_app

TEST_CSRF_TOKEN = "test-csrf-token"
TEST_SESSION_SECRET = "test-session-secret-with-at-least-32-characters"


@pytest.fixture
def app_settings() -> AppSettings:
    """Return isolated application settings that are safe for the HTTP test client."""
    return AppSettings(
        _env_file=None,
        environment="test",
        session_secret=TEST_SESSION_SECRET,
        session_cookie_secure=False,
        allowed_hosts=["testserver"],
        docs_enabled=True,
    )


@pytest.fixture
def identity_settings() -> IdentityVendorSettings:
    """Disable external providers without reading local credentials."""
    return IdentityVendorSettings(
        _env_file=None,
        ms_client_id=None,
        ms_client_secret=None,
        ms_tenant="common",
    )


@pytest.fixture
def config_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate configuration files and recognized environment variables."""
    monkeypatch.chdir(tmp_path)
    for settings_type in (AppSettings, IdentityVendorSettings):
        for field_name in settings_type.model_fields:
            monkeypatch.delenv(field_name.upper(), raising=False)
    return tmp_path


@pytest.fixture
def client(
    app_settings: AppSettings,
    identity_settings: IdentityVendorSettings,
) -> Iterator[TestClient]:
    """Return a client with a test-only helper for creating a signed session."""
    app = create_app(app_settings, identity_settings)

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
