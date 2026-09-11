"""Native Pydantic settings sources with isolated files and offline credentials."""

import json
import os
from io import StringIO
from pathlib import Path

import pytest
from dotenv.parser import parse_stream
from pydantic import SecretStr
from pydantic_settings import BaseSettings

from cxplorer.ai.config import (
    TASK_IDS,
    AIConfigurationError,
    AISettings,
    AITaskSettings,
    AIVendorSettings,
    VendorSettings,
    load_ai_settings,
)

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "https://contoso.openai.azure.com"
OFFLINE_KEY = "contoso-offline-api-key-123456789"


@pytest.fixture(autouse=True)
def isolated_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith(("CX_AI", "AZURE_")) or name.casefold() in {
            "vendors",
            "tasks",
            "pipeline",
        }:
            monkeypatch.delenv(name)
    return tmp_path


@pytest.fixture
def ai_files(isolated_sources: Path) -> Path:
    (isolated_sources / "ai_vendors.env").write_text(
        f"CX_AI__AZURE_OPENAI__ENDPOINT={ENDPOINT}\n", encoding="utf-8"
    )
    (isolated_sources / "ai_tasks.env").write_text("", encoding="utf-8")
    return isolated_sources


def set_environment(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_dedicated_sources_are_native_settings_with_safe_defaults() -> None:
    assert issubclass(AIVendorSettings, BaseSettings)
    assert issubclass(AITaskSettings, BaseSettings)
    vendors = AIVendorSettings(_env_file=None).vendors
    tasks = AITaskSettings(_env_file=None).tasks
    assert vendors["azure_openai"].endpoint == ""
    assert vendors["azure_openai"].auth_mode == "entra_id"
    assert vendors["openai"].auth_mode == "api_key"
    assert vendors["openai"].endpoint == ""
    assert not any(vendor.enabled for vendor in vendors.values())
    assert all(vendor.request_timeout_seconds == 600 for vendor in vendors.values())
    assert set(tasks) == set(TASK_IDS)
    assert all(task.vendor == "azure_openai" for task in tasks.values())
    assert all(task.timeout_seconds == 600 for task in tasks.values())
    assert [name for name, task in tasks.items() if task.use_web_search] == ["discover_news"]
    assert tasks["discover_news"].max_tool_calls == 25


def test_pipeline_defaults_match_operational_contract() -> None:
    limits = AISettings().pipeline
    assert limits.default_audiences == ("ceo", "cto", "cio", "cfo", "ciso")
    assert limits.talk_points_per_audience == 3
    assert limits.max_seed_urls == 6 and limits.max_fetched_pages == 12
    assert limits.news_days == 90 and limits.max_news_articles == 3
    assert limits.max_active_jobs_per_user == 1
    assert limits.max_running_jobs == 2 and limits.max_queued_jobs == 4
    assert limits.job_timeout_seconds == 900 and limits.max_model_calls == 40
    assert limits.max_total_input_tokens == 300000 and limits.max_total_output_tokens == 120000
    assert limits.max_repair_rounds == 1 and limits.fetch_timeout_seconds == 20
    assert limits.max_html_bytes == 2 * 1024 * 1024
    assert limits.max_pdf_bytes == 15 * 1024 * 1024 and limits.max_pdf_pages == 200
    assert limits.result_ttl_seconds == 3600 and limits.max_results == 16


def test_native_layers_and_constructor_overrides_preserve_sibling_defaults(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (ai_files / "ai_vendors.local.env").write_text(
        "CX_AI__AZURE_OPENAI__ENDPOINT=https://contoso-local.openai.azure.com\n"
        f"CX_AI__AZURE_OPENAI__API_KEY={OFFLINE_KEY}\n",
        encoding="utf-8",
    )
    (ai_files / "ai_tasks.env").write_text(
        "CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT=low\n", encoding="utf-8"
    )
    (ai_files / "ai_tasks.local.env").write_text(
        "CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT=High\n", encoding="utf-8"
    )
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__ENDPOINT", "https://contoso-env.openai.azure.com")
    vendors = AIVendorSettings(CX_AI={"azure_openai": {"max_retries": 1}}).vendors
    assert vendors["azure_openai"].endpoint == "https://contoso-env.openai.azure.com/openai/v1/"
    assert vendors["azure_openai"].api_key.get_secret_value() == OFFLINE_KEY
    assert vendors["azure_openai"].max_retries == 1
    assert vendors["openai"].endpoint == ""
    tasks = AITaskSettings(CX_AI_TASK={"review_report": {"model": "contoso-deployment"}}).tasks
    assert tasks["review_report"].model == "contoso-deployment"
    assert tasks["review_report"].reasoning_effort == "high"
    assert tasks["review_report"].max_input_tokens == 32000
    assert tasks["extract_evidence"].max_input_tokens == 12000
    assert tasks["discover_news"].use_web_search
    assert set(tasks) == set(TASK_IDS)


def test_native_json_trees_and_nested_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_environment(
        monkeypatch,
        {
            "CX_AI": json.dumps({"azure_openai": {"endpoint": ENDPOINT, "max_retries": 1}}),
            "CX_AI__AZURE_OPENAI__MAX_RETRIES": "3",
            "CX_AI_TASK": json.dumps({"review_report": {"model": "contoso-json-deployment"}}),
            "CX_AI_TASK__REVIEW_REPORT__MAX_OUTPUT_TOKENS": "6500",
            "CX_AI_PIPELINE": json.dumps({"default_audiences": ["ceo", "cfo"]}),
        },
    )
    settings = load_ai_settings()
    assert settings.vendors["azure_openai"].max_retries == 3
    assert settings.tasks["review_report"].model == "contoso-json-deployment"
    assert settings.tasks["review_report"].max_output_tokens == 6500
    assert settings.tasks["review_report"].reasoning_effort == "high"
    assert settings.tasks["discover_news"].use_web_search
    assert settings.pipeline.default_audiences == ("ceo", "cfo")


def test_designated_files_use_their_matching_local_overrides(ai_files: Path) -> None:
    vendor = ai_files / "contoso-vendors.env"
    task = ai_files / "contoso-tasks.env"
    vendor.write_text(f"CX_AI__AZURE_OPENAI__ENDPOINT={ENDPOINT}\n", encoding="utf-8")
    task.write_text("", encoding="utf-8")
    task.with_name("contoso-tasks.local.env").write_text(
        "CX_AI_TASK__VERIFY_SOURCES__MODEL=contoso-deployment\n", encoding="utf-8"
    )
    assert load_ai_settings(vendor, task).tasks["verify_sources"].model == ("contoso-deployment")


def test_environment_only_deployments_do_not_require_dotenv_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert load_ai_settings().vendors["azure_openai"].endpoint == ""
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__ENDPOINT", ENDPOINT)
    assert load_ai_settings().vendors["azure_openai"].endpoint == (ENDPOINT + "/openai/v1/")
    assert not load_ai_settings().enabled
    monkeypatch.setenv("CX_AI__OPENAI__API_KEY", OFFLINE_KEY)
    settings = load_ai_settings()
    assert settings.enabled and settings.vendors["openai"].enabled
    assert not settings.vendors["azure_openai"].enabled


@pytest.mark.parametrize(
    "spelling",
    ["AzureOpenAI", "Azure OpenAI", "Azure-OpenAI", "Azure_Open_AI", "azureopenai"],
)
def test_azure_routing_names_are_normalized(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    monkeypatch.setenv("CX_AI_TASK__EXTRACT_EVIDENCE__VENDOR", spelling)
    assert load_ai_settings().tasks["extract_evidence"].vendor == "azure_openai"


@pytest.mark.parametrize("spelling", ["OpenAI", "Open AI", "Open-AI", "Open_AI", "openai"])
def test_openai_routing_names_and_unused_azure_are_supported(
    monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    set_environment(
        monkeypatch, {f"CX_AI_TASK__{task.upper()}__VENDOR": spelling for task in TASK_IDS}
    )
    monkeypatch.setenv("CX_AI__OPENAI__API_KEY", OFFLINE_KEY)
    assert load_ai_settings().vendors["azure_openai"].endpoint == ""
    assert load_ai_settings().enabled
    monkeypatch.delenv("CX_AI__OPENAI__API_KEY")
    assert not load_ai_settings().enabled


@pytest.mark.parametrize("suffix", ["", "/", "/openai/v1", "/openai/v1/"])
def test_azure_endpoint_roots_and_v1_bases(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__ENDPOINT", ENDPOINT + suffix)
    assert load_ai_settings().vendors["azure_openai"].endpoint == (ENDPOINT + "/openai/v1/")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://contoso.openai.azure.com",
        "https://contoso.openai.azure.com:8443",
        "https://contoso.openai.azure.com/openai/deployments/contoso",
        "https://contoso.openai.azure.com/openai/v1/responses",
        "https://contoso.openai.azure.com?api-key=contoso-private",
        "https://contoso:private@contoso.openai.azure.com",
        "https://contoso.openai.azure.com/#contoso",
    ],
)
def test_invalid_endpoints_are_redacted(monkeypatch: pytest.MonkeyPatch, endpoint: str) -> None:
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__ENDPOINT", endpoint)
    with pytest.raises(AIConfigurationError) as caught:
        load_ai_settings()
    assert caught.value.errors == ("CX_AI__AZURE_OPENAI__ENDPOINT",)
    assert "contoso" not in str(caught.value)


def test_azure_identity_is_sdk_environment_only_and_not_exported_from_dotenv(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (ai_files / "ai_vendors.env").open("a", encoding="utf-8") as stream:
        stream.write(
            "AZURE_TENANT_ID=contoso-file-tenant\n"
            "AZURE_CLIENT_ID=contoso-file-client\n"
            f"AZURE_CLIENT_SECRET={OFFLINE_KEY}\n"
            "MS_CLIENT_SECRET=contoso-login-secret\n"
        )
    monkeypatch.setenv("AZURE_CLIENT_ID", "contoso-sdk-client")
    settings = load_ai_settings()
    assert "AZURE_TENANT_ID" not in os.environ
    assert os.environ["AZURE_CLIENT_ID"] == "contoso-sdk-client"
    assert "AZURE_CLIENT_SECRET" not in os.environ
    assert "azure_identity" not in AISettings.model_fields
    assert "contoso-file" not in settings.model_dump_json()
    assert OFFLINE_KEY not in settings.model_dump_json()
    assert not settings.vendors["azure_openai"].api_key.get_secret_value()


@pytest.mark.parametrize("legacy", ["CX_AI_AZURE_OPENAI__ENDPOINT", "CX_AI__AzureOpenAI_ENDPOINT"])
def test_legacy_vendor_variable_shapes_are_not_loaded(
    monkeypatch: pytest.MonkeyPatch, legacy: str
) -> None:
    monkeypatch.setenv(legacy, ENDPOINT)
    if legacy.startswith("CX_AI__"):
        with pytest.raises(AIConfigurationError) as caught:
            load_ai_settings()
        assert caught.value.errors == ("CX_AI",)
    else:
        assert not load_ai_settings().vendors["azure_openai"].enabled


def test_unrelated_dotenv_roots_are_ignored(ai_files: Path) -> None:
    with (ai_files / "ai_vendors.env").open("a", encoding="utf-8") as stream:
        stream.write("CONTOSO_FEATURE=true\nCX_AI_TASK__REVIEW_REPORT__MODEL=contoso-ignored\n")
    with (ai_files / "ai_tasks.env").open("a", encoding="utf-8") as stream:
        stream.write("CONTOSO_OTHER_SETTING=42\nCX_AI__AZURE_OPENAI__MAX_RETRIES=1\n")
    settings = load_ai_settings()
    assert settings.tasks["review_report"].model != "contoso-ignored"
    assert settings.vendors["azure_openai"].max_retries == 2


def test_native_dotenv_interpolation_and_duplicate_precedence(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTOSO_AI_KEY", OFFLINE_KEY)
    with (ai_files / "ai_vendors.env").open("a", encoding="utf-8") as stream:
        stream.write(
            "CX_AI__OPENAI__API_KEY=contoso-first\nCX_AI__OPENAI__API_KEY=${CONTOSO_AI_KEY}\n"
        )
    assert load_ai_settings().vendors["openai"].api_key.get_secret_value() == OFFLINE_KEY
    assert "CX_AI__OPENAI__API_KEY" not in os.environ


def test_explicit_azure_api_key_auth(ai_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__AUTH_MODE", "API_KEY")
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__API_KEY", OFFLINE_KEY)
    settings = load_ai_settings()
    assert settings.vendors["azure_openai"].auth_mode == "api_key"
    assert settings.enabled


@pytest.mark.parametrize(
    "key", ["", " ", "YOUR_API_KEY", "<api-key>", "${CONTOSO_KEY}", "placeholder", "sk-..."]
)
def test_placeholder_keys_do_not_enable_a_vendor(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__AUTH_MODE", "api_key")
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__API_KEY", key)
    assert not load_ai_settings().vendors["azure_openai"].enabled


def test_native_scalar_parsing_and_csv_audiences(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_environment(
        monkeypatch,
        {
            "CX_AI_TASK__DISCOVER_NEWS__USE_WEB_SEARCH": "True",
            "CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT": " High ",
            "CX_AI_PIPELINE__DEFAULT_AUDIENCES": "CEO, cto,CISO",
            "CX_AI_PIPELINE__MAX_RUNNING_JOBS": "3",
            "CX_AI__AZURE_OPENAI__REQUEST_TIMEOUT_SECONDS": "125.5",
            "CX_AI_TASK__EXTRACT_EVIDENCE__TEMPERATURE": "0.2",
        },
    )
    settings = load_ai_settings()
    assert settings.tasks["discover_news"].use_web_search
    assert settings.tasks["review_report"].reasoning_effort == "high"
    assert settings.tasks["extract_evidence"].reasoning_effort == "low"
    assert settings.tasks["extract_evidence"].temperature == 0.2
    assert settings.pipeline.default_audiences == ("ceo", "cto", "ciso")
    assert settings.pipeline.max_running_jobs == 3
    assert settings.vendors["azure_openai"].request_timeout_seconds == 125.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CX_AI_TASK__DISCOVER_NEWS__USE_WEB_SEARCH", "False"),
        ("CX_AI_TASK__DISCOVER_NEWS__USE_WEB_SEARCH", "truthy"),
        ("CX_AI_TASK__DISCOVER_NEWS__MAX_TOOL_CALLS", "0"),
        ("CX_AI_TASK__DISCOVER_NEWS__MAX_TOOL_CALLS", "26"),
        ("CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT", ""),
        ("CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT", "minimal"),
        ("CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT", "none"),
        ("CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT", "xhigh"),
        ("CX_AI_TASK__REVIEW_REPORT__REASONING_EFFORT", "extreme"),
        ("CX_AI_TASK__REVIEW_REPORT__MODEL", ""),
        ("CX_AI_TASK__REVIEW_REPORT__MODEL", "contoso secret"),
        ("CX_AI_TASK__REVIEW_REPORT__PROMPT_VERSION", "v2"),
        ("CX_AI_TASK__REVIEW_REPORT__TIMEOUT_SECONDS", "0"),
        ("CX_AI_TASK__REVIEW_REPORT__MAX_INPUT_TOKENS", "1.5"),
        ("CX_AI_TASK__REVIEW_REPORT__MAX_OUTPUT_TOKENS", "-1"),
        ("CX_AI_TASK__REVIEW_REPORT__TEMPERATURE", "nan"),
        ("CX_AI__AZURE_OPENAI__AUTH_MODE", "user_oauth"),
        ("CX_AI__OPENAI__AUTH_MODE", "entra_id"),
        ("CX_AI__AZURE_OPENAI__MAX_RETRIES", "4"),
        ("CX_AI__AZURE_OPENAI__MAX_CONCURRENCY", "0"),
        ("CX_AI__AZURE_OPENAI__CONNECT_TIMEOUT_SECONDS", "inf"),
        ("CX_AI_PIPELINE__MAX_REPAIR_ROUNDS", "2"),
        ("CX_AI_PIPELINE__TALK_POINTS_PER_AUDIENCE", "true"),
        ("CX_AI_PIPELINE__TALK_POINTS_PER_AUDIENCE", "6"),
        ("CX_AI_PIPELINE__DEFAULT_AUDIENCES", "ceo,CEO"),
        ("CX_AI_PIPELINE__DEFAULT_AUDIENCES", ""),
        ("CX_AI_PIPELINE__DEFAULT_AUDIENCES", "coo"),
        ("CX_AI_PIPELINE__MAX_RUNNING_JOBS", "0"),
        ("CX_AI_PIPELINE__MAX_QUEUED_JOBS", "-1"),
        ("CX_AI_PIPELINE__MAX_MODEL_CALLS", "41"),
        ("CX_AI_PIPELINE__MAX_TOTAL_INPUT_TOKENS", "300001"),
        ("CX_AI_PIPELINE__MAX_TOTAL_OUTPUT_TOKENS", "120001"),
        ("CX_AI_PIPELINE__JOB_TIMEOUT_SECONDS", "900.1"),
        ("CX_AI_PIPELINE__FETCH_TIMEOUT_SECONDS", "20.1"),
        ("CX_AI_PIPELINE__MAX_HTML_BYTES", "2097153"),
        ("CX_AI_PIPELINE__MAX_PDF_BYTES", "15728641"),
        ("CX_AI_PIPELINE__MAX_PDF_PAGES", "201"),
        ("CX_AI_PIPELINE__MAX_REDIRECTS", "4"),
        ("CX_AI_PIPELINE__NEWS_DAYS", "91"),
        ("CX_AI_PIPELINE__MAX_NEWS_ARTICLES", "0"),
        ("CX_AI_PIPELINE__RESULT_TTL_SECONDS", "0"),
        ("CX_AI_PIPELINE__MAX_RESULTS", "17"),
        ("CX_AI_PIPELINE__TOKENIZER_ENCODING", "contoso-unknown-encoding"),
        ("CX_AI_PIPELINE__TOKEN_ESTIMATE_MULTIPLIER", "nan"),
    ],
)
def test_invalid_values_report_only_setting_names(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(AIConfigurationError) as caught:
        load_ai_settings()
    assert name in caught.value.errors
    assert "contoso secret" not in str(caught.value)


@pytest.mark.parametrize(
    "name",
    [
        "CX_AI__OPENAI__SECRET",
        "CX_AI_TASK__REVIEW_REPORT__UNKNOWN",
        "CX_AI_TASK__REVIEW_REPORT__API_KEY",
        "CX_AI_PIPELINE__NEWS_ENABLED",
    ],
)
def test_unknown_nested_config_fields_are_validated(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, OFFLINE_KEY)
    with pytest.raises(AIConfigurationError) as caught:
        load_ai_settings()
    assert caught.value.errors == (name,)
    assert OFFLINE_KEY not in str(caught.value)


def test_cross_field_bounds_remain_enforced(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CX_AI_PIPELINE__MAX_FETCHED_PAGES", "5")
    with pytest.raises(AIConfigurationError) as caught:
        load_ai_settings()
    assert set(caught.value.errors) == {
        "CX_AI_PIPELINE__MAX_FETCHED_PAGES",
        "CX_AI_PIPELINE__MAX_SEED_URLS",
    }
    monkeypatch.delenv("CX_AI_PIPELINE__MAX_FETCHED_PAGES")
    monkeypatch.setenv("CX_AI_PIPELINE__MAX_TOTAL_INPUT_TOKENS", "16000")
    with pytest.raises(AIConfigurationError, match="CX_AI_TASK__REVIEW_REPORT__MAX_INPUT_TOKENS"):
        load_ai_settings()


def test_malformed_root_json_is_redacted(ai_files: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CX_AI", '{"azure_openai":{"api_key":"' + OFFLINE_KEY)
    with pytest.raises(AIConfigurationError) as caught:
        load_ai_settings()
    assert caught.value.errors == ("CX_AI",)
    assert OFFLINE_KEY not in str(caught.value)


def test_secrets_never_enter_serialization_or_fingerprints(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__API_KEY", OFFLINE_KEY)
    first = load_ai_settings()
    monkeypatch.setenv("CX_AI__AZURE_OPENAI__API_KEY", "contoso-rotated-offline-key")
    assert first.fingerprint == load_ai_settings().fingerprint
    assert OFFLINE_KEY not in first.model_dump_json()
    assert OFFLINE_KEY not in repr(first)
    assert OFFLINE_KEY not in repr(AIVendorSettings())
    assert "api_key" not in first.model_dump()["vendors"]["azure_openai"]
    custom = AIVendorSettings(CX_AI={"azure_openai": {"api_key": SecretStr(OFFLINE_KEY)}})
    assert OFFLINE_KEY not in custom.model_dump_json()


def test_shared_files_document_canonical_keys_and_sdk_identity_only() -> None:
    for filename in ("ai_vendors.env", "ai_tasks.env"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        bindings = list(parse_stream(StringIO(text)))
        assert not any(binding.error for binding in bindings)
        values = {binding.key: binding.value for binding in bindings if binding.key}
        assert len(values) == sum(binding.key is not None for binding in bindings)
        assert all(
            not value
            for name, value in values.items()
            if name.endswith(("__API_KEY", "__ORGANIZATION", "__PROJECT"))
        )
        if filename == "ai_vendors.env":
            assert all(name.startswith("CX_AI__") for name in values)
            assert values["CX_AI__AZURE_OPENAI__ENDPOINT"] == ""
            assert values["CX_AI__AZURE_OPENAI__REQUEST_TIMEOUT_SECONDS"] == "600"
            assert values["CX_AI__OPENAI__REQUEST_TIMEOUT_SECONDS"] == "600"
            assert "CX_AI__OPENAI__ENDPOINT" not in values
            for name in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
                assert name in text and name not in values
            assert AIVendorSettings(_env_file=ROOT / filename).vendors[
                "azure_openai"
            ].auth_mode == ("entra_id")
        else:
            tasks = AITaskSettings(_env_file=ROOT / filename).tasks
            assert tasks["discover_news"].use_web_search
            assert all(task.timeout_seconds == 600 for task in tasks.values())
            assert {task.model for task in tasks.values()} <= {
                "gpt-5.6-luna",
                "gpt-5.6-terra",
                "gpt-5.6-sol",
            }
            assert all(
                task.reasoning_effort in {"low", "medium", "high"} for task in tasks.values()
            )


AZURE_ENVIRONMENT = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")


@pytest.mark.parametrize(
    ("endpoint_set", "api_key_set", "environment_fields", "expected"),
    [
        pytest.param(False, False, (), False, id="nothing-configured"),
        pytest.param(False, True, (), False, id="api-key-needs-endpoint"),
        pytest.param(
            False,
            False,
            AZURE_ENVIRONMENT,
            False,
            id="sdk-identity-needs-endpoint",
        ),
        pytest.param(True, False, (), False, id="endpoint-only"),
        pytest.param(
            True,
            False,
            AZURE_ENVIRONMENT[:-1],
            False,
            id="incomplete-sdk-identity",
        ),
        pytest.param(True, True, (), True, id="endpoint-and-api-key"),
        pytest.param(
            True,
            False,
            AZURE_ENVIRONMENT,
            True,
            id="endpoint-and-sdk-identity",
        ),
    ],
)
def test_azure_availability_requires_endpoint_and_key_or_complete_sdk_environment(
    monkeypatch: pytest.MonkeyPatch,
    endpoint_set: bool,
    api_key_set: bool,
    environment_fields: tuple[str, ...],
    expected: bool,
) -> None:
    for name in environment_fields:
        monkeypatch.setenv(name, "contoso-offline-identity-value")
    vendor = VendorSettings(
        endpoint=ENDPOINT if endpoint_set else "",
        api_key=SecretStr(OFFLINE_KEY if api_key_set else ""),
    )
    assert vendor.enabled is expected


@pytest.mark.parametrize("preference", ["entra_id", "api_key"])
def test_azure_uses_available_credentials_and_honors_preference_when_both_exist(
    monkeypatch: pytest.MonkeyPatch, preference: str
) -> None:
    vendor = VendorSettings.model_validate(
        {"endpoint": ENDPOINT, "auth_mode": preference, "api_key": OFFLINE_KEY}
    )
    assert vendor.effective_auth_mode == "api_key"
    for name in AZURE_ENVIRONMENT:
        monkeypatch.setenv(name, "contoso-offline-identity-value")
    assert vendor.effective_auth_mode == preference
    without_key = vendor.model_copy(update={"api_key": SecretStr("")})
    assert without_key.enabled and without_key.effective_auth_mode == "entra_id"


def test_vendor_availability_does_not_require_every_routed_vendor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CX_AI__OPENAI__API_KEY", OFFLINE_KEY)
    settings = load_ai_settings()
    assert settings.enabled
    assert settings.vendors["openai"].enabled
    assert not settings.vendors["azure_openai"].enabled
    assert all(task.vendor == "azure_openai" for task in settings.tasks.values())


def test_blank_sdk_environment_values_do_not_enable_azure(
    ai_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in AZURE_ENVIRONMENT:
        monkeypatch.setenv(name, " ")
    assert not load_ai_settings().enabled
