"""Offline HTTP workflows using the real configured pipeline, jobs, and cache."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from cxplorer.ai.providers import AIProviderClient
from cxplorer.auth.dependencies import CSRF_TOKEN_KEY, SESSION_USER_KEY
from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.insights.schemas import AcceptedReport
from cxplorer.insights.urls import SourceError
from cxplorer.main import create_app
from tests.conftest import TEST_CSRF_TOKEN
from tests.test_insights_pipeline import (
    ABOUT,
    HOME,
    NEWS,
    FakeFetcher,
    FakeProvider,
    document,
    recent_candidate,
)

OWNER = hashlib.sha256(b"ada@example.com").hexdigest()
PRODUCTS = "https://contoso.com/products"
AUDIENCES = ["ceo", "cto", "cio", "cfo", "ciso"]


def application(app_settings: AppSettings, identity_settings: IdentityVendorSettings) -> FastAPI:
    app = create_app(app_settings, identity_settings)

    @app.post("/_test/sign-in", include_in_schema=False)
    def sign_in(request: Request, email: str = "ada@example.com") -> Response:
        request.session[SESSION_USER_KEY] = {
            "provider": "microsoft",
            "subject": "test-subject",
            "display_name": "Contoso reviewer",
            "email": email,
        }
        request.session[CSRF_TOKEN_KEY] = TEST_CSRF_TOKEN
        return Response(status_code=204)

    return app


@pytest.fixture
def offline_application(tmp_path, monkeypatch, app_settings, identity_settings):
    import cxplorer.ai.providers as providers
    import cxplorer.insights.sources as sources

    for name in tuple(os.environ):
        if name.startswith(("CX_AI_", "AZURE_")):
            monkeypatch.delenv(name)
    vendor_file = tmp_path / "ai_vendors.env"
    vendor_file.write_text(
        "CX_AI__AZURE_OPENAI__ENDPOINT=https://contoso.openai.azure.com\n"
        "CX_AI__AZURE_OPENAI__API_KEY=contoso-offline-workflow-key\n"
        "CX_AI__AZURE_OPENAI__MAX_RETRIES=0\n",
        encoding="utf-8",
    )
    task_file = tmp_path / "ai_tasks.env"
    task_file.write_text("", encoding="utf-8")
    provider_instances = []
    fetcher_instances = []

    class OfflineProvider(FakeProvider):
        def __init__(self, configured):
            super().__init__(configured, news=[recent_candidate()])
            self.closed = False
            provider_instances.append(self)

        async def close(self):
            self.closed = True

    class OfflineFetcher(FakeFetcher):
        def __init__(self, limits):
            super().__init__(
                [
                    document(),
                    document(
                        ABOUT, text="Contoso explains the company mission for invoice reviewers."
                    ),
                    document(
                        PRODUCTS,
                        text="Contoso provides reviewer worklists and invoice exception queues.",
                    ),
                    document(
                        NEWS,
                        published_at=datetime.now(UTC).date() - timedelta(days=4),
                        text=[
                            'Contoso CEO Alex Morgan said, "We are investing in reviewer workflows."'
                        ],
                    ),
                ]
            )
            self.closed = False
            fetcher_instances.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(providers, "AIProviderClient", OfflineProvider)
    monkeypatch.setattr(sources, "SourceFetcher", OfflineFetcher)
    configured = app_settings.model_copy(
        update={
            "ai_vendor_config_file": vendor_file,
            "ai_task_config_file": task_file,
        }
    )
    return application(configured, identity_settings), provider_instances, fetcher_instances


def submission() -> dict:
    return {
        "csrf_token": TEST_CSRF_TOKEN,
        "submission_id": "offline-contoso-workflow",
        "homepage_url": HOME,
        "about_url": ABOUT,
        "products_url": PRODUCTS,
        "audiences": AUDIENCES,
        "seller_context": "Help Contoso reviewers assess invoice exceptions.",
    }


async def finish_job(app: FastAPI, report_id: str):
    async with asyncio.timeout(10):
        while True:
            job = app.state.insights_jobs.get_job(report_id, OWNER)
            assert job is not None
            if job.state not in {"queued", "running"}:
                return job
            await asyncio.sleep(0)


def test_generate_review_save_and_restore_on_a_fresh_disabled_server(
    offline_application, app_settings, identity_settings
):
    app, providers, fetchers = offline_application
    with TestClient(app) as client:
        assert client.post("/_test/sign-in").status_code == 204
        provider, fetcher = providers[0], fetchers[0]
        provider.pause_task = "verify_sources"
        invalid_csrf = {**submission(), "csrf_token": "not-the-session-token"}
        assert client.post("/insights", data=invalid_csrf).status_code == 403
        assert not provider.calls
        assert not fetcher.calls

        response = client.post("/insights", data=submission(), follow_redirects=False)
        assert response.status_code == 303
        report_url = response.headers["location"]
        report_id = report_url.rsplit("/", 1)[1]
        status_url = f"/api/private/insights/{report_id}/status"
        cache_url = f"/api/private/insights/{report_id}/cache"
        download_url = f"/insights/{report_id}/download"
        assert client.get(status_url).json()["state"] in {"queued", "running"}
        assert "insights-progress.js" in client.get(report_url).text

        assert client.portal is not None
        client.portal.call(provider.release.set)
        assert client.portal.call(finish_job, app, report_id).state == "completed"
        assert client.get(status_url).json()["state"] == "completed"
        downloaded = client.get(download_url)
        assert downloaded.status_code == 200
        report = AcceptedReport.model_validate_json(downloaded.content)
        assert [section.audience for section in report.audiences] == AUDIENCES
        assert all(len(section.talk_points) == 3 for section in report.audiences)
        assert report.news_status == "found"
        assert report.announcements[0].executive_name == "Alex Morgan"
        assert {url for url, _, _ in fetcher.calls} == {HOME, ABOUT, PRODUCTS, NEWS}
        assert {call.task for call in provider.calls} == set(app.state.ai_settings.tasks)
        search = next(call for call in provider.calls if call.task == "discover_news")
        assert search.domains == ("contoso.com",)
        assert all("ada@example.com" not in json.dumps(call.data) for call in provider.calls)
        assert "insights-report.js" in client.get(report_url).text
        cached = client.get(cache_url)
        assert cached.status_code == 200
        assert cached.headers["cache-control"] == "no-store"
        payload = cached.json()
        assert payload["report_id"] == report_id
        call_count, fetch_count = len(provider.calls), len(fetcher.calls)

    assert provider.closed and fetcher.closed
    assert not app.state.insights_pipeline.can_retry(report_id)
    with TestClient(application(app_settings, identity_settings)) as fresh:
        fresh.post("/_test/sign-in", params={"email": "another-reviewer@example.com"})
        restore = {
            "csrf_token": TEST_CSRF_TOKEN,
            "cache_blob": payload["blob"],
            "report_id": report_id,
        }
        assert fresh.post("/insights/restore", data=restore).status_code == 400
        fresh.post("/_test/sign-in")
        assert fresh.get(cache_url).status_code == 404
        assert "data-insight-restore" in fresh.get(report_url).text
        assert (
            fresh.post(
                "/insights/restore", data={**restore, "cache_blob": "!" + payload["blob"]}
            ).status_code
            == 400
        )
        restored = fresh.post("/insights/restore", data=restore, follow_redirects=False)
        assert restored.status_code == 303
        assert fresh.get(restored.headers["location"]).status_code == 200
        assert fresh.get(download_url).json() == downloaded.json()
        restored_payload = fresh.get(cache_url).json()
        assert restored_payload["generated_at"] == payload["generated_at"]
        assert restored_payload["expires_at"] == payload["expires_at"]
        assert len(provider.calls) == call_count
        assert len(fetcher.calls) == fetch_count
        assert len(providers) == len(fetchers) == 1


def test_partial_report_retry_preserves_browser_cache_identity_and_expiry(offline_application):
    app, providers, _ = offline_application
    with TestClient(app) as client:
        client.post("/_test/sign-in")
        provider = providers[0]
        failed_once = False

        def temporary_failure(call, value):
            nonlocal failed_once
            if (
                call.task == "generate_talk_points"
                and call.data["audience"] == "cfo"
                and not failed_once
            ):
                from cxplorer.ai.providers import ProviderError

                failed_once = True
                return ProviderError(
                    "provider_unavailable", "Contoso research is temporarily unavailable.", True
                )
            return value

        provider.transform = temporary_failure
        response = client.post("/insights", data=submission(), follow_redirects=False)
        assert response.status_code == 303
        report_id = response.headers["location"].rsplit("/", 1)[1]
        assert client.portal is not None
        assert client.portal.call(finish_job, app, report_id).state == "partial"
        cache_url = f"/api/private/insights/{report_id}/cache"
        original = client.get(cache_url).json()
        assert client.post(f"/insights/{report_id}/retry", data={}).status_code == 403
        calls_before_retry = len(provider.calls)
        retried = client.post(
            f"/insights/{report_id}/retry",
            data={"csrf_token": TEST_CSRF_TOKEN},
            follow_redirects=False,
        )
        assert retried.status_code == 303
        assert retried.headers["location"].endswith(f"/insights/{report_id}")
        assert client.portal.call(finish_job, app, report_id).state == "completed"
        assert len(provider.calls) == calls_before_retry + 2
        updated = client.get(cache_url).json()
        assert updated["report_id"] == original["report_id"]
        assert updated["generated_at"] == original["generated_at"]
        assert updated["expires_at"] == original["expires_at"]
        assert updated["blob"] != original["blob"]


def test_enabled_installation_reports_an_unconfigured_task_vendor_at_runtime(
    offline_application, monkeypatch
):
    from unittest.mock import Mock

    app, _, _ = offline_application
    app.state.app_settings.ai_vendor_config_file.write_text(
        "CX_AI__OPENAI__API_KEY=contoso-offline-workflow-key\n", encoding="utf-8"
    )
    monkeypatch.setattr("cxplorer.ai.providers.AIProviderClient", AIProviderClient)
    sdk_factory = Mock(side_effect=AssertionError("No SDK request should be created."))
    monkeypatch.setattr("cxplorer.ai.providers.AsyncOpenAI", sdk_factory)
    with TestClient(app) as client:
        assert app.state.ai_enabled
        client.post("/_test/sign-in")
        workspace = client.get("/dashboard")
        assert workspace.status_code == 200
        assert "Generation is disabled on this installation" not in workspace.text
        response = client.post("/insights", data=submission(), follow_redirects=False)
        assert response.status_code == 303
        report_id = response.headers["location"].rsplit("/", 1)[1]
        assert client.portal is not None
        job = client.portal.call(finish_job, app, report_id)
        assert job.state == "failed"
        assert "AzureOpenAI" in job.message and "not configured" in job.message
        page = client.get(response.headers["location"])
        assert "AzureOpenAI" in page.text and "ai_tasks.env" in page.text
        assert client.get(f"/api/private/insights/{report_id}/cache").status_code == 404
    sdk_factory.assert_not_called()


@pytest.mark.parametrize("failure", ["inaccessible", "unrelated"])
def test_invalid_supplied_sources_fail_without_publishing_a_report(offline_application, failure):
    app, providers, fetchers = offline_application
    with TestClient(app) as client:
        client.post("/_test/sign-in")
        provider, fetcher = providers[0], fetchers[0]
        if failure == "inaccessible":
            fetcher.errors[ABOUT] = SourceError(
                "blocked", "Use an accessible official Contoso About page."
            )
        else:

            def reject_about(call, value):
                if call.task != "verify_sources":
                    return value
                return value.model_copy(
                    update={
                        "verdicts": [
                            verdict.model_copy(update={"relation": "unrelated"})
                            if verdict.source_id != value.homepage_source_id
                            else verdict
                            for verdict in value.verdicts
                        ]
                    }
                )

            provider.transform = reject_about
        response = client.post("/insights", data=submission(), follow_redirects=False)
        assert response.status_code == 303
        report_url = response.headers["location"]
        report_id = report_url.rsplit("/", 1)[1]
        assert client.portal is not None
        assert client.portal.call(finish_job, app, report_id).state == "failed"
        assert client.get(f"/api/private/insights/{report_id}/status").json()["state"] == "failed"
        assert client.get(report_url).status_code == 200
        assert client.get(f"/api/private/insights/{report_id}/cache").status_code == 404
        assert client.get(f"/insights/{report_id}/download").status_code == 404
        assert not any(call.task == "discover_news" for call in provider.calls)
