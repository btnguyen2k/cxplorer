"""Microsoft authentication flow tests using a local OAuth test double."""

from collections.abc import Mapping

import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from cxplorer.auth.models import AuthenticatedUser
from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.main import create_app


class FakeMicrosoftClient:
    """Provide deterministic OAuth responses without contacting Microsoft."""

    def __init__(self, claims: Mapping[str, object]) -> None:
        self.claims = claims
        self.redirect_uri: str | None = None
        self.prompt: str | None = None

    async def authorize_redirect(
        self,
        request: Request,
        redirect_uri: str,
        *,
        prompt: str,
    ) -> RedirectResponse:
        self.redirect_uri = redirect_uri
        self.prompt = prompt
        return RedirectResponse("https://login.microsoftonline.test/authorize")

    async def authorize_access_token(
        self, request: Request, **_kwargs: object
    ) -> dict[str, object]:
        return {
            "access_token": "access-token-that-must-not-be-stored",
            "id_token": "id-token-that-must-not-be-stored",
            "userinfo": self.claims,
        }


class FakeOAuth:
    """Return the configured Microsoft test client."""

    def __init__(self, client: FakeMicrosoftClient) -> None:
        self.client = client

    def create_client(self, name: str) -> FakeMicrosoftClient | None:
        return self.client if name == "microsoft" else None


def microsoft_settings() -> IdentityVendorSettings:
    return IdentityVendorSettings(
        _env_file=None,
        ms_client_id="client-id",
        ms_client_secret="client-secret",
        ms_tenant="common",
    )


def test_microsoft_callback_establishes_minimal_session(app_settings: AppSettings) -> None:
    oauth_client = FakeMicrosoftClient(
        {
            "sub": "microsoft-subject",
            "name": "Grace Hopper",
            "email": "grace@example.com",
        }
    )
    app = create_app(app_settings, microsoft_settings())
    app.state.oauth = FakeOAuth(oauth_client)

    @app.get("/_test/session", include_in_schema=False)
    def session_contents(request: Request) -> dict[str, object]:
        return dict(request.session)

    with TestClient(app) as client:
        login = client.get(
            "/auth/microsoft/login",
            params={"next": "/projects?view=recent"},
            follow_redirects=False,
        )
        callback = client.get("/auth/microsoft/callback", follow_redirects=False)
        session = client.get("/_test/session").json()
        current_user = client.get("/api/private/me")

    assert login.status_code == 307
    assert login.headers["location"] == "https://login.microsoftonline.test/authorize"
    assert oauth_client.redirect_uri == "http://testserver/auth/microsoft/callback"
    assert oauth_client.prompt == "select_account"
    assert callback.status_code == 303
    assert callback.headers["location"] == "/projects?view=recent"
    assert set(session) == {"csrf_token", "user"}
    assert session["user"]["display_name"] == "Grace Hopper"
    assert current_user.status_code == 200
    assert current_user.json()["subject"] == "microsoft-subject"


def test_microsoft_callback_rejects_incomplete_identity(app_settings: AppSettings) -> None:
    app = create_app(app_settings, microsoft_settings())
    app.state.oauth = FakeOAuth(FakeMicrosoftClient({"name": "Missing Subject"}))

    with TestClient(app) as client:
        response = client.get("/auth/microsoft/callback", follow_redirects=False)
        current_user = client.get("/api/private/me")

    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?error=invalid_identity")
    assert current_user.status_code == 401


@pytest.mark.parametrize(
    ("rule", "email"),
    [
        ("seller@contoso.example", "SELLER@contoso.example"),
        ("*@partners.contoso.example", "seller@partners.contoso.example"),
    ],
)
def test_microsoft_callback_accepts_configured_email_rules(
    app_settings: AppSettings, rule: str, email: str
) -> None:
    restricted = app_settings.model_copy(update={"login_allowed_emails": (rule,)})
    app = create_app(restricted, microsoft_settings())
    app.state.oauth = FakeOAuth(
        FakeMicrosoftClient(
            {
                "sub": "contoso-seller",
                "name": "Contoso seller",
                "email": email,
            }
        )
    )

    with TestClient(app) as client:
        response = client.get("/auth/microsoft/callback", follow_redirects=False)
        current_user = client.get("/api/private/me")

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    assert current_user.status_code == 200


def test_microsoft_callback_rejects_email_outside_configured_allowlist(
    app_settings: AppSettings,
) -> None:
    restricted = app_settings.model_copy(
        update={"login_allowed_emails": ("approved@contoso.example",)}
    )
    app = create_app(restricted, microsoft_settings())
    app.state.oauth = FakeOAuth(
        FakeMicrosoftClient(
            {
                "sub": "contoso-seller",
                "name": "Contoso seller",
                "email": "other@contoso.example",
            }
        )
    )

    with TestClient(app) as client:
        response = client.get("/auth/microsoft/callback", follow_redirects=False)
        current_user = client.get("/api/private/me")
        login = client.get(response.headers["location"])

    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?error=email_not_allowed")
    assert current_user.status_code == 401
    assert "This email address is not authorized to access CXplorer." in login.text


def test_default_sign_in_opens_the_draft_workspace(app_settings: AppSettings) -> None:
    app = create_app(app_settings, microsoft_settings())
    app.state.oauth = FakeOAuth(
        FakeMicrosoftClient(
            {
                "sub": "contoso-seller",
                "name": "Contoso seller",
                "email": "seller@contoso.example",
            }
        )
    )

    with TestClient(app) as client:
        login = client.get("/auth/microsoft/login", follow_redirects=False)
        callback = client.get("/auth/microsoft/callback", follow_redirects=False)
        workspace = client.get(callback.headers["location"])

    assert login.status_code == 307
    assert callback.status_code == 303
    assert callback.headers["location"] == "/dashboard"
    assert workspace.status_code == 200
    assert "Workspace" in workspace.text
    assert "Contoso seller" in workspace.text
    assert "draft" in workspace.text.casefold()


def test_identity_without_a_display_name_uses_its_provider_email() -> None:
    user = AuthenticatedUser.from_microsoft_claims(
        {"sub": "contoso-seller", "email": "seller@contoso.example"}
    )

    assert user.display_name == "seller@contoso.example"
    assert user.provider == "microsoft"
    assert user.email == "seller@contoso.example"
