"""Public and private route behavior tests."""

from fastapi.testclient import TestClient

from tests.conftest import TEST_CSRF_TOKEN


def test_public_routes_are_available(client: TestClient) -> None:
    landing = client.get("/")
    login = client.get("/login")
    health = client.get("/api/health")

    assert landing.status_code == 200
    assert "Explore with confidence" in landing.text
    assert login.status_code == 200
    assert "Microsoft sign-in unavailable" in login.text
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_security_headers_are_added(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_plain_stylesheet_is_served_without_a_build_step(client: TestClient) -> None:
    response = client.get("/static/css/app.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert ":root" in response.text
    assert "tailwind" not in response.text.lower()


def test_development_api_docs_allow_their_pinned_assets(client: TestClient) -> None:
    response = client.get("/api/docs")

    assert response.status_code == 200
    assert "https://cdn.jsdelivr.net" in response.text
    assert "https://cdn.jsdelivr.net" in response.headers["content-security-policy"]


def test_unauthenticated_private_routes_are_protected(client: TestClient) -> None:
    dashboard = client.get("/dashboard", follow_redirects=False)
    current_user = client.get("/api/private/me")

    assert dashboard.status_code == 303
    assert dashboard.headers["location"].endswith("/login?next=%2Fdashboard")
    assert current_user.status_code == 401
    assert current_user.headers["www-authenticate"] == "Session"


def test_configured_session_can_access_private_routes(client: TestClient) -> None:
    assert client.post("/_test/sign-in").status_code == 204

    dashboard = client.get("/dashboard")
    current_user = client.get("/api/private/me")

    assert dashboard.status_code == 200
    assert dashboard.headers["cache-control"] == "no-store"
    assert "Ada Lovelace" in dashboard.text
    assert current_user.status_code == 200
    assert current_user.json()["email"] == "ada@example.com"


def test_logout_requires_csrf_token_and_clears_session(client: TestClient) -> None:
    client.post("/_test/sign-in")

    rejected = client.post(
        "/auth/logout",
        data={"csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    accepted = client.post(
        "/auth/logout",
        data={"csrf_token": TEST_CSRF_TOKEN},
        follow_redirects=False,
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "http://testserver/"
    assert client.get("/api/private/me").status_code == 401


def test_microsoft_login_reports_missing_configuration(client: TestClient) -> None:
    response = client.get("/auth/microsoft/login", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?error=not_configured")
