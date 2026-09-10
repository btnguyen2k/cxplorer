"""Provider email requirements and email-only browser identity."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from cxplorer.auth.models import AuthenticatedUser, AuthenticationEmailError
from cxplorer.config import AppSettings
from cxplorer.main import create_app
from tests.test_authentication import FakeMicrosoftClient, FakeOAuth, microsoft_settings


@pytest.mark.parametrize("email", [None, "", "not an email", 12])
def test_callback_rejects_missing_or_invalid_email(
    app_settings: AppSettings, email: object
) -> None:
    app = create_app(app_settings, microsoft_settings())
    app.state.oauth = FakeOAuth(
        FakeMicrosoftClient(
            {
                "sub": "contoso-user",
                "name": "Contoso seller",
                "preferred_username": "seller@contoso.example",
                "email": email,
            }
        )
    )
    with TestClient(app) as client:
        response = client.get("/auth/microsoft/callback", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].endswith("/login?error=email_required")
        assert client.get("/api/private/me").status_code == 401
        assert "did not share a usable email" in client.get(response.headers["location"]).text


def test_username_is_not_substituted_for_provider_email() -> None:
    with pytest.raises(AuthenticationEmailError):
        AuthenticatedUser.from_microsoft_claims(
            {"sub": "contoso-user", "preferred_username": "seller@contoso.example"}
        )


def test_cache_namespace_is_exact_email_sha256_not_provider_subject_or_name() -> None:
    email = "seller@contoso.example"
    first = AuthenticatedUser(
        provider="microsoft", subject="first-subject", display_name="Contoso seller", email=email
    )
    second = first.model_copy(update={"subject": "another-subject", "display_name": "Updated"})
    assert first.cache_namespace == hashlib.sha256(email.encode("utf-8")).hexdigest()
    assert second.cache_namespace == first.cache_namespace
    assert first.model_copy(update={"email": "other@contoso.example"}).cache_namespace != (
        first.cache_namespace
    )


def test_non_ascii_csrf_is_rejected_not_a_server_error(client: TestClient) -> None:
    client.post("/_test/sign-in")
    assert client.post("/auth/logout", data={"csrf_token": "\u2603"}).status_code == 403
