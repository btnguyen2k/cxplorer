"""Configuration behavior tests."""

import pytest
from pydantic import ValidationError

from cxplorer.config import Settings


def test_default_application_name_uses_official_branding() -> None:
    settings = Settings(
        _env_file=None,
        session_secret="test-session-secret-with-at-least-32-characters",
    )

    assert settings.app_name == "CXplorer"


def test_microsoft_credentials_must_be_configured_together() -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(
            _env_file=None,
            session_secret="test-session-secret-with-at-least-32-characters",
            ms_client_id="client-id",
        )


def test_production_defaults_are_secure() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        session_secret="test-session-secret-with-at-least-32-characters",
    )

    assert settings.use_secure_cookies is True
    assert settings.expose_api_docs is False


def test_ms_environment_variables_configure_microsoft_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MS_CLIENT_ID", "client-id")
    monkeypatch.setenv("MS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("MS_TENANT", "organizations")

    settings = Settings(
        _env_file=None,
        session_secret="test-session-secret-with-at-least-32-characters",
    )

    assert settings.ms_client_id == "client-id"
    assert settings.ms_client_secret
    assert settings.ms_client_secret.get_secret_value() == "client-secret"
    assert settings.ms_tenant == "organizations"
    assert settings.microsoft_auth_enabled is True
