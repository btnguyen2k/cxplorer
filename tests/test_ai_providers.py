"""Single-request provider tests with typed SDK responses and wholly offline dependencies."""

import asyncio
import json
import sys
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.credentials import AccessToken
from azure.identity import CredentialUnavailableError
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
    OpenAIError,
)
from openai.types.responses import Response
from openai.types.responses.response_error import ResponseError
from openai.types.responses.response_output_item import ResponseFunctionToolCall
from pydantic import BaseModel, ConfigDict, Field, RootModel, SecretStr, create_model

from cxplorer.ai.config import AISettings, TaskSettings, VendorSettings
from cxplorer.ai.providers import ENTRA_SCOPE, AIProviderClient, ProviderError, Usage

OFFLINE_KEY = "contoso-offline-provider-key"
OFFLINE_TOKEN = "contoso-offline-entra-token"
OFFICIAL_HOST = "contoso.example"
ARTIFACT = '{"name":"Contoso","count":2,"note":null}'


@pytest.fixture(autouse=True)
def provider_environment(isolated_ai_environment: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


@pytest.fixture
def azure_identity_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_TENANT_ID", "contoso-tenant")
    monkeypatch.setenv("AZURE_CLIENT_ID", "contoso-client")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", OFFLINE_KEY)


def _sdk_http_backend() -> Any:
    # SDK exports can rewrite __module__; actual client class identity is authoritative.
    for name in ("httpx2", "httpx"):
        backend = sys.modules.get(name)
        if backend is not None and issubclass(DefaultAsyncHttpxClient, backend.AsyncClient):
            return backend
    raise AssertionError("The OpenAI SDK HTTP backend was not identified")


http_backend = _sdk_http_backend()


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=16)
    count: int = Field(ge=1, le=3)
    note: str | None = None


class Company(BaseModel):
    name: str = Field(min_length=1, max_length=16)


class NestedArtifact(BaseModel):
    company: Company
    audiences: list[str] = Field(min_length=1, max_length=5)


class ByteTokenizer:
    """An injected deterministic fake, not the application's real token estimator."""

    def __init__(self) -> None:
        self.inputs: list[str] = []

    def encode(self, text: str, *, disallowed_special: tuple[()] = ()) -> bytes:
        assert disallowed_special == ()
        self.inputs.append(text)
        return text.encode("utf-8")


def sdk_response(
    text: str = ARTIFACT,
    *,
    status: str = "completed",
    search_calls: list[dict[str, Any]] | None = None,
    refusal: bool = False,
    usage: bool = True,
    annotations: list[dict[str, Any]] | None = None,
) -> Response:
    content = (
        {"type": "refusal", "refusal": OFFLINE_KEY}
        if refusal
        else {"type": "output_text", "text": text, "annotations": annotations or []}
    )
    response = Response.model_validate(
        {
            "id": "resp_contoso",
            "object": "response",
            "created_at": 1700000000,
            "model": "contoso-deployment",
            "status": status,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "tools": [],
            "output": [
                *(search_calls or []),
                {
                    "id": "msg_contoso",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [content],
                },
            ],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
                "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 20},
            }
            if usage
            else None,
        }
    )
    object.__setattr__(response, "_request_id", "req_contoso")
    return response


def expected_usage(tool_calls: int = 0) -> Usage:
    return Usage(
        input_tokens=120,
        output_tokens=30,
        tool_calls=tool_calls,
        cached_input_tokens=40,
        cache_write_tokens=0,
        reasoning_tokens=20,
        total_tokens=150,
    )


def search_call(
    *,
    url: str = "https://contoso.example/news",
    kind: str = "search",
    status: str = "completed",
) -> dict[str, Any]:
    action = (
        {
            "type": "search",
            "query": "site:contoso.example official announcements",
            "sources": [{"type": "url", "url": url}],
        }
        if kind == "search"
        else {
            "type": kind,
            "url": url,
            **({"pattern": "Contoso"} if kind == "find_in_page" else {}),
        }
    )
    return {"id": "ws_contoso", "type": "web_search_call", "action": action, "status": status}


def ai_settings(vendor: str = "azure_openai", auth_mode: str = "api_key") -> AISettings:
    settings = AISettings()
    vendors = dict(settings.vendors)
    vendors[vendor] = VendorSettings(
        vendor_id=vendor,
        endpoint=(
            "https://contoso.openai.azure.com"
            if vendor == "azure_openai"
            else "https://api.openai.com"
        ),
        auth_mode=auth_mode,
        api_key=SecretStr(OFFLINE_KEY) if auth_mode == "api_key" else SecretStr(""),
    )
    return settings.model_copy(
        update={
            "vendors": vendors,
            "tasks": {
                task_id: task.model_copy(update={"vendor": vendor})
                for task_id, task in settings.tasks.items()
            },
        }
    )


class FakeSDK:
    def __init__(self, response: Response) -> None:
        self.responses = SimpleNamespace(create=AsyncMock(return_value=response))
        self.close = AsyncMock()


def provider(
    response: Response | None = None,
    *,
    settings: AISettings | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[AIProviderClient, FakeSDK, Mock, ByteTokenizer]:
    sdk = FakeSDK(response if response is not None else sdk_response())
    factory = Mock(return_value=sdk)
    tokenizer = ByteTokenizer()
    client = AIProviderClient(
        settings or ai_settings(),
        client_factory=factory,
        tokenizer_factory=lambda _name: tokenizer,
        clock=clock,
    )
    return client, sdk, factory, tokenizer


def generate(client: AIProviderClient, task_id: str = "verify_sources", **kwargs: Any) -> Any:
    return asyncio.run(
        client.generate(
            task_id,
            "Use the supplied Contoso evidence.",
            {"company": "Contoso"},
            Artifact,
            **kwargs,
        )
    )


@pytest.mark.parametrize("vendor", ["openai", "azure_openai"])
def test_same_sdk_contract_native_strict_output_and_no_retries(vendor: str) -> None:
    client, sdk, factory, _ = provider(settings=ai_settings(vendor))
    assert not factory.called
    generated = generate(client)
    sdk.responses.create.assert_awaited_once()
    request = sdk.responses.create.call_args.kwargs
    options = factory.call_args.kwargs
    assert options["max_retries"] == 0
    assert options["api_key"] == OFFLINE_KEY
    assert options["base_url"].endswith("/openai/v1/" if vendor == "azure_openai" else "/v1/")
    assert options["timeout"].read == 600 and options["timeout"].connect == 10
    assert request["model"] == "gpt-5.6-sol"
    assert request["reasoning"] == {"effort": "low"}
    assert request["store"] is False and request["stream"] is False
    assert request["truncation"] == "disabled"
    assert request["max_output_tokens"] == 2500
    assert request["timeout"].read == 600 and request["timeout"].connect == 10
    assert not (
        {"response_format", "tools", "previous_response_id", "conversation"} & request.keys()
    )
    schema_format = request["text"]["format"]
    assert schema_format["type"] == "json_schema" and schema_format["strict"] is True
    schema = schema_format["schema"]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert schema["required"] == ["name", "count", "note"]
    assert "default" not in schema["properties"]["note"]
    assert "maxLength" not in schema["properties"]["name"]
    assert "maximum" not in schema["properties"]["count"]
    assert json.loads(request["input"][0]["content"]) == {"company": "Contoso"}
    assert generated.value == Artifact(name="Contoso", count=2, note=None)
    assert generated.request_id == "req_contoso"
    assert generated.usage == expected_usage()
    asyncio.run(client.close())
    sdk.close.assert_awaited_once()


def test_model_call_logs_safe_settings_duration_and_detailed_usage(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Mock(side_effect=[100.0, 112.3456])
    client, _, _, _ = provider(clock=clock)
    private_prompt = "contoso-private-prompt-marker"
    with caplog.at_level("INFO", logger="cxplorer.ai.providers"):
        generated = asyncio.run(
            client.generate(
                "verify_sources",
                private_prompt,
                {"source_text": private_prompt},
                Artifact,
            )
        )
    messages = [record.getMessage() for record in caplog.records]
    assert generated.usage == expected_usage()
    assert len(messages) == 2
    assert messages[0] == (
        "AI task before: task=verify_sources vendor=azure_openai "
        "model=gpt-5.6-sol reasoning_effort=low web_search_enabled=false "
        "max_tool_calls=0 timeout_seconds=600.000"
    )
    assert "status=completed error_code=none duration_seconds=12.346" in messages[1]
    assert "input_tokens=120 cached_input_tokens=40 cache_write_tokens=0" in messages[1]
    assert "output_tokens=30 reasoning_tokens=20 total_tokens=150" in messages[1]
    assert "web_search_calls=0 request_id=req_contoso" in messages[1]
    assert private_prompt not in caplog.text
    assert OFFLINE_KEY not in caplog.text


def test_model_call_logs_unavailable_optional_usage_without_inventing_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = sdk_response()
    response.usage = response.usage.model_copy(
        update={
            "input_tokens_details": None,
            "output_tokens_details": None,
            "total_tokens": None,
        }
    )
    client, _, _, _ = provider(response, clock=Mock(side_effect=[5.0, 6.0]))
    with caplog.at_level("INFO", logger="cxplorer.ai.providers"):
        generated = generate(client)
    assert generated.usage == Usage(120, 30)
    finished = caplog.records[-1].getMessage()
    assert "cached_input_tokens=unavailable cache_write_tokens=unavailable" in finished
    assert "reasoning_tokens=unavailable total_tokens=unavailable" in finished


@pytest.mark.parametrize(
    ("factory", "code"),
    [
        (lambda request: APITimeoutError(request=request), "timeout"),
        (
            lambda request: APIConnectionError(message=OFFLINE_TOKEN, request=request),
            "provider_unavailable",
        ),
    ],
)
def test_failed_model_calls_log_safe_code_and_duration_without_sdk_details(
    caplog: pytest.LogCaptureFixture,
    factory: Callable[[Any], Exception],
    code: str,
) -> None:
    client, sdk, _, _ = provider(clock=Mock(side_effect=[20.0, 620.0]))
    sdk.responses.create.side_effect = factory(
        http_backend.Request(
            "POST",
            "https://private-provider.contoso.invalid",
            headers={"Authorization": OFFLINE_KEY},
        )
    )
    with (
        caplog.at_level("INFO", logger="cxplorer.ai.providers"),
        pytest.raises(ProviderError) as caught,
    ):
        generate(client)
    assert caught.value.code == code
    finished = caplog.records[-1].getMessage()
    assert f"status=failed error_code={code} duration_seconds=600.000" in finished
    assert "input_tokens=unavailable" in finished
    assert OFFLINE_KEY not in caplog.text
    assert OFFLINE_TOKEN not in caplog.text
    assert "private-provider" not in caplog.text


def test_rejected_model_output_logs_known_usage(caplog: pytest.LogCaptureFixture) -> None:
    client, _, _, _ = provider(sdk_response(refusal=True), clock=Mock(side_effect=[50.0, 52.0]))
    with (
        caplog.at_level("INFO", logger="cxplorer.ai.providers"),
        pytest.raises(ProviderError) as caught,
    ):
        generate(client)
    assert caught.value.code == "refusal"
    finished = caplog.records[-1].getMessage()
    assert "status=failed error_code=refusal duration_seconds=2.000" in finished
    assert "input_tokens=120 cached_input_tokens=40" in finished
    assert "reasoning_tokens=20 total_tokens=150" in finished
    assert OFFLINE_KEY not in caplog.text


def test_invalid_output_logs_safe_contract_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_output = "contoso-private-output-marker"
    response = sdk_response(json.dumps({"name": private_output, "count": 2, "note": None}))
    client, _, _, _ = provider(response, clock=Mock(side_effect=[70.0, 72.0]))
    with (
        caplog.at_level("INFO", logger="cxplorer.ai.providers"),
        pytest.raises(ProviderError) as caught,
    ):
        generate(client)
    details = caught.value.contract_details
    assert details is not None
    assert details.stage == "model_validation"
    assert details.error_count == 1
    assert details.locations == ("name",)
    assert details.error_types == ("string_too_long",)
    assert (
        "AI output contract detail: task=verify_sources vendor=azure_openai "
        "model=gpt-5.6-sol output_type=Artifact stage=model_validation "
        "validation_errors=1 locations=name types=string_too_long request_id=req_contoso"
        in caplog.text
    )
    assert private_output not in caplog.text
    assert OFFLINE_KEY not in caplog.text


def test_openai_key_alone_uses_the_sdk_default_endpoint() -> None:
    configured = ai_settings("openai")
    configured.vendors["openai"] = VendorSettings(
        vendor_id="openai", auth_mode="api_key", api_key=SecretStr(OFFLINE_KEY)
    )
    requests = []

    def respond(request):
        requests.append(request)
        return http_backend.Response(200, json=sdk_response().model_dump(mode="json"))

    def sdk_factory(**kwargs):
        assert "base_url" not in kwargs
        return AsyncOpenAI(
            **kwargs,
            http_client=http_backend.AsyncClient(transport=http_backend.MockTransport(respond)),
        )

    client = AIProviderClient(
        configured, client_factory=sdk_factory, tokenizer_factory=lambda _name: ByteTokenizer()
    )
    generate(client)
    assert str(requests[0].url) == "https://api.openai.com/v1/responses"
    assert requests[0].headers["Authorization"] == "Bearer " + OFFLINE_KEY
    asyncio.run(client.close())


def test_azure_key_enables_key_auth_without_sdk_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_factory = Mock()
    monkeypatch.setattr("cxplorer.ai.providers.DefaultAzureCredential", credential_factory)
    configured = ai_settings()
    configured.vendors["azure_openai"] = configured.vendors["azure_openai"].model_copy(
        update={"auth_mode": "entra_id"}
    )
    client, _, factory, _ = provider(settings=configured)
    generate(client)
    assert factory.call_args.kwargs["api_key"] == OFFLINE_KEY
    credential_factory.assert_not_called()
    asyncio.run(client.close())


@pytest.mark.parametrize("required_vendor", ["openai", "azure_openai"])
@pytest.mark.parametrize("missing_entry", [False, True])
def test_unconfigured_task_vendor_fails_before_any_sdk_or_tokenizer_work(
    required_vendor: str, missing_entry: bool
) -> None:
    available_vendor = "azure_openai" if required_vendor == "openai" else "openai"
    configured = ai_settings(available_vendor)
    if missing_entry:
        del configured.vendors[required_vendor]
    configured.tasks["verify_sources"] = configured.tasks["verify_sources"].model_copy(
        update={"vendor": required_vendor}
    )
    assert configured.enabled
    client, sdk, factory, tokenizer = provider(settings=configured)
    for operation in (
        lambda: generate(client),
        lambda: client.estimate_input_tokens("verify_sources", "Contoso", {}, Artifact),
    ):
        with pytest.raises(ProviderError) as caught:
            operation()
        assert caught.value.code == "vendor_disabled" and not caught.value.retryable
        assert "verify_sources" in caught.value.public_message
        assert ("OpenAI" if required_vendor == "openai" else "AzureOpenAI") in str(caught.value)
        assert "ai_tasks.env" in str(caught.value)
        assert OFFLINE_KEY not in str(caught.value)
    factory.assert_not_called()
    sdk.responses.create.assert_not_awaited()
    assert not tokenizer.inputs
    asyncio.run(client.close())


def test_nested_properties_are_closed_and_bounds_stay_python_validated() -> None:
    response = sdk_response('{"company":{"name":"Contoso"},"audiences":["ceo"]}')
    client, sdk, _, _ = provider(response)
    result = asyncio.run(client.generate("verify_sources", "Contoso", {}, NestedArtifact))
    assert result.value.company.name == "Contoso"
    schema = sdk.responses.create.call_args.kwargs["text"]["format"]["schema"]
    assert schema["$defs"]["Company"]["additionalProperties"] is False
    assert schema["$defs"]["Company"]["required"] == ["name"]
    assert "maxItems" not in schema["properties"]["audiences"]
    sdk.responses.create.return_value = sdk_response(
        '{"company":{"name":"Contoso"},"audiences":[]}'
    )
    with pytest.raises(ProviderError, match="output contract"):
        asyncio.run(client.generate("verify_sources", "Contoso", {}, NestedArtifact))
    sdk.responses.create.return_value = sdk_response(
        '{"company":{"name":"Contoso","unapproved":true},"audiences":["ceo"]}'
    )
    with pytest.raises(ProviderError, match="output contract"):
        asyncio.run(client.generate("verify_sources", "Contoso", {}, NestedArtifact))


def test_model_parameters_are_not_silently_dropped() -> None:
    settings = ai_settings()
    settings.tasks["verify_sources"] = settings.tasks["verify_sources"].model_copy(
        update={"reasoning_effort": "high", "temperature": 0.2, "model": "contoso-exact-deployment"}
    )
    client, sdk, _, _ = provider(settings=settings)
    generate(client)
    request = sdk.responses.create.call_args.kwargs
    assert request["reasoning"] == {"effort": "high"}
    assert request["temperature"] == 0.2
    assert request["model"] == "contoso-exact-deployment"


@pytest.mark.parametrize("api_key_also_configured", [False, True])
def test_entra_identity_is_lazy_async_refreshable_and_closed(
    api_key_also_configured: bool,
    monkeypatch: pytest.MonkeyPatch,
    azure_identity_environment: None,
) -> None:
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken(OFFLINE_TOKEN, int(time.time()) + 3600)),
        close=AsyncMock(),
    )
    default_factory = Mock(return_value=credential)
    monkeypatch.setattr("cxplorer.ai.providers.DefaultAzureCredential", default_factory)
    configured = ai_settings(auth_mode="entra_id")
    if api_key_also_configured:
        configured.vendors["azure_openai"] = configured.vendors["azure_openai"].model_copy(
            update={"api_key": SecretStr(OFFLINE_KEY)}
        )
    requests: list[http_backend.Request] = []
    options: list[dict[str, Any]] = []

    def respond(request: http_backend.Request) -> http_backend.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer " + OFFLINE_TOKEN
        return http_backend.Response(200, json=sdk_response().model_dump(mode="json"))

    def sdk_factory(**kwargs: Any) -> AsyncOpenAI:
        options.append(kwargs)
        return AsyncOpenAI(
            **kwargs,
            http_client=http_backend.AsyncClient(transport=http_backend.MockTransport(respond)),
        )

    client = AIProviderClient(
        configured,
        client_factory=sdk_factory,
        tokenizer_factory=lambda _name: ByteTokenizer(),
    )
    client.estimate_input_tokens("verify_sources", "Contoso", {}, Artifact)
    assert not default_factory.called and not requests

    async def scenario() -> None:
        await client.generate("verify_sources", "Contoso", {}, Artifact)
        await client.generate("verify_sources", "Contoso", {}, Artifact)
        await client.close()

    asyncio.run(scenario())
    assert len(requests) == 2 and len(options) == 1
    assert callable(options[0]["api_key"]) and options[0]["max_retries"] == 0
    credential.get_token.assert_awaited_once()
    assert credential.get_token.call_args.args == (ENTRA_SCOPE,)
    default_factory.assert_called_once_with(
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
    )
    credential.close.assert_awaited_once()


def test_entra_unavailable_is_non_transient_and_never_leaks_sdk_error(
    azure_identity_environment: None,
) -> None:
    credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=CredentialUnavailableError(OFFLINE_KEY)),
        close=AsyncMock(),
    )
    sdk = FakeSDK(sdk_response())

    def factory(**kwargs: Any) -> FakeSDK:
        async def create(**_request: Any) -> Response:
            await kwargs["api_key"]()
            return sdk_response()

        sdk.responses.create.side_effect = create
        return sdk

    client = AIProviderClient(
        ai_settings(auth_mode="entra_id"),
        client_factory=factory,
        credential_factory=Mock(return_value=credential),
        tokenizer_factory=lambda _name: ByteTokenizer(),
    )
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "authentication_unavailable" and not caught.value.retryable
    assert OFFLINE_KEY not in str(caught.value) and OFFLINE_KEY not in repr(caught.value)
    sdk.responses.create.assert_awaited_once()
    asyncio.run(client.close())
    credential.close.assert_awaited_once()
    sdk.close.assert_awaited_once()


def test_missing_async_identity_transport_is_an_explicit_auth_failure(
    azure_identity_environment: None,
) -> None:
    client = AIProviderClient(
        ai_settings(auth_mode="entra_id"),
        credential_factory=Mock(side_effect=ImportError("contoso-async-transport")),
        tokenizer_factory=lambda _name: ByteTokenizer(),
    )
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "authentication_unavailable" and not caught.value.retryable
    assert "contoso-async-transport" not in str(caught.value)
    asyncio.run(client.close())


def test_failed_token_provider_construction_keeps_credential_for_cleanup(
    azure_identity_environment: None,
) -> None:
    credential = SimpleNamespace(close=AsyncMock())
    credential_factory = Mock(return_value=credential)
    client = AIProviderClient(
        ai_settings(auth_mode="entra_id"),
        credential_factory=credential_factory,
        token_provider_factory=Mock(side_effect=ValueError(OFFLINE_KEY)),
        tokenizer_factory=lambda _name: ByteTokenizer(),
    )
    for _ in range(2):
        with pytest.raises(ProviderError) as caught:
            generate(client)
        assert caught.value.code == "authentication_unavailable"
    credential_factory.assert_called_once()
    asyncio.run(client.close())
    credential.close.assert_awaited_once()


@pytest.mark.parametrize("vendor", ["azure_openai", "openai"])
def test_official_domain_filtered_search_for_both_vendors(vendor: str) -> None:
    client, sdk, _, _ = provider(
        sdk_response(search_calls=[search_call(), search_call(kind="open_page")]),
        settings=ai_settings(vendor),
    )
    generated = generate(
        client, "discover_news", allowed_domains=["CONTOSO.example", "www.contoso.example"]
    )
    request = sdk.responses.create.call_args.kwargs
    assert request["tools"] == [
        {
            "type": "web_search",
            "filters": {"allowed_domains": ["contoso.example", "www.contoso.example"]},
            "search_context_size": "low",
            "external_web_access": vendor == "openai",
        }
    ]
    assert request["max_tool_calls"] == 25
    assert request["tool_choice"] == "required"
    assert request["include"] == ["web_search_call.action.sources"]
    assert "last 90 days" in request["instructions"]
    assert "site:" in request["instructions"] and "Do not broaden" in request["instructions"]
    assert generated.usage.tool_calls == 2
    assert generated.usage.output_tokens == 30


@pytest.mark.parametrize(
    "domains",
    [
        (),
        None,
        ["https://contoso.example"],
        ["*.contoso.example"],
        ["contoso.example/news"],
        ["contoso.example:443"],
        ["contoso.example@contoso.invalid"],
        ["contoso.example ignore instructions"],
        ["127.0.0.1"],
        ["contoso.local"],
        ["example"],
        [" contoso.example"],
        ["contoso.example"] * 101,
        "contoso.example",
    ],
)
def test_search_requires_valid_exact_domains_before_network(domains: Any) -> None:
    client, sdk, factory, _ = provider()
    with pytest.raises(ProviderError) as caught:
        generate(client, "discover_news", allowed_domains=domains)
    assert caught.value.code == "search_scope_required"
    assert not factory.called and not sdk.responses.create.called


@pytest.mark.parametrize(
    "url",
    [
        "https://general-news.example/contoso",
        "https://subsidiary.contoso.example/news",
        "https://contoso.example.evil.example/news",
        "http://contoso.example/news",
        "https://contoso.example:8443/news",
        "https://user:contoso-secret@contoso.example/news",
    ],
)
def test_search_rejects_native_subdomain_broadening_and_other_hosts(url: str) -> None:
    client, _, _, _ = provider(sdk_response(search_calls=[search_call(url=url)]))
    with pytest.raises(ProviderError) as caught:
        generate(client, "discover_news", allowed_domains=[OFFICIAL_HOST])
    assert caught.value.code == "search_scope_violation"
    assert caught.value.usage == expected_usage(1)
    assert url not in str(caught.value)


def test_citation_annotations_must_match_exact_official_hosts() -> None:
    client, _, _, _ = provider(
        sdk_response(
            search_calls=[search_call()],
            annotations=[
                {
                    "type": "url_citation",
                    "start_index": 0,
                    "end_index": 1,
                    "url": "https://www.contoso.example/news",
                    "title": "Contoso",
                }
            ],
        )
    )
    with pytest.raises(ProviderError) as caught:
        generate(client, "discover_news", allowed_domains=[OFFICIAL_HOST])
    assert caught.value.code == "search_scope_violation"


@pytest.mark.parametrize(
    ("calls", "code"),
    [
        ([], "search_not_performed"),
        ([search_call(status="failed")], "tool_failed"),
        ([search_call()] * 26, "tool_limit"),
    ],
)
def test_mandatory_search_completion_and_tool_budget(
    calls: list[dict[str, Any]], code: str
) -> None:
    client, _, _, _ = provider(sdk_response(search_calls=calls))
    with pytest.raises(ProviderError) as caught:
        generate(client, "discover_news", allowed_domains=[OFFICIAL_HOST])
    assert caught.value.code == code and not caught.value.retryable


def test_unconfigured_tool_is_not_accepted() -> None:
    client, _, _, _ = provider(sdk_response(search_calls=[search_call()]))
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "unexpected_tool"


def test_arbitrary_function_output_is_never_executed_or_accepted() -> None:
    response = sdk_response()
    response.output.insert(
        0,
        ResponseFunctionToolCall(
            type="function_call",
            call_id="call_contoso",
            name="contoso_unapproved_action",
            arguments="{}",
        ),
    )
    client, sdk, _, _ = provider(response)
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "unexpected_tool"
    sdk.responses.create.assert_awaited_once()


def test_discovery_cannot_bypass_search_with_disabled_settings() -> None:
    settings = ai_settings()
    settings.tasks["discover_news"] = settings.tasks["discover_news"].model_copy(
        update={"use_web_search": False}
    )
    client, sdk, factory, _ = provider(settings=settings)
    with pytest.raises(ProviderError) as caught:
        generate(client, "discover_news")
    assert caught.value.code == "configuration"
    assert not factory.called and not sdk.responses.create.called


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (sdk_response(refusal=True), "refusal"),
        (sdk_response(refusal=True, usage=False), "refusal"),
        (sdk_response('{"name":', status="incomplete"), "incomplete_output"),
        (sdk_response(status="failed"), "invalid_response"),
        (sdk_response(status="in_progress"), "invalid_response"),
        (sdk_response(usage=False), "usage_unavailable"),
        (sdk_response(""), "invalid_output"),
        (sdk_response("```json\n" + ARTIFACT + "\n```"), "invalid_output"),
        (sdk_response(ARTIFACT + ARTIFACT), "invalid_output"),
        (sdk_response('{"name":"Contoso","count":"2","note":null}'), "invalid_output"),
        (sdk_response('{"name":"Contoso","count":2.0,"note":null}'), "invalid_output"),
        (sdk_response('{"name":"Contoso","count":9,"note":null}'), "invalid_output"),
        (sdk_response('{"name":"Contoso","count":2}'), "invalid_output"),
        (
            sdk_response('{"name":"Contoso","name":"Contoso","count":2,"note":null}'),
            "invalid_output",
        ),
        (sdk_response('{"name":"Contoso","count":2,"note":null,"extra":1}'), "invalid_output"),
        (sdk_response('{"name":"Contoso","count":NaN,"note":null}'), "invalid_output"),
        (
            sdk_response('{"name":"Contoso-too-long-a-company","count":2,"note":null}'),
            "invalid_output",
        ),
    ],
)
def test_refusals_truncation_and_invalid_artifacts_are_distinct(
    response: Response, code: str
) -> None:
    client, sdk, _, _ = provider(response)
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == code and not caught.value.retryable
    assert caught.value.request_id == "req_contoso"
    if response.usage is not None:
        assert caught.value.usage == expected_usage()
    assert OFFLINE_KEY not in str(caught.value)
    sdk.responses.create.assert_awaited_once()


@pytest.mark.parametrize("value", [-1, True, "120", None])
def test_malformed_usage_never_becomes_free_success(value: object) -> None:
    response = sdk_response()
    response.usage = response.usage.model_copy(update={"input_tokens": value})
    client, _, _, _ = provider(response)
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "invalid_response" and caught.value.usage is None


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("input", "cached_tokens"),
        ("input", "cache_write_tokens"),
        ("output", "reasoning_tokens"),
        ("usage", "total_tokens"),
    ],
)
def test_malformed_detailed_usage_is_rejected(section: str, field: str) -> None:
    response = sdk_response()
    if section == "input":
        details = response.usage.input_tokens_details.model_copy(update={field: -1})
        response.usage = response.usage.model_copy(update={"input_tokens_details": details})
    elif section == "output":
        details = response.usage.output_tokens_details.model_copy(update={field: -1})
        response.usage = response.usage.model_copy(update={"output_tokens_details": details})
    else:
        response.usage = response.usage.model_copy(update={field: -1})
    client, _, _, _ = provider(response)
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "invalid_response" and caught.value.usage is None


@pytest.mark.parametrize(
    ("provider_code", "expected_code", "retryable"),
    [
        ("server_error", "provider_unavailable", True),
        ("rate_limit_exceeded", "rate_limited", True),
        ("invalid_prompt", "provider_error", False),
    ],
)
def test_failed_response_metadata_preserves_only_safe_codes_and_usage(
    provider_code: str, expected_code: str, retryable: bool
) -> None:
    response = sdk_response(status="failed")
    response.error = ResponseError.model_construct(code=provider_code, message=OFFLINE_KEY)
    client, _, _, _ = provider(response)
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == expected_code and caught.value.retryable is retryable
    assert caught.value.usage == expected_usage()
    assert OFFLINE_KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "invalid_request", False),
        (401, "authentication_failed", False),
        (403, "permission_denied", False),
        (404, "deployment_unavailable", False),
        (408, "timeout", True),
        (409, "provider_unavailable", True),
        (422, "invalid_request", False),
        (429, "rate_limited", True),
        (500, "provider_unavailable", True),
        (501, "provider_error", False),
        (502, "provider_unavailable", True),
        (503, "provider_unavailable", True),
        (504, "provider_unavailable", True),
    ],
)
def test_provider_status_errors_are_sanitized_and_do_not_retry(
    status: int, code: str, retryable: bool
) -> None:
    client, sdk, _, _ = provider()
    request = http_backend.Request(
        "POST", "https://contoso.example", headers={"Authorization": OFFLINE_KEY}
    )
    sdk.responses.create.side_effect = APIStatusError(
        OFFLINE_KEY,
        response=http_backend.Response(status, request=request, headers={"retry-after": "2.5"}),
        body={"message": OFFLINE_KEY, "code": "contoso-provider-code"},
    )
    with pytest.raises(ProviderError) as caught:
        generate(client)
    error = caught.value
    assert error.code == code and error.retryable is retryable
    assert error.retry_after == (2.5 if retryable else None)
    assert error.usage is None
    assert OFFLINE_KEY not in str(error) and OFFLINE_KEY not in repr(error)
    assert "contoso.example" not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    sdk.responses.create.assert_awaited_once()


@pytest.mark.parametrize(
    ("factory", "code", "retryable"),
    [
        (lambda request: APITimeoutError(request=request), "timeout", True),
        (
            lambda request: APIConnectionError(message=OFFLINE_KEY, request=request),
            "provider_unavailable",
            True,
        ),
        (
            lambda request: APIResponseValidationError(
                response=http_backend.Response(200, request=request), body={"message": OFFLINE_KEY}
            ),
            "invalid_response",
            False,
        ),
        (lambda _request: OpenAIError(OFFLINE_KEY), "provider_error", False),
    ],
)
def test_sdk_transport_and_protocol_errors(
    factory: Callable[[http_backend.Request], Exception], code: str, retryable: bool
) -> None:
    client, sdk, _, _ = provider()
    sdk.responses.create.side_effect = factory(
        http_backend.Request("POST", "https://contoso.example")
    )
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == code and caught.value.retryable is retryable
    assert OFFLINE_KEY not in str(caught.value)
    sdk.responses.create.assert_awaited_once()


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after": "-1"}, 0),
        ({"retry-after": "contoso-secret"}, None),
        ({"retry-after": "nan"}, None),
        ({"retry-after": "inf"}, None),
        ({"retry-after-ms": "invalid", "retry-after": "3"}, 3),
    ],
)
def test_retry_after_metadata_is_parsed_without_leaking(
    headers: dict[str, str], expected: Any
) -> None:
    client, sdk, _, _ = provider()
    sdk.responses.create.side_effect = APIStatusError(
        OFFLINE_KEY,
        response=http_backend.Response(
            429, request=http_backend.Request("POST", "https://contoso.example"), headers=headers
        ),
        body=None,
    )
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.retry_after == expected


def test_quota_exhaustion_is_not_a_transient_rate_limit() -> None:
    client, sdk, _, _ = provider()
    sdk.responses.create.side_effect = APIStatusError(
        OFFLINE_KEY,
        response=http_backend.Response(
            429, request=http_backend.Request("POST", "https://contoso.example")
        ),
        body={"code": "insufficient_quota"},
    )
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "quota_exceeded" and not caught.value.retryable


def test_native_search_rejection_never_falls_back_to_unfiltered_search() -> None:
    client, sdk, _, _ = provider()
    sdk.responses.create.side_effect = APIStatusError(
        "Unsupported filters with " + OFFLINE_KEY,
        response=http_backend.Response(
            400, request=http_backend.Request("POST", "https://contoso.example")
        ),
        body=None,
    )
    with pytest.raises(ProviderError) as caught:
        generate(client, "discover_news", allowed_domains=[OFFICIAL_HOST])
    assert caught.value.code == "invalid_request"
    sdk.responses.create.assert_awaited_once()


def test_input_estimation_includes_schema_messages_tools_and_safety_overhead() -> None:
    client, _, factory, tokenizer = provider()
    estimate = client.estimate_input_tokens("verify_sources", "Contoso <|endoftext|>", {}, Artifact)
    encoded_request = json.loads(tokenizer.inputs[-1])
    assert encoded_request["text"]["format"]["schema"]["properties"]
    assert encoded_request["input"] and "instructions" in encoded_request
    assert estimate >= len(tokenizer.inputs[-1].encode("utf-8")) * 1.25 + 256
    assert (
        client.estimate_input_tokens("verify_sources", "Contoso <|endoftext|>", {}, Artifact)
        == estimate
    )
    searched = client.estimate_input_tokens(
        "discover_news", "Contoso", {}, Artifact, allowed_domains=[OFFICIAL_HOST]
    )
    assert "tools" in json.loads(tokenizer.inputs[-1])
    assert searched == client.settings.tasks["discover_news"].max_input_tokens
    assert not factory.called


def test_input_limit_rejected_before_sdk_or_credential_creation() -> None:
    client, sdk, factory, _ = provider()
    with pytest.raises(ProviderError) as caught:
        asyncio.run(client.generate("verify_sources", "Contoso", {"text": "x" * 20000}, Artifact))
    assert caught.value.code == "input_limit"
    assert not factory.called and not sdk.responses.create.called


def test_invalid_input_and_unknown_task_are_not_sent() -> None:
    client, sdk, _, _ = provider()
    for data in (
        {"number": float("nan")},
        {"value": object()},
        {1: "Contoso"},
        {"nested": {1: "Contoso"}},
        {"value": "\ud800"},
    ):
        with pytest.raises(ProviderError) as caught:
            asyncio.run(client.generate("verify_sources", "Contoso", data, Artifact))
        assert caught.value.code == "invalid_input"
    with pytest.raises(ProviderError) as caught:
        generate(client, "contoso-unknown-task")
    assert caught.value.code == "unknown_task"
    assert not sdk.responses.create.called


def test_unsupported_schema_root_and_arbitrary_maps_fail_before_request() -> None:
    class ArbitraryMap(BaseModel):
        values: dict[str, str]

    client, sdk, _, _ = provider()
    for output_type in (RootModel[list[str]], ArbitraryMap):
        with pytest.raises(ProviderError) as caught:
            asyncio.run(client.generate("verify_sources", "Contoso", {}, output_type))
        assert caught.value.code == "schema_unsupported"
    assert not sdk.responses.create.called


def test_azure_schema_property_ceiling_is_enforced() -> None:
    output_type = create_model(
        "ContosoLargeArtifact", **{f"field_{i}": (str, ...) for i in range(101)}
    )
    client, sdk, _, _ = provider()
    with pytest.raises(ProviderError) as caught:
        asyncio.run(client.generate("verify_sources", "Contoso", {}, output_type))
    assert caught.value.code == "schema_unsupported"
    assert not sdk.responses.create.called


def test_referenced_schemas_are_not_rejected_by_speculative_vendor_depth_counting() -> None:
    class ContosoCriterion(BaseModel):
        baseline_fact_ids: list[str]

    class ContosoOpportunity(BaseModel):
        success_criteria: list[ContosoCriterion]

    class ContosoStrategy(BaseModel):
        opportunities: list[ContosoOpportunity]

    client, sdk, factory, _ = provider()
    estimate = client.estimate_input_tokens("synthesize_strategy", "Contoso", {}, ContosoStrategy)
    assert estimate > 0
    assert not factory.called and not sdk.responses.create.called


def test_tokenizer_failure_is_explicit_and_offline() -> None:
    factory = Mock(side_effect=OSError("contoso-tokenizer-error"))
    client = AIProviderClient(ai_settings(), tokenizer_factory=factory)
    with pytest.raises(ProviderError) as caught:
        client.estimate_input_tokens("verify_sources", "Contoso", {}, Artifact)
    assert caught.value.code == "tokenizer_unavailable"
    assert "contoso-tokenizer-error" not in str(caught.value)
    factory.assert_called_once_with("o200k_base")


def test_close_without_use_is_lazy_and_idempotent() -> None:
    client, sdk, factory, _ = provider()
    asyncio.run(client.close())
    asyncio.run(client.close())
    assert not factory.called and not sdk.close.called
    with pytest.raises(ProviderError) as caught:
        generate(client)
    assert caught.value.code == "closed"


def test_cleanup_attempts_every_created_client_even_when_one_close_fails() -> None:
    settings = ai_settings()
    settings.vendors["openai"] = VendorSettings(
        vendor_id="openai",
        endpoint="https://api.openai.com/v1/",
        auth_mode="api_key",
        api_key=OFFLINE_KEY,
    )
    settings.tasks["extract_evidence"] = TaskSettings(vendor="openai")
    sdks = [FakeSDK(sdk_response()), FakeSDK(sdk_response())]
    sdks[0].close.side_effect = RuntimeError(OFFLINE_KEY)
    client = AIProviderClient(
        settings,
        client_factory=Mock(side_effect=sdks),
        tokenizer_factory=lambda _name: ByteTokenizer(),
    )
    generate(client)
    generate(client, "extract_evidence")
    with pytest.raises(ProviderError) as caught:
        asyncio.run(client.close())
    assert caught.value.code == "cleanup_failed"
    assert OFFLINE_KEY not in str(caught.value)
    for sdk in sdks:
        sdk.close.assert_awaited_once()
    asyncio.run(client.close())


def test_concurrent_closes_wait_for_the_same_cleanup() -> None:
    client, sdk, _, _ = provider()
    generate(client)

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_close() -> None:
            entered.set()
            await release.wait()

        sdk.close.side_effect = slow_close
        first = asyncio.create_task(client.close())
        await entered.wait()
        second = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        assert not second.done()
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    sdk.close.assert_awaited_once()


def test_cancellation_is_not_converted_or_retried() -> None:
    client, sdk, _, _ = provider()
    sdk.responses.create.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        generate(client)
    sdk.responses.create.assert_awaited_once()
