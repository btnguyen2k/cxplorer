"""AI-enabled application wiring without credentials or provider requests."""

import logging
import os

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.main import create_app


def test_unconfigured_generation_logs_its_reason_once_at_startup(
    app_settings: AppSettings,
    identity_settings: IdentityVendorSettings,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_ENABLED", "true")
    app = create_app(app_settings, identity_settings)
    assert not any(record.name == "cxplorer.main" for record in caplog.records)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/health").status_code == 200
        assert app.state.ai_enabled is False
    records = [
        record
        for record in caplog.records
        if record.name == "cxplorer.main"
        and record.levelno == logging.WARNING
        and "generation is disabled" in record.getMessage()
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "no AI vendor is configured" in message
    assert "CX_AI__OPENAI__API_KEY" in message
    assert "CX_AI__AZURE_OPENAI__ENDPOINT" in message
    assert "CX_AI__AZURE_OPENAI__API_KEY" in message
    assert all(
        name in message for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")
    )
    assert "AI_ENABLED" not in message
    assert app_settings.session_secret.get_secret_value() not in caplog.text
    assert "CXplorer startup before: environment=test" in caplog.text
    assert "CXplorer startup after: insights_enabled=false vendors=none" in caplog.text
    assert "CXplorer shutdown before" not in caplog.text
    assert "CXplorer shutdown after" not in caplog.text


def test_enabled_factory_wires_configured_limits_and_closes_resources(
    tmp_path,
    monkeypatch,
    app_settings: AppSettings,
    identity_settings: IdentityVendorSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import cxplorer.ai.providers as providers
    import cxplorer.insights.pipeline as pipelines
    import cxplorer.insights.sources as sources

    for key in tuple(os.environ):
        if key.startswith(("CX_AI_", "AZURE_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AI_ENABLED", "false")
    vendor_file = tmp_path / "ai_vendors.env"
    vendor_file.write_text(
        'CX_AI__AZURE_OPENAI__ENDPOINT="https://contoso.openai.azure.com"\n'
        "CX_AI__AZURE_OPENAI__API_KEY=contoso-offline-factory-key\n",
        encoding="utf-8",
    )
    task_file = tmp_path / "ai_tasks.env"
    task_file.write_text(
        "CX_AI_PIPELINE__DEFAULT_AUDIENCES=ceo,cfo\n"
        "CX_AI_PIPELINE__MAX_SEED_URLS=4\n"
        "CX_AI_PIPELINE__FETCH_CONCURRENCY=2\n",
        encoding="utf-8",
    )
    events = []

    class FakeProvider:
        def __init__(self, settings):
            assert settings.tasks["discover_news"].use_web_search
            assert settings.vendors["azure_openai"].auth_mode == "entra_id"
            events.append("provider-created")

        async def close(self):
            events.append("provider-closed")

    class FakeFetcher:
        def __init__(self, limits):
            assert limits.max_concurrent_fetches == 2
            assert limits.request_timeout == 20
            events.append("fetcher-created")

        async def close(self):
            events.append("fetcher-closed")

    class FakePipeline:
        def __init__(self, settings, provider, fetcher):
            events.append("pipeline-created")

        async def run(self, report_id, request, progress):
            raise AssertionError("This factory test must not generate a report.")

        def forget(self, report_id):
            events.append("forgotten")

        def can_retry(self, report_id):
            return False

        async def close(self):
            events.append("pipeline-closed")

    monkeypatch.setattr(providers, "AIProviderClient", FakeProvider)
    monkeypatch.setattr(sources, "SourceFetcher", FakeFetcher)
    monkeypatch.setattr(pipelines, "InsightsPipeline", FakePipeline)
    settings = app_settings.model_copy(
        update={
            "ai_vendor_config_file": vendor_file,
            "ai_task_config_file": task_file,
        }
    )
    app = create_app(settings, identity_settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert app.state.ai_enabled is True
        assert app.state.insights_default_audiences == ["ceo", "cfo"]
        assert app.state.insights_max_seed_urls == 4
        assert len(app.state.insights_config_fingerprint) == 64
    assert events == [
        "provider-created",
        "fetcher-created",
        "pipeline-created",
        "pipeline-closed",
        "fetcher-closed",
        "provider-closed",
    ]
    assert not any(
        record.name == "cxplorer.main" and "generation is disabled" in record.getMessage()
        for record in caplog.records
    )


def test_billable_generation_rejects_a_known_shared_session_placeholder(
    app_settings: AppSettings, identity_settings: IdentityVendorSettings
) -> None:
    app_settings.ai_vendor_config_file.write_text(
        "CX_AI__OPENAI__API_KEY=contoso-offline-factory-key\n", encoding="utf-8"
    )
    settings = app_settings.model_copy(
        update={
            "environment": "production",
            "session_secret": SecretStr("<remember to generate a secure secret for production>"),
        }
    )
    with (
        pytest.raises(ValueError, match="real SESSION_SECRET"),
        TestClient(create_app(settings, identity_settings)),
    ):
        pass


@pytest.mark.parametrize("configuration", ["openai", "azure_key", "azure_identity"])
def test_any_configured_vendor_automatically_enables_the_application(
    app_settings: AppSettings,
    identity_settings: IdentityVendorSettings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    configuration: str,
) -> None:
    if configuration == "openai":
        contents = "CX_AI__OPENAI__API_KEY=contoso-offline-factory-key\n"
    else:
        contents = "CX_AI__AZURE_OPENAI__ENDPOINT=https://contoso.openai.azure.com\n"
        if configuration == "azure_key":
            contents += "CX_AI__AZURE_OPENAI__API_KEY=contoso-offline-factory-key\n"
        else:
            for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
                monkeypatch.setenv(name, "contoso-offline-identity-value")
    app_settings.ai_vendor_config_file.write_text(contents, encoding="utf-8")
    app = create_app(app_settings, identity_settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert app.state.ai_enabled is True
        assert all(task.vendor == "azure_openai" for task in app.state.ai_settings.tasks.values())
        assert app.state.insights_pipeline is not None
    assert not any(
        record.name == "cxplorer.main" and "generation is disabled" in record.getMessage()
        for record in caplog.records
    )
