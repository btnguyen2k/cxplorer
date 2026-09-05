"""Public and private route behavior tests."""

from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cxplorer.config import Settings
from cxplorer.main import create_app
from tests.conftest import TEST_CSRF_TOKEN


class RenderedPage(HTMLParser):
    """Collect copy, accessible labels, and links without counting URLs as page copy."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.copy: list[str] = []
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.links: list[dict[str, str]] = []
        self._active_link: dict[str, str] | None = None
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        self.copy.extend(
            value
            for name, value in attrs
            if name in {"aria-label", "aria-description", "alt", "title"} and value
        )
        if tag == "meta" and attributes.get("name") == "description":
            self.copy.append(attributes.get("content") or "")
        if tag == "a":
            self._active_link = {
                "href": attributes.get("href") or "",
                "class": attributes.get("class") or "",
                "aria-describedby": attributes.get("aria-describedby") or "",
                "text": "",
            }
            self.links.append(self._active_link)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._active_link = None

    def handle_data(self, data: str) -> None:
        self.copy.append(data)
        if self._active_link is not None:
            self._active_link["text"] += data

    @property
    def text(self) -> str:
        return " ".join(" ".join(self.copy).split())


def test_public_routes_are_available(client: TestClient) -> None:
    landing = client.get("/")
    login = client.get("/login")
    health = client.get("/api/health")

    assert landing.status_code == 200
    assert "Walk in already" in landing.text
    assert login.status_code == 200
    assert "Microsoft sign-in unavailable" in login.text
    assert "Continue with the Microsoft account connected to your workspace." in login.text
    assert "OpenID Connect verification" in login.text
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


@pytest.mark.parametrize("signed_in", [False, True])
def test_landing_page_focuses_on_business_outcomes(client: TestClient, signed_in: bool) -> None:
    if signed_in:
        assert client.post("/_test/sign-in").status_code == 204

    response = client.get("/")
    assert response.status_code == 200
    page = RenderedPage(response.text)

    assert [tag for tag, _ in page.elements].count("h1") == 1
    for copy in (
        "CXplorer",
        "Built for technical sellers",
        "Walk in already understanding their business.",
        "A relevant conversation for every room.",
        "web address",
        "what the business is trying to win",
        "what each executive is accountable for",
        "where AI belongs on their agenda",
        "CEO",
        "CTO",
        "CIO",
        "CFO",
        "Security leadership",
        "Growth agenda",
        "Return on spend",
        "Risk & resilience",
        "Where the AI conversation starts",
        "Read the business",
        "Map the room",
        "Make AI concrete",
        "Bring the business conversation, not the product tour.",
    ):
        assert copy in page.text

    assert "Prepared for the CEO" not in page.text

    for technical_marketing in (
        "microsoft",
        "openid",
        "oidc",
        "oauth",
        "secure by design",
        "secure workspace",
        "fastapi",
        "python",
        "framework",
        "server-rendered",
        "ssr",
        "html",
        "css",
        "foundation",
        "session boundaries",
        "route groups",
        "trusted identity",
        "responsive by default",
    ):
        assert technical_marketing not in page.text.casefold()


@pytest.mark.parametrize("signed_in", [False, True])
def test_landing_states_that_insights_are_not_generated_yet(
    client: TestClient, signed_in: bool
) -> None:
    if signed_in:
        assert client.post("/_test/sign-in").status_code == 204

    response = client.get("/")
    page = RenderedPage(response.text)

    assert "Insight generation is not available yet." in page.text
    assert "An illustrative brief showing what CXplorer is being built to produce." in page.text

    captions = [
        attributes
        for tag, attributes in page.elements
        if tag == "figcaption" and attributes.get("id") == "lp-availability"
    ]
    assert len(captions) == 1

    # Every call to action points at the availability note instead of promising a report.
    actions = [link for link in page.links if "lp-cta" in link["class"].split()]
    assert actions
    assert all(link["aria-describedby"] == "lp-availability" for link in actions)


@pytest.mark.parametrize(
    ("signed_in", "action_label", "nav_label", "destination"),
    [
        (False, "Sign in to CXplorer", "Sign in", "/login"),
        (True, "Open your workspace", "Dashboard", "/dashboard"),
    ],
)
def test_landing_actions_use_existing_destinations(
    client: TestClient,
    signed_in: bool,
    action_label: str,
    nav_label: str,
    destination: str,
) -> None:
    if signed_in:
        assert client.post("/_test/sign-in").status_code == 204

    response = client.get("/")
    page = RenderedPage(response.text)
    expected_url = f"http://testserver{destination}"

    actions = [link for link in page.links if "lp-cta" in link["class"].split()]
    assert actions
    for link in actions:
        assert link["href"] == expected_url
        assert " ".join(link["text"].split()) == action_label

    navigation = [link for link in page.links if "nav-link" in link["class"].split()]
    assert len(navigation) == 1
    assert navigation[0]["href"] == expected_url
    assert " ".join(navigation[0]["text"].split()) == nav_label

    # Nothing links to an insights route that does not exist yet.
    ids = {attributes["id"] for _, attributes in page.elements if "id" in attributes}
    for link in page.links:
        if link["href"].startswith("#"):
            assert link["href"][1:] in ids
        else:
            assert link["href"] in {"http://testserver/", expected_url}
    assert client.get(expected_url).status_code == 200

    assert "script-src 'self'; style-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]


def test_landing_preview_is_labelled_and_inert(client: TestClient) -> None:
    page = RenderedPage(client.get("/").text)

    assert "Contoso" in page.text
    figures = [attributes for tag, attributes in page.elements if tag == "figure"]
    assert len(figures) == 1
    assert any(
        tag == "figcaption" and attributes.get("id") == "lp-availability"
        for tag, attributes in page.elements
    )
    for talking_point in (
        "Which services could we sell next year that we cannot staff for today?",
        "What would an AI-assisted service look like to a customer?",
        "Which day-to-day work should stop being manual first?",
        "What proves the payback before the budget is committed?",
        "What has to be true for AI adoption to be defensible?",
    ):
        assert talking_point in page.text

    # The preview illustrates the product; it must not pose as a working generator.
    assert not {"form", "input", "button", "select", "textarea"} & {tag for tag, _ in page.elements}
    assert not any(
        attributes.get("role") in {"button", "tab", "textbox", "combobox"}
        for _, attributes in page.elements
    )
    assert all(
        attributes.get("aria-hidden") == "true" for tag, attributes in page.elements if tag == "svg"
    )
    assert not {"script", "style"} & {tag for tag, _ in page.elements}
    assert not any(
        name == "style" or name.startswith("on")
        for _, attributes in page.elements
        for name in attributes
    )


def test_login_page_keeps_the_shared_dark_chrome(client: TestClient) -> None:
    page = RenderedPage(client.get("/login").text)

    theme_colors = [
        attributes.get("content")
        for tag, attributes in page.elements
        if tag == "meta" and attributes.get("name") == "theme-color"
    ]
    assert theme_colors == ["#020617"]


def test_configured_login_keeps_its_provider_action(settings: Settings) -> None:
    configured_settings = settings.model_copy(
        update={"ms_client_id": "test-client", "ms_client_secret": SecretStr("test-secret")}
    )
    with TestClient(create_app(configured_settings)) as client:
        response = client.get("/login", params={"next": "/dashboard"})

    assert response.status_code == 200
    page = RenderedPage(response.text)
    assert "Continue with the Microsoft account connected to your workspace." in page.text
    provider_links = [link for link in page.links if "button--primary" in link["class"].split()]
    assert len(provider_links) == 1
    assert " ".join(provider_links[0]["text"].split()) == "Continue with Microsoft"
    provider_url = urlsplit(provider_links[0]["href"])
    assert provider_url.path == "/auth/microsoft/login"
    assert parse_qs(provider_url.query) == {"next": ["/dashboard"]}


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
