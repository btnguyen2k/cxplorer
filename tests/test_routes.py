"""Public and private route behavior tests."""

from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cxplorer import __version__
from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.main import create_app
from tests.conftest import TEST_CSRF_TOKEN


class RenderedPage(HTMLParser):
    """Collect copy, accessible labels, and links without counting URLs as page copy."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.copy: list[str] = []
        self.elements: list[tuple[str, dict[str, str | None]]] = []
        self.links: list[dict[str, str]] = []
        self.headings: list[dict[str, str]] = []
        self.form_controls: list[tuple[str, dict[str, str | None]]] = []
        self._active_link: dict[str, str] | None = None
        self._active_heading: dict[str, str] | None = None
        self._in_form = False
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
        if tag in {"h1", "h2", "h3"}:
            self._active_heading = {"tag": tag, "text": ""}
            self.headings.append(self._active_heading)
        if tag == "form":
            self._in_form = True
        if self._in_form and tag in {"input", "button", "select", "textarea"}:
            self.form_controls.append((tag, attributes))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._active_link = None
        if tag in {"h1", "h2", "h3"}:
            self._active_heading = None
        if tag == "form":
            self._in_form = False

    def handle_data(self, data: str) -> None:
        self.copy.append(data)
        if self._active_link is not None:
            self._active_link["text"] += data
        if self._active_heading is not None:
            self._active_heading["text"] += data

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
    assert "Sign in" in RenderedPage(login.text).text
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_application_uses_the_global_package_version(client: TestClient) -> None:
    response = client.get("/api/openapi.json")

    assert response.status_code == 200
    assert client.app.version == __version__
    assert response.json()["info"]["version"] == __version__


def test_footer_uses_the_configured_name_and_global_version(
    app_settings: AppSettings,
    identity_settings: IdentityVendorSettings,
) -> None:
    configured_name = "Contoso Seller Intelligence"
    configured_settings = app_settings.model_copy(update={"app_name": configured_name})
    with TestClient(create_app(configured_settings, identity_settings)) as client:
        page = RenderedPage(client.get("/").text)

    assert f"© {datetime.now(tz=UTC).year} {configured_name} · Version {__version__}" in page.text


def test_landing_page_focuses_on_business_outcomes(client: TestClient) -> None:
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


def test_landing_discloses_disabled_generation_and_illustrative_sample(
    client: TestClient,
) -> None:
    response = client.get("/")
    page = RenderedPage(response.text)

    assert "Insight generation needs operator-configured AI access." in page.text
    assert "An illustrative brief, not a generated company report." in page.text
    assert "Preview" not in page.text

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
        (True, "Open your workspace", "Workspace", "/dashboard"),
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


def test_landing_sample_is_labelled_and_inert(client: TestClient) -> None:
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

    # The illustrative sample must not pose as a working generator.
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


@pytest.mark.parametrize("path", ["/login", "/dashboard"])
def test_account_pages_inherit_the_landing_theme(client: TestClient, path: str) -> None:
    if path == "/dashboard":
        assert client.post("/_test/sign-in").status_code == 204

    pages = [RenderedPage(client.get(url).text) for url in ("/", path)]
    theme_colors = [
        [
            attributes.get("content")
            for tag, attributes in page.elements
            if tag == "meta" and attributes.get("name") == "theme-color"
        ]
        for page in pages
    ]
    assert theme_colors == [["#020617"], ["#020617"]]
    assert any(
        tag == "html" and "application-root" in (attributes.get("class") or "").split()
        for tag, attributes in pages[1].elements
    )


def test_application_theme_tokens_have_a_single_shared_definition(client: TestClient) -> None:
    stylesheet = client.get("/static/css/app.css")
    assert stylesheet.status_code == 200
    definitions = [
        line.split(":", 1)[0].strip()
        for line in stylesheet.text.splitlines()
        if line.lstrip().startswith(("--color-", "--radius-"))
    ]
    assert "--color-background" in definitions
    assert len(definitions) == len(set(definitions))


@pytest.mark.parametrize(
    ("next_path", "destination"),
    [
        (None, "/dashboard"),
        ("/dashboard", "/dashboard"),
        ("/dashboard?view=recent", "/dashboard?view=recent"),
        ("https://contoso.example", "/dashboard"),
        ("//contoso.example", "/dashboard"),
    ],
)
def test_configured_login_offers_only_the_enabled_provider(
    app_settings: AppSettings,
    identity_settings: IdentityVendorSettings,
    next_path: str | None,
    destination: str,
) -> None:
    configured_identity_settings = identity_settings.model_copy(
        update={"ms_client_id": "test-client", "ms_client_secret": SecretStr("test-secret")}
    )
    with TestClient(create_app(app_settings, configured_identity_settings)) as client:
        response = client.get("/login", params={"next": next_path} if next_path else {})

    assert response.status_code == 200
    page = RenderedPage(response.text)
    provider_links = [
        link for link in page.links if urlsplit(link["href"]).path.startswith("/auth/")
    ]
    assert len(provider_links) == 1
    assert " ".join(provider_links[0]["text"].split()) == "Continue with Microsoft Entra ID"
    provider_url = urlsplit(provider_links[0]["href"])
    assert provider_url.path == "/auth/microsoft/login"
    assert parse_qs(provider_url.query) == {"next": [destination]}
    assert not any(tag == "form" for tag, _ in page.elements)
    assert not any(
        attributes.get("type") in {"email", "password"} for _, attributes in page.elements
    )
    assert not any(
        word in heading["text"].casefold()
        for heading in page.headings
        for word in ("microsoft", "entra", "openid", "oauth")
    )


def test_unconfigured_login_has_no_working_provider_action(client: TestClient) -> None:
    page = RenderedPage(client.get("/login").text)

    assert not any(urlsplit(link["href"]).path.startswith("/auth/") for link in page.links)
    assert any(
        tag == "button" and "disabled" in attributes and attributes.get("type") == "button"
        for tag, attributes in page.elements
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("authentication_failed", "We couldn't complete your sign-in. Please try again."),
        (
            "invalid_identity",
            "Your account provider didn't return the details needed to sign in. "
            "Please try another account.",
        ),
        (
            "email_not_allowed",
            "This email address is not authorized to access CXplorer. "
            "Use an approved account or contact the administrator.",
        ),
        (
            "not_configured",
            "Sign-in is not available in this environment yet. Please contact the administrator.",
        ),
    ],
)
def test_login_errors_are_provider_neutral_and_accessible(
    client: TestClient, error: str, message: str
) -> None:
    response = client.get("/login", params={"error": error})
    page = RenderedPage(response.text)

    assert response.status_code == 200
    assert message in page.text
    assert any(attributes.get("role") == "alert" for _, attributes in page.elements)


def test_signed_in_login_offers_the_workspace_instead_of_signing_in_again(
    client: TestClient,
) -> None:
    assert client.post("/_test/sign-in").status_code == 204
    page = RenderedPage(client.get("/login").text)

    assert any(
        urlsplit(link["href"]).path == "/dashboard" and "workspace" in link["text"].casefold()
        for link in page.links
    )
    assert not any(urlsplit(link["href"]).path.startswith("/auth/") for link in page.links)


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


def test_workspace_has_six_accessible_draft_url_inputs(client: TestClient) -> None:
    assert client.post("/_test/sign-in").status_code == 204
    page = RenderedPage(client.get("/dashboard").text)
    url_inputs = [
        attributes
        for tag, attributes in page.elements
        if tag == "input" and attributes.get("type") == "url"
    ]
    assert len(url_inputs) == 6
    assert sum("required" in attributes for attributes in url_inputs) == 1

    ids = [attributes["id"] for _, attributes in page.elements if "id" in attributes]
    assert len(ids) == len(set(ids))
    labels = {attributes.get("for") for tag, attributes in page.elements if tag == "label"}
    source_controls = [
        attributes
        for tag, attributes in page.elements
        if tag == "select" or (tag == "input" and attributes.get("type") == "url")
    ]
    for attributes in source_controls:
        assert attributes.get("id") in labels
        assert "form" not in attributes
        for description_id in (attributes.get("aria-describedby") or "").split():
            assert description_id in ids

    for attributes in url_inputs:
        placeholder = attributes.get("placeholder") or ""
        assert urlsplit(placeholder).scheme == "https"
        assert urlsplit(placeholder).hostname == "contoso.example"
        assert not attributes.get("value")


def test_workspace_submission_and_logout_have_separate_csrf_protected_forms(
    client: TestClient,
) -> None:
    assert client.post("/_test/sign-in").status_code == 204
    page = RenderedPage(client.get("/dashboard").text)
    forms = [attributes for tag, attributes in page.elements if tag == "form"]

    assert len(forms) == 2
    assert all((form.get("method") or "").casefold() == "post" for form in forms)
    assert {urlsplit(form.get("action") or "").path for form in forms} == {
        "/auth/logout",
        "/insights",
    }
    assert sum(attributes.get("type") == "url" for _, attributes in page.form_controls) == 6
    csrf_fields = [
        attributes
        for tag, attributes in page.form_controls
        if tag == "input" and attributes.get("name") == "csrf_token"
    ]
    assert len(csrf_fields) == 2
    assert all(field.get("type") == "hidden" for field in csrf_fields)
    assert all(field.get("value") == TEST_CSRF_TOKEN for field in csrf_fields)
    generate_actions = [
        attributes
        for tag, attributes in page.elements
        if tag == "button"
        and attributes.get("type") == "submit"
        and "generation-status" in (attributes.get("aria-describedby") or "")
    ]
    assert len(generate_actions) == 1
    assert "disabled" in generate_actions[0]
    assert "draft" in page.text.casefold()
    assert "test-subject" not in page.text


def test_workspace_outlines_the_planned_sources_and_draft_limits(client: TestClient) -> None:
    assert client.post("/_test/sign-in").status_code == 204
    page = RenderedPage(client.get("/dashboard").text)

    for copy in (
        "One official company homepage is required.",
        "Three to six URLs recommended; six total maximum.",
        "About / Company / Who We Are",
        "Products / Services / Solutions",
        "public HTTPS HTML pages or text-based PDFs only",
        "No authenticated sources",
        "personal executive profiles",
        "paywalled databases",
        "High-value optional sources",
        "Investors & annual reports",
        "strategy presentation",
        "Newsroom & announcements",
        "Trust, security & compliance",
        "responsible AI",
        "Industry solutions & customer case studies",
        "Careers & engineering blogs",
        "lower-confidence signals",
        "not proof of installed technology",
        "Official announcements are always searched.",
        "last 90 days",
    ):
        assert copy in page.text
    assert "Signing out keeps both." in page.text
    assert not any(
        attributes.get("name") in {"news", "include_news", "web_search"}
        for _, attributes in page.form_controls
    )

    options = [attributes.get("value") for tag, attributes in page.elements if tag == "option"]
    for purpose in ("", "investors", "newsroom", "trust", "industry", "careers"):
        assert options.count(purpose) == 3


@pytest.mark.parametrize(
    ("path", "signed_in"), [("/login", False), ("/login", True), ("/dashboard", True)]
)
def test_account_pages_keep_semantic_markup_and_strict_csp(
    client: TestClient, path: str, signed_in: bool
) -> None:
    if signed_in:
        assert client.post("/_test/sign-in").status_code == 204
    response = client.get(path)
    page = RenderedPage(response.text)

    assert len([heading for heading in page.headings if heading["tag"] == "h1"]) == 1
    assert "style" not in {tag for tag, _ in page.elements}
    for tag, attributes in page.elements:
        if tag == "script":
            assert urlsplit(attributes.get("src") or "").path.startswith("/static/js/")
            assert attributes.get("type") == "module"
    assert not any(
        name == "style" or name.startswith("on")
        for _, attributes in page.elements
        for name in attributes
    )
    assert "script-src 'self'; style-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "draft preview" not in page.text.casefold()
    assert "non-production" not in page.text.casefold()


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
