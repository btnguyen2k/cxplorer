"""Isolated UI contracts; no application settings, private env files or live AI.

Run the optional, offline browser checks with CXPLORER_UI_BROWSER_TESTS=1.
They use an installed Playwright browser, route all traffic locally in-process,
and do not start a server or create screenshots.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from cxplorer import __version__
from cxplorer.insights.schemas import AcceptedReport

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "cxplorer" / "templates"
STATIC = ROOT / "src" / "cxplorer" / "static"
ORIGIN = "https://cxplorer.test"
REPORT_ID = "a" * 32
NAMESPACE = "1" * 64
PREFIX = f"cxplorer:{NAMESPACE}"
NOW = datetime.now(UTC).replace(microsecond=0)
GENERATED = NOW - timedelta(hours=1)
AUDIENCES = {"ceo": "CEO", "cto": "CTO", "cio": "CIO", "cfo": "CFO", "ciso": "CISO"}
FIELDS = {
    "homepage_url": "",
    "about_url": "",
    "products_url": "",
    "source_url_4": "",
    "source_url_5": "",
    "source_url_6": "",
    "source_purpose_4": "",
    "source_purpose_5": "",
    "source_purpose_6": "",
    "seller_context": "",
}


def claim(value="Contoso operates regional freight services.", basis="source_backed"):
    return {
        "text": None if basis == "unknown" else value,
        "basis": basis,
        "fact_ids": [] if basis == "unknown" else ["fact_1"],
    }


def report_fixture():
    company = {
        name: []
        for name in (
            "industries",
            "offerings",
            "customer_segments",
            "geographies",
            "recent_initiatives",
            "strategic_priorities",
            "financials",
            "technology",
            "leadership",
            "security",
        )
    }
    company.update(
        business_description=claim(),
        industries=[claim("Freight services")],
        technology=[claim(basis="unknown")],
    )
    opportunity = {
        "id": "opportunity_1",
        "priority": 1,
        "title": "Improve exception handling",
        "approach": "ai_assisted",
        "company_signal": claim(),
        "hypothesis": claim("Test assisted triage in a bounded workflow.", "hypothesis"),
        "workflow": "Route a service exception to a human reviewer.",
        "business_outcome": claim("Faster triage may improve service.", "analysis"),
        "prerequisites": {
            name: [f"Validate {name.replace('_', ' ')} in discovery."]
            for name in (
                "data",
                "integration",
                "business_owner",
                "adoption",
                "security",
                "governance",
            )
        },
        "success_criteria": [
            {
                "metric": "Time to triage",
                "baseline_fact_ids": [],
                "target": None,
                "measurement_plan": "Establish a baseline before a bounded pilot.",
            }
        ],
        "human_controls": ["A human approves each customer-facing action."],
        "non_ai_alternative": "Improve routing rules first.",
        "next_step": "Map the existing exception workflow.",
    }
    point = {
        "opportunity_id": opportunity["id"],
        "company_signal": claim(),
        "seller_line": claim("Could we explore service exception triage?", "hypothesis"),
        "audience_relevance": claim(
            "Operational consistency may support the business.", "analysis"
        ),
        "discovery_question": "Where do exceptions wait today?",
        "success_measure": "Agree the triage time baseline.",
        "next_step": "Meet the workflow owner.",
    }
    sources = [
        {
            "id": source_id,
            "original_url": url,
            "url": url,
            "title": title,
            "media_type": "text/html",
            "retrieved_at": GENERATED,
            "published_at": NOW.date() if news else None,
            "content_hash": "f" * 64,
            "purpose": "news" if news else "homepage",
            "is_news": news,
            "coverage_note": "Only public company statements were accepted.",
        }
        for source_id, url, title, news in (
            ("source_1", "https://contoso.com", "Contoso company", False),
            ("source_2", "https://contoso.com/news", "Contoso announcement", True),
        )
    ]
    return AcceptedReport.model_validate(
        {
            "report_id": REPORT_ID,
            "generated_at": GENERATED,
            "as_of_date": NOW.date(),
            "status": "completed",
            "requested_audiences": list(AUDIENCES),
            "audience_coverage": [{"audience": role, "status": "complete"} for role in AUDIENCES],
            "company_name": claim("Contoso"),
            "company": company,
            "executive_summary": [claim(), claim("Explore service consistency.", "analysis")],
            "opportunities": [opportunity],
            "audiences": [
                {
                    "audience": role,
                    "coverage": "complete",
                    "opening": claim(f"Explore the {label} perspective.", "hypothesis"),
                    "talk_points": [point],
                    "discovery_questions": ["How do you define success?"],
                    "objections": [
                        {
                            "objection": "How do we establish value?",
                            "response": claim("Start with the baseline.", "hypothesis"),
                        }
                    ],
                    "next_step_ask": "Agree a discovery session.",
                    "evidence_gaps": [
                        {"area": "Baseline", "detail": "A baseline is not published."}
                    ],
                }
                for role, label in AUDIENCES.items()
            ],
            "evidence_gaps": [{"area": "Technology", "detail": "Installed systems are unknown."}],
            "contradictions": [
                {
                    "fact_ids": ["fact_1", "fact_2"],
                    "subject": "Service scope",
                    "explanation": "The two pages describe different service scopes; clarify in discovery.",
                }
            ],
            "facts": [
                {
                    "id": fact_id,
                    "category": "company",
                    "entity": "Contoso",
                    "statement": statement,
                    "attribution": "company_reported",
                    "quantities": [],
                    "time_context": "Current company page",
                    "evidence": [
                        {
                            "span_id": f"span_{index}",
                            "quote": statement,
                            "source_id": "source_1",
                            "page": None,
                            "section": "Company overview",
                        }
                    ],
                }
                for index, (fact_id, statement) in enumerate(
                    (
                        ("fact_1", "Contoso operates regional freight services."),
                        ("fact_2", "Contoso describes a wider service footprint."),
                    ),
                    start=1,
                )
            ],
            "sources": sources,
            "source_outcomes": [
                {
                    "url": "https://contoso.com/blocked",
                    "status": "rejected",
                    "reason": "The page was not accessible.",
                }
            ],
            "announcements": [
                {
                    "source_id": "source_2",
                    "title": "Contoso leadership announcement",
                    "published_at": NOW.date(),
                    "executive_name": "Contoso executive",
                    "executive_role": "CEO",
                }
            ],
            "news_status": "found",
        }
    )


def context_fixture(**overrides):
    report = report_fixture()
    result = {
        "app_name": "CXplorer",
        "app_version": __version__,
        "current_year": NOW.year,
        "user": SimpleNamespace(display_name="Contoso seller", email="seller@contoso.example"),
        "csrf_token": "ui-fixture-csrf",
        "cache_namespace": NAMESPACE,
        "ai_enabled": True,
        "form_values": dict(FIELDS),
        "selected_audiences": list(AUDIENCES),
        "audience_labels": AUDIENCES,
        "submission_id": "ui-fixture-submission",
        "errors": [],
        "cached_list_url": f"{ORIGIN}/api/private/insights/cached-list",
        "report_id": REPORT_ID,
        "status_url": f"{ORIGIN}/api/private/insights/{REPORT_ID}/status",
        "cache_payload_url": f"{ORIGIN}/api/private/insights/{REPORT_ID}/cache",
        "download_url": f"{ORIGIN}/insights/{REPORT_ID}/download",
        "restore_url": f"{ORIGIN}/insights/restore",
        "invalid_cache": False,
        "error": None,
        "can_retry": False,
        "cached_entries": [],
        "job": SimpleNamespace(
            id=REPORT_ID,
            state="running",
            stage="verify_sources",
            message="Verifying the company sources.",
            error=None,
            source_outcomes=[],
        ),
        "report": report,
        "opportunities_by_id": {item.id: item for item in report.opportunities},
        "facts_by_id": {item.id: item for item in report.facts},
        "sources_by_id": {item.id: item for item in report.sources},
    }
    result.update(overrides)
    return result


def render(template, **overrides):
    """Render via an isolated FastAPI/Jinja app, not the concurrently changing backend."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,
    )
    templates = Jinja2Templates(env=env)
    app = FastAPI()
    context = context_fixture(**overrides)

    async def page(request: Request):
        return templates.TemplateResponse(request=request, name=template, context=context)

    routes = {
        "landing_page": "/",
        "dashboard": "/dashboard",
        "login_page": "/login",
        "logout": "/auth/logout",
        "create_insight": "/insights",
        "restore_insight": "/insights/restore",
        "insight_report": "/insights/{report_id}",
        "insight_status": "/api/private/insights/{report_id}/status",
        "cancel_insight": "/insights/{report_id}/cancel",
        "retry_insight": "/insights/{report_id}/retry",
        "download_insight": "/insights/{report_id}/download",
        "cached_report_list": "/api/private/insights/cached-list",
    }
    for name, path in routes.items():
        app.add_api_route(path, page, name=name, response_class=HTMLResponse)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    path = {
        "dashboard.html": "/dashboard",
        "landing.html": "/",
        "insights_progress.html": f"/insights/{REPORT_ID}",
        "insights_report.html": f"/insights/{REPORT_ID}",
        "insights_restore.html": f"/insights/{REPORT_ID}",
        "_saved_reports.html": "/api/private/insights/cached-list",
    }[template]
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get(path)
    assert response.status_code == 200
    return response.text


class Tags(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def find(self, tag=None, **attrs):
        return [
            attributes
            for name, attributes in self.tags
            if (tag is None or tag == name)
            and all(attributes.get(key) == value for key, value in attrs.items())
        ]


@pytest.mark.parametrize(
    "template",
    [
        "dashboard.html",
        "insights_progress.html",
        "insights_report.html",
        "insights_restore.html",
    ],
)
def test_private_templates_keep_theme_semantics_and_csp(template):
    html = render(template)
    tags = Tags(html)
    assert len(tags.find("h1")) == 1
    assert len(tags.find("main")) == 1
    assert tags.find("html")[0]["class"] == "application-root"
    assert tags.find("meta", name="theme-color")[0]["content"] == "#020617"
    assert tags.find("body")[0]["class"] == "application-page"
    for name, attrs in tags.tags:
        assert "style" not in attrs
        assert not any(key.startswith("on") for key in attrs)
        if name == "script":
            assert attrs["src"].startswith(f"{ORIGIN}/static/js/")
            assert attrs["type"] == "module"
    ids = [attrs["id"] for _, attrs in tags.tags if "id" in attrs]
    assert len(ids) == len(set(ids))
    for _, attrs in tags.tags:
        for target in attrs.get("aria-describedby", "").split():
            assert target in ids
        for target in attrs.get("aria-labelledby", "").split():
            assert target in ids
        if attrs.get("href", "").startswith("#"):
            assert attrs["href"][1:] in ids


@pytest.mark.parametrize("enabled", [True, False])
def test_dashboard_contract_and_operator_availability(enabled):
    html = render("dashboard.html", ai_enabled=enabled)
    tags = Tags(html)
    assert len(tags.find("input", type="url")) == 6
    assert tags.find("input", id="company-homepage")[0]["name"] == "homepage_url"
    assert "required" in tags.find("input", id="company-homepage")[0]
    assert tags.find("input", id="company-about")[0]["name"] == "about_url"
    assert tags.find("input", id="company-products")[0]["name"] == "products_url"
    assert [item["value"] for item in tags.find("input", type="checkbox")] == list(AUDIENCES)
    assert all("checked" in item for item in tags.find("input", type="checkbox"))
    buttons = [
        attrs
        for attrs in tags.find("button", type="submit")
        if attrs.get("aria-describedby") == "generation-status verification-note"
    ]
    assert ("disabled" not in buttons[0]) is enabled
    assert tags.find("form", id="insight-form")[0]["action"] == f"{ORIGIN}/insights"
    assert tags.find("input", name="csrf_token")
    assert tags.find("input", name="submission_id")
    assert "Official announcements are always searched" in html
    assert "last 90 days" in html
    assert "up to three" in html
    assert "generation is not available yet" not in html.lower()
    assert 'data-cache-prefix="cxplorer:' + NAMESPACE + '"' in html
    assert "Your browser workspace" not in html
    assert html.index("Choose your company sources") < html.index("Saved in this browser")
    assert html.index("Saved in this browser") < html.index("Browser storage")
    assert len([item for item in tags.find("p") if "data-draft-status" in item]) == 1
    assert len([item for item in tags.find("button") if "data-draft-retry" in item]) == 1
    if not enabled:
        assert "Ask the operator to configure AI" in html
        assert "edit and save your browser draft" in html


def test_server_errors_keep_submitted_values_escaped():
    values = dict(FIELDS, homepage_url='https://contoso.com/?q="><script>alert(1)</script>')
    html = render(
        "dashboard.html", form_values=values, selected_audiences=[], errors=["Choose an audience."]
    )
    tags = Tags(html)
    assert tags.find("form", id="insight-form")[0]["data-has-errors"] == "true"
    assert tags.find("input", id="company-homepage")[0]["value"] == values["homepage_url"]
    assert all("checked" not in item for item in tags.find("input", type="checkbox"))
    assert len(tags.find("script")) == 1
    assert "A saved draft will not replace them." in html


def test_report_renders_typed_evidence_and_shared_role_contracts():
    html = render("insights_report.html")
    tags = Tags(html)
    for role in AUDIENCES:
        assert tags.find("section", id=f"audience-{role}")
    for term in (
        "Analysis · interpretation",
        "Hypothesis · validate in discovery",
        "Unknown · not established",
        "Company-reported fact",
        "Shared prerequisites",
        "Human controls",
        "Success criteria",
        "Proposed target, not a forecast",
        "Sources &amp; checks",
        "The page was not accessible.",
        "Company overview",
        "Contoso leadership announcement",
        "Conflicting evidence",
    ):
        assert term in html
    assert "Source-backed fact" not in html
    assert tags.find("span", **{"class": "claim-meta"})
    assert tags.find("sup", **{"class": "source-citation-marker"})
    citations = tags.find("a", **{"class": "source-citation"})
    assert citations
    assert all(citation["target"] == "_blank" for citation in citations)
    assert {citation["href"] for citation in citations} == {
        "https://contoso.com/",
        "https://contoso.com/news",
    }
    assert ">[1]</a>" in html
    assert ">[2]</a>" in html
    title_start = html.index('id="report-title"')
    title_end = html.index("</h1>", title_start)
    assert "source-citation" not in html[title_start:title_end]
    assert (
        'Contoso operates regional freight services.&#8288;<span class="claim-meta"><sup '
        'class="source-citation-marker">'
    ) in html
    assert "Know what is established" not in html
    assert "The server keeps this report only temporarily" not in html
    assert html.index('id="report-cache-title"') < html.index('id="executive-summary"')
    assert "Saving a temporary browser copy." in html
    assert len(tags.find("a", href="#opportunity-opportunity_1")) == 5
    external_sources = [item["href"] for item in tags.find("a") if item.get("target") == "_blank"]
    assert set(external_sources) == {"https://contoso.com/", "https://contoso.com/news"}
    assert "https://contoso.com/blocked" not in external_sources


def test_claim_citations_are_numbered_by_source_order_without_spaces():
    report = report_fixture().model_copy(deep=True)
    second_fact = report.facts[1]
    second_citation = second_fact.evidence[0].model_copy(update={"source_id": "source_2"})
    report.facts[1] = second_fact.model_copy(update={"evidence": [second_citation]})
    report.executive_summary[0] = report.executive_summary[0].model_copy(
        update={
            "text": report.executive_summary[0].text + " ",
            "fact_ids": ["fact_1", "fact_2"],
        }
    )
    html = render(
        "insights_report.html",
        report=report,
        facts_by_id={item.id: item for item in report.facts},
    )
    assert ">[1]</a></sup><sup" in html
    assert ">[2]</a>" in html
    assert "services. &#8288;" not in html


def test_partial_and_empty_report_states_are_explicit():
    report = report_fixture().model_copy(deep=True)
    report.status = "partial"
    report.audience_coverage[-1].status = "unavailable"
    report.audiences.pop()
    report.announcements = []
    report.news_status = "none_found"
    report.opportunities = []
    html = render("insights_report.html", report=report, opportunities_by_id={})
    assert "Partial report — coverage needs attention" not in html
    assert "Report coverage" in html
    assert "This requested audience was not delivered" in html
    assert "No qualifying announcements found" in html
    assert "No accepted opportunities" in html
    assert "not proof that none exist" in html


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("preflight", "10"),
        ("verify_sources", "20"),
        ("generate_talk_points", "84"),
        ("repair", "94"),
        ("review_report", "94"),
    ],
)
def test_running_progress_renders_an_accessible_stage_estimate(stage, expected):
    job = SimpleNamespace(
        id=REPORT_ID,
        state="running",
        stage=stage,
        message="Building the Contoso brief.",
        error=None,
        source_outcomes=[],
    )
    html = render("insights_progress.html", job=job)
    meters = Tags(html).find("div", role="progressbar")

    assert len(meters) == 1
    meter = meters[0]
    assert meter["aria-labelledby"] == "generation-progress-label"
    assert meter["aria-describedby"] == "progress-stage generation-progress-note"
    assert meter["aria-valuemin"] == "0"
    assert meter["aria-valuemax"] == "100"
    assert meter["aria-valuenow"] == expected
    assert meter["data-progress-level"] == expected
    assert meter["data-progress-active"] == "true"
    assert f"About {expected} percent." in meter["aria-valuetext"]
    assert f"About {expected}%" in html


@pytest.mark.parametrize(
    ("state", "stage", "visible_value"),
    [
        ("queued", "queued", "Waiting"),
        ("running", "new_server_stage", "Working"),
    ],
)
def test_progress_uses_indeterminate_semantics_when_no_estimate_exists(state, stage, visible_value):
    job = SimpleNamespace(
        id=REPORT_ID,
        state=state,
        stage=stage,
        message="Preparing the Contoso brief.",
        error=None,
        source_outcomes=[],
    )
    html = render("insights_progress.html", job=job)
    meter = Tags(html).find("div", role="progressbar")[0]

    assert "aria-valuenow" not in meter
    assert meter["data-progress-level"] == "indeterminate"
    assert meter["data-progress-active"] == "true"
    assert visible_value in html


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_progress_has_no_automatic_generation_form(state):
    job = SimpleNamespace(
        id=REPORT_ID,
        state=state,
        stage=state,
        message="Generation stopped.",
        error="Source verification could not finish." if state == "failed" else None,
        source_outcomes=[],
    )
    html = render("insights_progress.html", job=job)
    assert "No new generation will start automatically" in html
    assert not Tags(html).find("form")
    assert f'data-job-state="{state}"' in html


@pytest.mark.parametrize("can_retry", [False, True])
def test_progress_accepts_minimal_job_context(can_retry):
    job = SimpleNamespace(
        id=REPORT_ID,
        state="failed",
        stage="failed",
        message="Generation stopped.",
        error="The sources could not be verified.",
    )
    html = render("insights_progress.html", job=job, can_retry=can_retry)
    forms = Tags(html).find("form")
    assert "progress-sources-title" not in html
    if can_retry:
        assert len(forms) == 1
        assert forms[0]["method"] == "post"
        assert forms[0]["action"] == f"{ORIGIN}/insights/{REPORT_ID}/retry"
    else:
        assert not forms


def test_restore_rejection_and_saved_metadata_are_escaped():
    html = render(
        "insights_restore.html", invalid_cache=True, error="Signature could not be verified."
    )
    tags = Tags(html)
    assert 'data-invalid-cache="true"' in html
    assert tags.find("form")[0]["method"] == "post"
    assert {item["name"] for item in tags.find("input")} == {
        "csrf_token",
        "report_id",
        "cache_blob",
    }
    assert tags.find("input", name="cache_blob")[0]["value"] == ""
    entry = SimpleNamespace(
        report_id=REPORT_ID,
        company_name='Contoso <script>alert("x")</script>',
        generated_at=GENERATED,
        expires_at=GENERATED + timedelta(days=7),
    )
    fragment = render("_saved_reports.html", cached_entries=[entry])
    assert not Tags(fragment).find("script")
    assert "&lt;script&gt;" in fragment
    assert Tags(fragment).find("a")[0]["href"] == f"{ORIGIN}/insights/{REPORT_ID}"


@pytest.mark.parametrize("enabled", [True, False])
def test_landing_sample_uses_runtime_availability(enabled):
    html = render("landing.html", ai_enabled=enabled)
    assert "An illustrative brief, not a generated company report." in html
    assert ">Preview<" not in html
    assert ("Generate source-backed insights from your workspace" in html) is enabled
    assert ("operator-configured AI access" in html) is not enabled


def test_browser_assets_avoid_unsafe_dom_and_storage_apis():
    all_js = "\n".join(path.read_text(encoding="utf-8") for path in (STATIC / "js").glob("*.js"))
    for forbidden in (
        "localStorage.clear(",
        "sessionStorage",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write(",
        "DecompressionStream",
        "atob(",
        "eval(",
        ":v1:",
    ):
        assert forbidden not in all_js


@pytest.fixture
def browser():
    if os.environ.get("CXPLORER_UI_BROWSER_TESTS") != "1":
        pytest.skip("Set CXPLORER_UI_BROWSER_TESTS=1 for optional offline Playwright checks.")
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as manager:
        candidates = sorted(
            (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright").glob(
                "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"
            )
        )
        kwargs = {"executable_path": str(candidates[-1])} if candidates else {}
        instance = manager.chromium.launch(**kwargs)
        try:
            yield instance
        finally:
            instance.close()


def cache_record(report_id=REPORT_ID, **overrides):
    record = {
        "report_id": report_id,
        "company_name": "Contoso",
        "generated_at": GENERATED.isoformat(),
        "expires_at": (GENERATED + timedelta(days=7)).isoformat(),
        "input_fingerprint": "f" * 64,
        "blob": "opaque-signed-gzip-fixture",
    }
    record.update(overrides)
    return record


def draft_record(**values):
    return {
        "fields": dict(FIELDS, **values),
        "audiences": ["ceo", "ciso"],
        "updated_at": GENERATED.isoformat(),
        "expires_at": (GENERATED + timedelta(days=7)).isoformat(),
    }


def serve(page, template="dashboard.html", *, overrides=None, api=None):
    """All browser requests are fulfilled offline; nothing is sent to a third party."""
    html = render(template, **(overrides or {}))
    errors = []
    requests = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def handle(route):
        request = route.request
        path = urlsplit(request.url).path
        requests.append((path, request.method, request.post_data))
        if urlsplit(request.url).netloc != "cxplorer.test":
            route.abort()
            return
        if api and path in api:
            api[path](route)
        elif path.startswith("/static/"):
            asset = STATIC / path.removeprefix("/static/")
            route.fulfill(
                body=asset.read_text(encoding="utf-8"),
                content_type="text/css" if path.endswith(".css") else "text/javascript",
            )
        elif path == "/api/private/insights/cached-list":
            entries = [
                SimpleNamespace(
                    **dict(
                        entry,
                        generated_at=datetime.fromisoformat(entry["generated_at"]),
                        expires_at=datetime.fromisoformat(entry["expires_at"]),
                    )
                )
                for entry in json.loads(request.post_data)["entries"]
            ]
            route.fulfill(
                body=render("_saved_reports.html", cached_entries=entries),
                content_type="text/html",
            )
        elif path == f"/api/private/insights/{REPORT_ID}/cache":
            route.fulfill(json=cache_record())
        else:
            route.fulfill(
                body=html,
                content_type="text/html",
                headers={
                    "Content-Security-Policy": (
                        "default-src 'self'; base-uri 'self'; form-action 'self'; "
                        "frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; "
                        "script-src 'self'; style-src 'self'"
                    ),
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    page.route("**/*", handle)
    return errors, requests


def seed_storage(page, entries):
    # A test-only browser initializer, not an inline script in any delivered page.
    page.add_init_script(
        "for (const [key, value] of Object.entries(" + json.dumps(entries) + ")) {"
        "if (localStorage.getItem(key) === null) localStorage.setItem(key, value); }"
    )


def wait_for_js(page, expression, timeout=5000):
    """Poll through the test driver without page-side eval under the strict CSP."""
    from playwright.sync_api import Error

    deadline = monotonic() + timeout / 1000
    while monotonic() < deadline:
        try:
            if page.evaluate(
                "() => { try { return Boolean(" + expression + "); } catch { return false; } }"
            ):
                return
        except Error as error:
            if "Execution context was destroyed" not in str(error):
                raise
        page.wait_for_timeout(50)
    raise AssertionError(f"Browser condition did not become true: {expression}")


def held_route(page, pending, timeout=5000):
    """Wait for a deliberately held request, rather than guessing network timing."""
    deadline = monotonic() + timeout / 1000
    while not pending and monotonic() < deadline:
        page.wait_for_timeout(10)
    assert pending, "The expected offline request did not arrive."
    return pending.pop(0)


def hold_draft_debounce(page):
    """Hold the 250 ms debounce without freezing animation or navigation timers."""
    page.clock.set_fixed_time(NOW + timedelta(seconds=10))
    page.evaluate("""() => {
        const schedule = window.setTimeout.bind(window);
        window.setTimeout = (callback, delay, ...args) => {
            if (delay === 250) return 0;
            return schedule(callback, delay, ...args);
        };
    }""")


def hold_native_logout(page):
    """Inspect the flush at the native-POST boundary, before navigation replaces the document."""
    page.evaluate("""() => {
        const submit = HTMLFormElement.prototype.submit;
        window.fixtureNativePosts = 0;
        HTMLFormElement.prototype.submit = function() {
            window.fixtureNativePosts += 1;
            window.fixtureNativeFormIsLogout = this.matches('[data-sign-out]');
            window.releaseFixtureNativePost = () => submit.call(this);
        };
    }""")


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize(
    "template",
    [
        "dashboard.html",
        "insights_progress.html",
        "insights_report.html",
        "insights_restore.html",
    ],
)
def test_browser_mobile_and_desktop_layout(browser, width, template):
    context = browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
    try:
        page = context.new_page()
        errors, _ = serve(page, template)
        path = "/dashboard" if template == "dashboard.html" else f"/insights/{REPORT_ID}"
        page.goto(ORIGIN + path)
        page.wait_for_timeout(350)
        assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
        assert (
            page.locator("body").evaluate("(el) => getComputedStyle(el).backgroundColor")
            == "rgb(2, 6, 23)"
        )
        assert page.locator("html").evaluate("(el) => getComputedStyle(el).minWidth") == "0px"
        assert (
            page.locator("html").evaluate("(el) => getComputedStyle(el).scrollBehavior") == "auto"
        )
        page.keyboard.press("Tab")
        assert page.locator(":focus").count() == 1
        assert (
            page.locator(":focus").evaluate("(el) => getComputedStyle(el).outlineStyle") != "none"
        )
        if template == "insights_report.html":
            page.emulate_media(media="print")
            assert page.locator(".cache-panel").is_hidden()
            assert page.locator("#evidence-facts").is_visible()
            assert page.locator("#opportunities").is_visible()
        assert not errors
    finally:
        context.close()


def test_browser_citation_stays_with_final_word_at_wrap_boundary(browser):
    report = report_fixture().model_copy(deep=True)
    report.executive_summary[0] = report.executive_summary[0].model_copy(
        update={"text": report.executive_summary[0].text + " "}
    )
    context = browser.new_context(viewport={"width": 333, "height": 900})
    try:
        page = context.new_page()
        errors, _ = serve(
            page,
            "insights_report.html",
            overrides={
                "report": report,
                "facts_by_id": {item.id: item for item in report.facts},
            },
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        positions = page.locator(".grounded-text p").first.evaluate("""element => {
            const text = element.firstChild;
            const joiner = text.data.indexOf("\u2060");
            const end = joiner >= 0 ? joiner : text.data.trimEnd().length;
            const range = document.createRange();
            range.setStart(text, text.data.lastIndexOf(" ", end - 1) + 1);
            range.setEnd(text, end);
            const word = range.getBoundingClientRect();
            const citation = element.querySelector(".source-citation-marker").getBoundingClientRect();
            return {wordTop: word.top, citationTop: citation.top};
        }""")
        assert abs(positions["wordTop"] - positions["citationTop"]) < 10
        assert not errors
    finally:
        context.close()


def test_browser_draft_whitelist_restore_errors_and_expiry(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        existing = draft_record(
            homepage_url="https://contoso.com", source_url_4="https://contoso.com/news"
        )
        key = f"{PREFIX}:draft"
        seed_storage(page, {key: json.dumps(existing)})
        errors, _ = serve(page, overrides={"ai_enabled": False})
        page.goto(ORIGIN + "/dashboard")
        wait_for_js(
            page, "document.querySelector('[data-draft-status]').textContent.includes('restored')"
        )
        assert page.locator("#company-homepage").input_value() == existing["fields"]["homepage_url"]
        assert page.locator("[data-optional-sources]").get_attribute("open") is not None
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == existing
        assert page.get_by_role("button", name="Generate insights").is_disabled()
        page.locator("#company-about").fill("https://contoso.com/about")
        wait_for_js(
            page,
            "document.querySelector('[data-draft-status]').textContent.includes('Draft saved')",
        )
        saved = json.loads(page.evaluate("(key) => localStorage.getItem(key)", key))
        assert set(saved) == {"fields", "audiences", "updated_at", "expires_at"}
        assert set(saved["fields"]) == set(FIELDS)
        assert saved["audiences"] == ["ceo", "ciso"]
        assert "csrf" not in json.dumps(saved)
        assert "seller@contoso.example" not in json.dumps(saved)
        assert datetime.fromisoformat(saved["expires_at"]) - datetime.fromisoformat(
            saved["updated_at"]
        ) == timedelta(days=7)
        page.locator("#company-about").blur()
        page.wait_for_timeout(300)
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == saved
        page.unroute("**/*")
        serve(
            page,
            overrides={
                "form_values": dict(FIELDS, homepage_url="https://contoso.com/submitted"),
                "errors": ["Check these submitted values."],
                "selected_audiences": ["cto"],
            },
        )
        page.goto(ORIGIN + "/dashboard")
        wait_for_js(
            page, "document.querySelector('[data-draft-status]').textContent.includes('submitted')"
        )
        assert page.locator("#company-homepage").input_value() == "https://contoso.com/submitted"
        assert page.locator("#audience-cto").is_checked()
        assert not page.locator("#audience-ceo").is_checked()
        assert not errors
    finally:
        context.close()


def test_browser_report_save_preserves_dates_and_network_failures(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        key = f"{PREFIX}:report:{REPORT_ID}"
        expected = cache_record()
        errors, _ = serve(page, "insights_report.html")
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        wait_for_js(
            page,
            "document.querySelector('[data-report-save-status]').textContent.includes('Saved in this browser')",
        )
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == expected
        page.reload()
        wait_for_js(
            page,
            "document.querySelector('[data-report-save-status]').textContent.includes('Saved in this browser')",
        )
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == expected
        page.unroute("**/*")
        serve(
            page,
            "insights_report.html",
            api={
                f"/api/private/insights/{REPORT_ID}/cache": lambda route: route.fulfill(
                    status=401, json={"detail": "Sign in again."}
                )
            },
        )
        page.reload()
        wait_for_js(
            page,
            "document.querySelector('[data-report-save-status]').textContent.includes('paused')",
        )
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == expected
        assert page.locator("[data-session-link]").is_visible()
        assert not errors
    finally:
        context.close()


def test_browser_restore_posts_only_opaque_blob_and_never_deletes_on_401(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        key = f"{PREFIX}:report:{REPORT_ID}"
        record = cache_record()
        seed_storage(page, {key: json.dumps(record)})
        errors, requests = serve(
            page,
            "insights_restore.html",
            api={
                "/insights/restore": lambda route: route.fulfill(
                    status=401, body="Sign in again.", content_type="text/plain"
                )
            },
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        page.wait_for_url(f"{ORIGIN}/insights/restore")
        posts = [
            parse_qs(body)
            for path, method, body in requests
            if path == "/insights/restore" and method == "POST"
        ]
        assert len(posts) == 1
        assert posts[0] == {
            "csrf_token": ["ui-fixture-csrf"],
            "report_id": [REPORT_ID],
            "cache_blob": [record["blob"]],
        }
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == record
        assert record["blob"] not in page.url
        assert not errors
    finally:
        context.close()


def test_browser_definite_rejection_removes_only_exact_entry_without_loop(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        own_key = f"{PREFIX}:report:{REPORT_ID}"
        other_key = f"{PREFIX}:report:{'b' * 32}"
        draft_key = f"{PREFIX}:draft"
        seed_storage(
            page,
            {
                own_key: json.dumps(cache_record()),
                other_key: json.dumps(cache_record("b" * 32)),
                draft_key: json.dumps(draft_record()),
                "unrelated": "keep me",
            },
        )
        errors, requests = serve(
            page,
            "insights_restore.html",
            overrides={
                "invalid_cache": True,
                "error": "The signed copy could not be verified.",
            },
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        wait_for_js(
            page, "document.querySelector('[data-cache-notice]').textContent.includes('removed')"
        )
        assert page.evaluate("(key) => localStorage.getItem(key)", own_key) is None
        assert page.evaluate("(key) => localStorage.getItem(key)", other_key)
        assert page.evaluate("(key) => localStorage.getItem(key)", draft_key)
        assert page.evaluate("localStorage.getItem('unrelated')") == "keep me"
        assert not [item for item in requests if item[0] == "/insights/restore"]
        assert not errors
    finally:
        context.close()


def test_browser_cache_budget_expiry_clear_and_account_isolation(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        other_prefix = "cxplorer:" + "2" * 64
        expired = cache_record(
            "b" * 32,
            generated_at=(NOW - timedelta(days=8)).isoformat(),
            expires_at=(NOW - timedelta(days=1)).isoformat(),
        )
        expired_key = f"{PREFIX}:report:{'b' * 32}"
        other_key = f"{other_prefix}:report:{'c' * 32}"
        seed_storage(
            page,
            {
                expired_key: json.dumps(expired),
                f"{PREFIX}:report:{'d' * 32}": "{unreadable",
                other_key: json.dumps(cache_record("c" * 32)),
                "cxplorer:reserved-capacity": "x" * 1_046_000,
                "unrelated": "keep me",
            },
        )
        errors, _ = serve(
            page,
            "insights_report.html",
            api={
                f"/api/private/insights/{REPORT_ID}/cache": lambda route: route.fulfill(
                    json=cache_record(blob="x" * 5000)
                )
            },
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        wait_for_js(
            page, "document.querySelector('[data-cache-warning]').textContent.includes('2 MiB')"
        )
        assert page.evaluate("(key) => localStorage.getItem(key)", expired_key) is None
        assert (
            page.evaluate("(key) => localStorage.getItem(key)", f"{PREFIX}:report:{REPORT_ID}")
            is None
        )
        assert page.evaluate("(key) => localStorage.getItem(key)", other_key)
        total = page.evaluate("""() => Object.keys(localStorage).filter(k => k.startsWith('cxplorer:'))
            .reduce((sum, k) => sum + 2 * (k.length + localStorage.getItem(k).length), 0)""")
        assert total <= 2 * 1024 * 1024
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator(".cache-tools summary").click()
        page.get_by_role("button", name="Clear my browser cache").click()
        wait_for_js(
            page, "document.querySelector('[data-cache-notice]').textContent.includes('Cleared')"
        )
        assert page.evaluate("(key) => localStorage.getItem(key)", other_key)
        assert page.evaluate("localStorage.getItem('unrelated')") == "keep me"
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("failure", ["network", "404", "413", "500", "wrong_id", "bad_date"])
def test_browser_failed_cache_transport_never_discards_existing_copy(browser, failure):
    context = browser.new_context()
    try:
        page = context.new_page()
        key = f"{PREFIX}:report:{REPORT_ID}"
        original = cache_record()
        seed_storage(page, {key: json.dumps(original)})

        def fail(route):
            if failure == "network":
                route.abort("failed")
            elif failure == "wrong_id":
                route.fulfill(json=cache_record("b" * 32))
            elif failure == "bad_date":
                route.fulfill(json=cache_record(expires_at="2026-02-30T12:00:00+00:00"))
            else:
                route.fulfill(status=int(failure), json={"detail": "Not available."})

        errors, _ = serve(
            page,
            "insights_report.html",
            api={f"/api/private/insights/{REPORT_ID}/cache": fail},
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        wait_for_js(
            page,
            "document.querySelector('[data-report-save-status]').textContent.includes('not confirmed')",
        )
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == original
        assert page.locator("[data-cache-warning]").is_visible()
        assert page.get_by_role("button", name="Retry saving report").is_enabled()
        assert page.get_by_role("link", name="Download JSON").is_visible()
        assert not errors
    finally:
        context.close()


def test_browser_history_is_server_rendered_and_metadata_is_bounded(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        malicious_label = "Contoso <img src=x onerror=window.injected=true>"
        own_key = f"{PREFIX}:report:{REPORT_ID}"
        other_key = f"cxplorer:{'2' * 64}:report:{'b' * 32}"
        seed_storage(
            page,
            {
                own_key: json.dumps(cache_record(company_name=malicious_label)),
                other_key: json.dumps(cache_record("b" * 32)),
                f"{PREFIX}:report:{'c' * 32}": json.dumps(
                    cache_record("c" * 32, company_name="C" * 241)
                ),
            },
        )
        errors, requests = serve(page)
        page.goto(ORIGIN + "/dashboard")
        page.wait_for_selector("[data-saved-report-fragment]")
        assert page.locator(".saved-report-card h3").inner_text() == malicious_label
        assert page.locator("[data-saved-list] img").count() == 0
        assert page.evaluate("window.injected === undefined")
        posted = [
            json.loads(body)
            for path, method, body in requests
            if path == "/api/private/insights/cached-list" and method == "POST"
        ]
        assert len(posted[0]["entries"]) == 1
        assert set(posted[0]["entries"][0]) == {
            "report_id",
            "company_name",
            "generated_at",
            "expires_at",
        }
        assert posted[0]["entries"][0]["report_id"] == REPORT_ID
        page.on("dialog", lambda dialog: dialog.accept())
        page.locator("[data-delete-saved]").click()
        wait_for_js(
            page, "document.querySelector('[data-cache-notice]').textContent.includes('deleted')"
        )
        assert page.evaluate("(key) => localStorage.getItem(key)", own_key) is None
        assert page.evaluate("(key) => localStorage.getItem(key)", other_key)
        assert not errors
    finally:
        context.close()


def test_browser_expired_draft_is_removed_but_future_dated_report_is_kept(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        draft = draft_record(homepage_url="https://contoso.com/expired")
        draft["updated_at"] = (NOW - timedelta(days=8)).isoformat()
        draft["expires_at"] = (NOW - timedelta(days=1)).isoformat()
        future = cache_record(
            generated_at=(NOW + timedelta(hours=2)).isoformat(),
            expires_at=(NOW + timedelta(days=7, hours=2)).isoformat(),
        )
        draft_key = f"{PREFIX}:draft"
        report_key = f"{PREFIX}:report:{REPORT_ID}"
        seed_storage(page, {draft_key: json.dumps(draft), report_key: json.dumps(future)})
        errors, requests = serve(page)
        page.goto(ORIGIN + "/dashboard")
        wait_for_js(
            page,
            "document.querySelector('[data-cache-notice]').textContent.includes('future-dated')",
        )
        assert page.locator("#company-homepage").input_value() == ""
        assert page.evaluate("(key) => localStorage.getItem(key)", draft_key) is None
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", report_key)) == future
        assert "expired" in page.locator("[data-cache-notice]").inner_text()
        lists = [
            json.loads(body)
            for path, method, body in requests
            if path == "/api/private/insights/cached-list" and method == "POST"
        ]
        assert all(not posted["entries"] for posted in lists)
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("exception", ["QuotaExceededError", "SecurityError"])
def test_browser_storage_failures_warn_but_do_not_block_native_generation(browser, exception):
    context = browser.new_context()
    try:
        page = context.new_page()
        errors, requests = serve(
            page,
            api={
                "/insights": lambda route: route.fulfill(
                    body="Generation accepted by the isolated fixture.", content_type="text/plain"
                )
            },
        )
        page.goto(ORIGIN + "/dashboard")
        page.wait_for_selector("[data-saved-report-fragment]")
        page.evaluate(
            """(name) => {
                Storage.prototype.setItem = function() {
                    throw new DOMException('Storage unavailable in fixture', name);
                };
            }""",
            exception,
        )
        page.locator("#company-homepage").fill("https://contoso.com")
        wait_for_js(page, "!document.querySelector('[data-cache-warning]').hidden")
        assert page.get_by_role("button", name="Generate insights").is_enabled()
        assert page.locator("[data-draft-retry]").is_visible()
        page.get_by_role("button", name="Generate insights").click()
        page.wait_for_url(ORIGIN + "/insights")
        posts = [
            parse_qs(body, keep_blank_values=True)
            for path, method, body in requests
            if path == "/insights" and method == "POST"
        ]
        assert len(posts) == 1
        assert posts[0]["homepage_url"] == ["https://contoso.com"]
        assert posts[0]["audiences"] == list(AUDIENCES)
        assert posts[0]["csrf_token"] == ["ui-fixture-csrf"]
        assert posts[0]["submission_id"] == ["ui-fixture-submission"]
        assert not errors
    finally:
        context.close()


def test_browser_account_switch_never_mixes_drafts_or_clear_operations(browser):
    context = browser.new_context()
    try:
        first = context.new_page()
        second_prefix = "cxplorer:" + "2" * 64
        first_draft = draft_record(homepage_url="https://contoso.com/first-workspace")
        second_draft = draft_record(homepage_url="https://contoso.com/second-workspace")
        first_key = f"{PREFIX}:draft"
        second_key = f"{second_prefix}:draft"
        seed_storage(
            first,
            {
                first_key: json.dumps(first_draft),
                second_key: json.dumps(second_draft),
                "unrelated": "keep me",
            },
        )
        first_errors, _ = serve(first)
        first.goto(ORIGIN + "/dashboard")
        wait_for_js(
            first, "document.querySelector('[data-draft-status]').textContent.includes('restored')"
        )
        second = context.new_page()
        second_errors, _ = serve(second, overrides={"cache_namespace": "2" * 64})
        second.goto(ORIGIN + "/dashboard")
        wait_for_js(
            second, "document.querySelector('[data-draft-status]').textContent.includes('restored')"
        )
        wait_for_js(
            first,
            "document.querySelector('[data-cache-warning]').textContent.includes('another tab')",
        )
        assert (
            first.locator("#company-homepage").input_value()
            == first_draft["fields"]["homepage_url"]
        )
        assert (
            second.locator("#company-homepage").input_value()
            == second_draft["fields"]["homepage_url"]
        )
        first.locator("#company-homepage").fill("https://contoso.com/stale-edit")
        first.wait_for_timeout(300)
        assert (
            json.loads(first.evaluate("(key) => localStorage.getItem(key)", first_key))
            == first_draft
        )
        second.locator("#company-homepage").fill("https://contoso.com/current-edit")
        wait_for_js(
            second,
            "document.querySelector('[data-draft-status]').textContent.includes('Draft saved')",
        )
        assert (
            json.loads(second.evaluate("(key) => localStorage.getItem(key)", second_key))["fields"][
                "homepage_url"
            ]
            == "https://contoso.com/current-edit"
        )
        second.on("dialog", lambda dialog: dialog.accept())
        second.locator(".cache-tools summary").click()
        second.get_by_role("button", name="Clear my browser cache").click()
        wait_for_js(
            second, "document.querySelector('[data-cache-notice]').textContent.includes('Cleared')"
        )
        assert second.evaluate("(key) => localStorage.getItem(key)", second_key) is None
        assert (
            json.loads(first.evaluate("(key) => localStorage.getItem(key)", first_key))
            == first_draft
        )
        assert second.evaluate("localStorage.getItem('unrelated')") == "keep me"
        assert not first_errors + second_errors
    finally:
        context.close()


def test_browser_parallel_cache_writes_cannot_exceed_app_budget(browser):
    context = browser.new_context()
    try:
        pages = [context.new_page(), context.new_page()]
        for page in pages:
            serve(page)
            page.goto(ORIGIN + "/dashboard")
            page.wait_for_selector("[data-saved-report-fragment]")
        pages[0].evaluate("""() => {
            navigator.locks.request('cxplorer:cache-write', async () => {
                window.fixtureLockHeld = true;
                await new Promise(resolve => { window.releaseFixtureLock = resolve; });
            });
        }""")
        wait_for_js(pages[0], "window.fixtureLockHeld")
        for page, report_id in zip(pages, [REPORT_ID, "b" * 32], strict=True):
            page.evaluate(
                """(record) => {
                    import('/static/js/workspace-cache.js').then(({BrowserCache}) => {
                        const cache = new BrowserCache(document.querySelector('[data-cache-root]'));
                        cache.saveReport(record, record.report_id).then(
                            () => { window.fixtureWrite = 'saved'; },
                            error => { window.fixtureWrite = error.code; }
                        );
                    });
                }""",
                cache_record(report_id, blob="x" * 600_000),
            )
        pages[0].evaluate("window.releaseFixtureLock()")
        for page in pages:
            wait_for_js(page, "window.fixtureWrite !== undefined")
        keys = pages[0].evaluate(
            "() => Object.keys(localStorage).filter(k => k.startsWith('cxplorer:'))"
        )
        assert len(keys) == 1
        total = pages[0].evaluate("""() => Object.keys(localStorage)
            .filter(k => k.startsWith('cxplorer:'))
            .reduce((sum, k) => sum + 2 * (k.length + localStorage.getItem(k).length), 0)""")
        assert total <= 2 * 1024 * 1024
        assert sorted(page.evaluate("window.fixtureWrite") for page in pages).count("saved") == 1
    finally:
        context.close()


def test_browser_progress_updates_the_stage_estimate_without_inline_styles(browser):
    context = browser.new_context(
        viewport={"width": 320, "height": 900},
        reduced_motion="reduce",
    )
    try:
        page = context.new_page()
        status_path = f"/api/private/insights/{REPORT_ID}/status"
        errors, _ = serve(
            page,
            "insights_progress.html",
            api={
                status_path: lambda route: route.fulfill(
                    json={
                        "state": "running",
                        "stage": "synthesize_strategy",
                        "message": "Developing the Contoso opportunity brief.",
                        "error": None,
                        "report_url": f"{ORIGIN}/insights/{REPORT_ID}",
                    }
                )
            },
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        page.wait_for_selector('[data-progress-meter][aria-valuenow="76"]', timeout=8000)

        meter = page.locator("[data-progress-meter]")
        track_width = meter.bounding_box()["width"]
        fill_width = page.locator(".generation-progress__fill").bounding_box()["width"]
        assert meter.get_attribute("aria-valuetext") == "About 76 percent. Synthesize strategy."
        assert meter.get_attribute("data-progress-level") == "76"
        assert page.locator("[data-progress-value]").inner_text().strip() == "About 76%"
        assert page.locator("[data-progress-stage]").inner_text() == "Synthesize strategy"
        assert 0.74 <= fill_width / track_width <= 0.78
        assert (
            page.locator(".generation-progress__fill").evaluate(
                "(element) => getComputedStyle(element).animationName"
            )
            == "none"
        )
        assert not page.locator("[data-progress-meter]").get_attribute("style")
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("state", ["partial", "failed"])
def test_browser_progress_reloads_server_markup_on_terminal_state(browser, state):
    context = browser.new_context()
    try:
        page = context.new_page()
        path = f"/insights/{REPORT_ID}"
        running = render("insights_progress.html")
        job = SimpleNamespace(
            id=REPORT_ID,
            state="failed",
            stage="failed",
            message="Source verification stopped.",
            error="The sources could not be verified.",
            source_outcomes=[],
        )
        report = report_fixture().model_copy(deep=True)
        report.status = "partial"
        finished = (
            render("insights_report.html", report=report)
            if state == "partial"
            else render("insights_progress.html", job=job)
        )
        visits = 0

        def report_page(route):
            nonlocal visits
            visits += 1
            route.fulfill(body=running if visits == 1 else finished, content_type="text/html")

        errors, requests = serve(
            page,
            "insights_progress.html",
            api={
                path: report_page,
                f"/api/private/insights/{REPORT_ID}/status": lambda route: route.fulfill(
                    json={
                        "state": state,
                        "stage": "complete",
                        "message": "Generation ended.",
                        "error": None,
                        "report_url": "https://not-used.invalid",
                    }
                ),
            },
        )
        page.goto(ORIGIN + path)
        if state == "partial":
            page.wait_for_selector("[data-insight-report]", timeout=8000)
            assert "Partial report" in page.locator("#coverage-title").inner_text()
        else:
            wait_for_js(
                page,
                "document.querySelector('[data-insight-progress]').dataset.jobState === 'failed'",
                timeout=8000,
            )
            assert page.locator("[data-cancel-form]").count() == 0
        assert visits == 2
        assert page.url == ORIGIN + path
        assert not [item for item in requests if item[0] == "/insights" and item[1] == "POST"]
        assert not errors
    finally:
        context.close()


def test_browser_progress_401_stops_polling_and_offers_sign_in(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        path = f"/api/private/insights/{REPORT_ID}/status"
        errors, requests = serve(
            page,
            "insights_progress.html",
            api={path: lambda route: route.fulfill(status=401, json={"detail": "Sign in again."})},
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        page.wait_for_selector("[data-session-link]:visible", timeout=8000)
        page.wait_for_timeout(2200)
        assert len([item for item in requests if item[0] == path]) == 1
        assert page.locator("[data-cancel-form] button").is_disabled()
        assert page.locator("[data-progress-warning]").is_visible()
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("change", ["draft", "other_report"])
def test_browser_pending_report_save_ignores_unrelated_cache_changes(browser, change):
    context = browser.new_context(viewport={"width": 320, "height": 900})
    try:
        page = context.new_page()
        pending = []
        path = f"/api/private/insights/{REPORT_ID}/cache"
        errors, _ = serve(page, "insights_report.html", api={path: pending.append})
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        response = held_route(page, pending)
        changed_key = f"{PREFIX}:draft" if change == "draft" else f"{PREFIX}:report:{'b' * 32}"
        page.evaluate(
            """key => {
                window.fixtureCacheChanged = false;
                addEventListener('storage', event => {
                    if (event.key === key) window.fixtureCacheChanged = true;
                });
            }""",
            changed_key,
        )
        workspace = context.new_page()
        workspace_errors, _ = serve(workspace)
        workspace.goto(ORIGIN + "/dashboard")
        workspace.wait_for_selector("[data-saved-report-fragment]")
        if change == "draft":
            workspace.locator("#company-homepage").fill("https://contoso.com/recent-edit")
        else:
            workspace.evaluate(
                """async record => {
                    const {BrowserCache} = await import('/static/js/workspace-cache.js');
                    const cache = new BrowserCache(document.querySelector('[data-cache-root]'));
                    await cache.saveReport(record, record.report_id);
                }""",
                cache_record("b" * 32),
            )
        wait_for_js(page, "window.fixtureCacheChanged")
        expected = cache_record()
        response.fulfill(json=expected)
        wait_for_js(page, "!document.querySelector('[data-report-retry]').disabled")
        raw = page.evaluate("(key) => localStorage.getItem(key)", f"{PREFIX}:report:{REPORT_ID}")
        assert raw is not None, page.locator("[data-report-save-status]").inner_text()
        assert json.loads(raw) == expected
        assert "Saved in this browser" in page.locator("[data-report-save-status]").inner_text()
        assert page.locator("[data-cache-warning]").is_hidden()
        assert page.evaluate("(key) => localStorage.getItem(key)", changed_key)
        assert not errors + workspace_errors
    finally:
        context.close()


@pytest.mark.parametrize(
    "change",
    [
        "local_delete",
        "delete_failure",
        "local_clear",
        "external_delete",
        "external_clear_empty",
        "external_replace",
        "account_switch",
    ],
)
def test_browser_pending_report_save_respects_relevant_invalidation(browser, change):
    context = browser.new_context()
    try:
        page = context.new_page()
        key = f"{PREFIX}:report:{REPORT_ID}"
        other_key = f"cxplorer:{'2' * 64}:report:{'b' * 32}"
        previous = cache_record(blob="previous-opaque-signed-copy")
        entries = {
            f"{PREFIX}:draft": json.dumps(draft_record(homepage_url="https://contoso.com")),
            other_key: json.dumps(cache_record("b" * 32)),
        }
        if change != "external_clear_empty":
            entries[key] = json.dumps(previous)
        seed_storage(page, entries)
        pending = []
        path = f"/api/private/insights/{REPORT_ID}/cache"
        errors, _ = serve(page, "insights_report.html", api={path: pending.append})
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        response = held_route(page, pending)
        page.on("dialog", lambda dialog: dialog.accept())
        expected = previous if change in {"delete_failure", "account_switch"} else None
        if change in {"local_delete", "delete_failure"}:
            if change == "delete_failure":
                page.evaluate("""() => {
                    Storage.prototype.removeItem = function() {
                        throw new DOMException('Removal blocked in fixture', 'SecurityError');
                    };
                }""")
            page.locator("[data-report-delete]").click()
        elif change == "local_clear":
            page.locator(".cache-tools summary").click()
            page.locator("[data-cache-clear]").click()
        else:
            workspace = context.new_page()
            overrides = {"cache_namespace": "2" * 64} if change == "account_switch" else None
            workspace_errors, _ = serve(workspace, overrides=overrides)
            workspace.goto(ORIGIN + "/dashboard")
            workspace.wait_for_selector("[data-saved-report-fragment]")
            workspace.on("dialog", lambda dialog: dialog.accept())
            if change == "external_delete":
                workspace.locator(f'[data-delete-saved="{REPORT_ID}"]').click()
            elif change == "external_clear_empty":
                workspace.locator(".cache-tools summary").click()
                workspace.locator("[data-cache-clear]").click()
            elif change == "external_replace":
                expected = cache_record(blob="newer-opaque-signed-copy")
                workspace.evaluate(
                    """async record => {
                        const {BrowserCache} = await import('/static/js/workspace-cache.js');
                        const cache = new BrowserCache(document.querySelector('[data-cache-root]'));
                        await cache.saveReport(record, record.report_id);
                    }""",
                    expected,
                )
            assert not workspace_errors
        wait_for_js(
            page,
            "!document.querySelector('[data-report-save-status]').textContent.includes('Preparing')",
        )
        with page.expect_request_finished(
            predicate=lambda request: urlsplit(request.url).path == path
        ):
            response.fulfill(json=cache_record(blob="stale-opaque-server-response"))
        # Drain the response continuation without relying on the 250 ms draft timer.
        page.bring_to_front()
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
        raw = page.evaluate("(key) => localStorage.getItem(key)", key)
        assert (json.loads(raw) if raw is not None else None) == expected
        message = page.locator("[data-report-save-status]").inner_text().lower()
        assert any(text in message for text in ("not saved", "stopped", "paused", "not confirmed"))
        assert page.locator("[data-report-retry]").is_visible()
        assert page.locator("[data-report-retry]").is_disabled() is (change == "account_switch")
        assert page.locator("[data-cache-root]").get_attribute("data-cache-prefix") == PREFIX
        assert page.evaluate("(key) => localStorage.getItem(key)", other_key)
        if change == "delete_failure":
            assert page.locator("[data-cache-warning]").is_visible()
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("failure", [None, "QuotaExceededError", "SecurityError"])
def test_browser_logout_flushes_pending_draft_before_native_post(browser, failure):
    context = browser.new_context(viewport={"width": 320, "height": 900})
    try:
        page = context.new_page()
        draft_key = f"{PREFIX}:draft"
        report_key = f"{PREFIX}:report:{REPORT_ID}"
        original = draft_record(homepage_url="https://contoso.com/previous")
        seed_storage(
            page,
            {
                draft_key: json.dumps(original),
                report_key: json.dumps(cache_record()),
            },
        )
        errors, requests = serve(
            page,
            api={
                "/auth/logout": lambda route: route.fulfill(
                    body="Signed out in the isolated fixture.", content_type="text/plain"
                )
            },
        )
        page.goto(ORIGIN + "/dashboard")
        wait_for_js(
            page, "document.querySelector('[data-draft-status]').textContent.includes('restored')"
        )
        page.wait_for_selector("[data-saved-report-fragment]")
        hold_draft_debounce(page)
        hold_native_logout(page)
        if failure:
            page.evaluate(
                """name => {
                    Storage.prototype.setItem = function() {
                        throw new DOMException('Write blocked in fixture', name);
                    };
                }""",
                failure,
            )
        page.evaluate(
            """key => {
                const observer = new BroadcastChannel('cxplorer:workspace-context');
                observer.onmessage = ({data}) => {
                    if (data.type === 'signed-out') {
                        window.fixtureSignOutDraft = localStorage.getItem(key);
                    }
                };
                navigator.locks.request('cxplorer:cache-write', async () => {
                    window.fixtureLockHeld = true;
                    await new Promise(resolve => { window.releaseFixtureLock = resolve; });
                });
            }""",
            draft_key,
        )
        wait_for_js(page, "window.fixtureLockHeld")
        page.evaluate("""() => {
            const input = document.querySelector('#company-homepage');
            input.value = 'https://contoso.com/immediate-sign-out';
            input.dispatchEvent(new Event('input', {bubbles: true}));
            const logout = document.querySelector('[data-sign-out]');
            window.fixtureLogoutPrevented = [];
            logout.addEventListener('submit', event => {
                window.fixtureLogoutPrevented.push(event.defaultPrevented);
                // Keep the pre-fix failure observable without an uncontrolled navigation.
                event.preventDefault();
            });
            logout.requestSubmit();
            logout.requestSubmit();
            document.querySelector('[data-draft-form]').requestSubmit();
        }""")
        assert page.evaluate("window.fixtureLogoutPrevented") == [True, True]
        assert page.evaluate("window.fixtureNativePosts") == 0
        assert (
            json.loads(page.evaluate("(key) => localStorage.getItem(key)", draft_key)) == original
        )
        page.evaluate("window.releaseFixtureLock()")
        wait_for_js(page, "window.fixtureNativePosts === 1")
        assert page.evaluate("window.fixtureNativeFormIsLogout")
        saved = json.loads(page.evaluate("(key) => localStorage.getItem(key)", draft_key))
        if failure:
            assert saved == original
            assert page.locator("[data-cache-warning]").is_visible()
            assert "not saved" in page.locator("[data-draft-status]").inner_text()
        else:
            assert saved["fields"]["homepage_url"] == "https://contoso.com/immediate-sign-out"
            assert set(saved["fields"]) == set(FIELDS)
            assert set(saved) == {"fields", "audiences", "updated_at", "expires_at"}
            assert datetime.fromisoformat(saved["updated_at"]) == NOW + timedelta(seconds=10)
            assert datetime.fromisoformat(saved["expires_at"]) - datetime.fromisoformat(
                saved["updated_at"]
            ) == timedelta(days=7)
        wait_for_js(page, "window.fixtureSignOutDraft !== undefined")
        assert json.loads(page.evaluate("window.fixtureSignOutDraft")) == saved
        assert "csrf" not in json.dumps(saved)
        assert "seller@contoso.example" not in json.dumps(saved)
        assert page.evaluate("(key) => localStorage.getItem(key)", report_key)
        page.evaluate("() => setTimeout(window.releaseFixtureNativePost, 0)")
        page.wait_for_url(ORIGIN + "/auth/logout")
        posts = [
            parse_qs(body)
            for path, method, body in requests
            if path == "/auth/logout" and method == "POST"
        ]
        assert posts == [{"csrf_token": ["ui-fixture-csrf"]}]
        assert len([item for item in requests if item[0] == "/auth/logout"]) == 1
        assert not [item for item in requests if item[0] == "/insights" and item[1] == "POST"]
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", draft_key)) == saved
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("change", ["edit_again", "account_switch"])
def test_browser_logout_pending_write_boundary(browser, change):
    context = browser.new_context()
    try:
        page = context.new_page()
        draft_key = f"{PREFIX}:draft"
        other_prefix = "cxplorer:" + "2" * 64
        other_key = f"{other_prefix}:draft"
        original = draft_record(homepage_url="https://contoso.com/previous")
        other_draft = draft_record(homepage_url="https://contoso.com/other-account")
        seed_storage(
            page,
            {draft_key: json.dumps(original), other_key: json.dumps(other_draft)},
        )
        errors, requests = serve(
            page,
            api={
                "/auth/logout": lambda route: route.fulfill(
                    status=403 if change == "account_switch" else 200,
                    body="The fixture validates the original logout form.",
                    content_type="text/plain",
                )
            },
        )
        page.goto(ORIGIN + "/dashboard")
        wait_for_js(
            page, "document.querySelector('[data-draft-status]').textContent.includes('restored')"
        )
        page.wait_for_selector("[data-saved-report-fragment]")
        hold_draft_debounce(page)
        hold_native_logout(page)
        page.evaluate("""() => {
            navigator.locks.request('cxplorer:cache-write', async () => {
                window.fixtureLockHeld = true;
                await new Promise(resolve => { window.releaseFixtureLock = resolve; });
            });
        }""")
        wait_for_js(page, "window.fixtureLockHeld")
        page.locator("#company-homepage").fill("https://contoso.com/first-pending-edit")
        page.evaluate("document.querySelector('[data-sign-out]').requestSubmit()")
        assert page.evaluate("window.fixtureNativePosts") == 0
        if change == "edit_again":
            page.locator("#company-homepage").fill("https://contoso.com/latest-pending-edit")
        else:
            workspace = context.new_page()
            workspace_errors, _ = serve(
                workspace,
                overrides={"cache_namespace": "2" * 64, "csrf_token": "other-session-csrf"},
            )
            workspace.goto(ORIGIN + "/dashboard")
            wait_for_js(
                page, "document.querySelector('[data-draft-status]').textContent.includes('paused')"
            )
        page.evaluate("window.releaseFixtureLock()")
        wait_for_js(page, "window.fixtureNativePosts === 1")
        assert page.evaluate("window.fixtureNativeFormIsLogout")
        saved = json.loads(page.evaluate("(key) => localStorage.getItem(key)", draft_key))
        if change == "edit_again":
            assert saved["fields"]["homepage_url"] == "https://contoso.com/latest-pending-edit"
            assert set(saved["fields"]) == set(FIELDS)
        else:
            assert saved == original
            workspace.wait_for_selector("[data-saved-report-fragment]")
            assert workspace.locator("[data-cache-warning]").is_hidden()
            assert not workspace_errors
        assert (
            json.loads(page.evaluate("(key) => localStorage.getItem(key)", other_key))
            == other_draft
        )
        page.evaluate("() => setTimeout(window.releaseFixtureNativePost, 0)")
        page.wait_for_url(ORIGIN + "/auth/logout")
        posts = [
            parse_qs(body)
            for path, method, body in requests
            if path == "/auth/logout" and method == "POST"
        ]
        assert posts == [{"csrf_token": ["ui-fixture-csrf"]}]
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize("changed_date", ["generated_at", "expires_at"])
def test_browser_report_save_rejects_original_date_changes(browser, changed_date):
    context = browser.new_context()
    try:
        page = context.new_page()
        key = f"{PREFIX}:report:{REPORT_ID}"
        original = cache_record()
        incoming = cache_record()
        if changed_date == "generated_at":
            incoming["generated_at"] = (GENERATED + timedelta(minutes=1)).isoformat()
            incoming["expires_at"] = (GENERATED + timedelta(days=7, minutes=1)).isoformat()
        else:
            original["expires_at"] = (GENERATED + timedelta(days=6)).isoformat()
        seed_storage(page, {key: json.dumps(original)})
        errors, _ = serve(
            page,
            "insights_report.html",
            api={
                f"/api/private/insights/{REPORT_ID}/cache": lambda route: route.fulfill(
                    json=incoming
                )
            },
        )
        page.goto(f"{ORIGIN}/insights/{REPORT_ID}")
        wait_for_js(
            page,
            "document.querySelector('[data-report-save-status]').textContent.includes('not confirmed')",
        )
        assert json.loads(page.evaluate("(key) => localStorage.getItem(key)", key)) == original
        assert "different original dates" in page.locator("[data-cache-warning]").inner_text()
        assert page.locator("[data-report-retry]").is_enabled()
        assert not errors
    finally:
        context.close()
