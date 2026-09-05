"""Microsoft authentication flow tests using a local OAuth test double."""

from collections.abc import Mapping

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from cxplorer.config import Settings
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

    async def authorize_access_token(self, request: Request) -> dict[str, object]:
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


def microsoft_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        session_secret="test-session-secret-with-at-least-32-characters",
        session_cookie_secure=False,
        allowed_hosts=["testserver"],
        ms_client_id="client-id",
        ms_client_secret="client-secret",
    )


def test_microsoft_callback_establishes_minimal_session() -> None:
    oauth_client = FakeMicrosoftClient(
        {
            "sub": "microsoft-subject",
            "name": "Grace Hopper",
            "preferred_username": "grace@example.com",
        }
    )
    app = create_app(microsoft_settings())
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


def test_microsoft_callback_rejects_incomplete_identity() -> None:
    app = create_app(microsoft_settings())
    app.state.oauth = FakeOAuth(FakeMicrosoftClient({"name": "Missing Subject"}))

    with TestClient(app) as client:
        response = client.get("/auth/microsoft/callback", follow_redirects=False)
        current_user = client.get("/api/private/me")

    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?error=invalid_identity")
    assert current_user.status_code == 401
