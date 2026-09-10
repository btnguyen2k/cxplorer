"""Nested AI settings with Pydantic-managed dotenv and environment sources."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict, SettingsError

VendorID = Literal["openai", "azure_openai"]
AudienceID = Literal["ceo", "cto", "cio", "cfo", "ciso"]
ReasoningEffort = Literal["low", "medium", "high"]
TASK_IDS = (
    "verify_sources",
    "discover_news",
    "extract_evidence",
    "consolidate_evidence",
    "synthesize_strategy",
    "generate_talk_points",
    "review_report",
)
VENDOR_IDS = ("openai", "azure_openai")
_AZURE_IDENTITY_ENV = ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET")


def _azure_identity_configured() -> bool:
    # Check presence only; Azure Identity still owns credential parsing and authentication.
    return all(os.environ.get(name, "").strip() for name in _AZURE_IDENTITY_ENV)


class AIConfigurationError(ValueError):
    """A configuration failure exposing setting names, never their values or file contents."""

    def __init__(self, setting_names: str | Sequence[str]) -> None:
        names = (setting_names,) if isinstance(setting_names, str) else setting_names
        self.errors = tuple(
            sorted(
                {
                    name
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,160}", name)
                    else "CX_AI_CONFIGURATION"
                    for name in names
                }
            )
        )
        super().__init__("Invalid or missing AI settings: " + ", ".join(self.errors))


def normalize_vendor_id(value: str) -> VendorID:
    """Accept the documented spelling variants, not arbitrary punctuation."""
    normalized = re.sub(r"[\s_-]+", "", value.strip()).casefold()
    if normalized == "openai":
        return "openai"
    if normalized == "azureopenai":
        return "azure_openai"
    raise ValueError("Unsupported vendor")


class _SettingsModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
    )


class VendorSettings(_SettingsModel):
    """Credentials stay in memory and are excluded even from model_dump(mode='json')."""

    vendor_id: VendorID = "azure_openai"
    endpoint: str = Field(default="", repr=False)
    auth_mode: Literal["entra_id", "api_key"] = "entra_id"
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), exclude=True, repr=False)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    request_timeout_seconds: float = Field(default=600.0, gt=0, le=900)
    max_retries: int = Field(default=2, ge=0, le=3)
    max_concurrency: int = Field(default=6, ge=1, le=16)
    organization: str | None = Field(default=None, max_length=256, repr=False)
    project: str | None = Field(default=None, max_length=256, repr=False)

    @property
    def has_api_key(self) -> bool:
        key = self.api_key.get_secret_value()
        return not _placeholder(key) and not any(char.isspace() for char in key)

    @property
    def enabled(self) -> bool:
        if self.vendor_id == "openai":
            return self.has_api_key
        return bool(self.endpoint) and (self.has_api_key or _azure_identity_configured())

    @property
    def effective_auth_mode(self) -> Literal["entra_id", "api_key"]:
        if self.vendor_id == "openai" or (
            self.has_api_key and (self.auth_mode == "api_key" or not _azure_identity_configured())
        ):
            return "api_key"
        return "entra_id"

    @field_validator("vendor_id", mode="before")
    @classmethod
    def normalize_vendor(cls, value: object) -> object:
        return normalize_vendor_id(value) if isinstance(value, str) else value

    @field_validator("auth_mode", mode="before")
    @classmethod
    def normalize_auth_mode(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("api_key", mode="before")
    @classmethod
    def protect_api_key(cls, value: object) -> object:
        return SecretStr(value.strip()) if isinstance(value, str) else value

    @field_validator("organization", "project", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip() or None
            if value is not None and any(ord(char) < 32 for char in value):
                raise ValueError("Invalid identifier")
        return value

    @model_validator(mode="after")
    def validate_vendor(self) -> VendorSettings:
        if self.vendor_id == "openai" and self.auth_mode != "api_key":
            raise AIConfigurationError("CX_AI__OPENAI__AUTH_MODE")
        if self.vendor_id == "azure_openai" and (self.organization or self.project):
            raise AIConfigurationError(
                ["CX_AI__AZURE_OPENAI__ORGANIZATION", "CX_AI__AZURE_OPENAI__PROJECT"]
            )
        object.__setattr__(self, "endpoint", normalize_endpoint(self.vendor_id, self.endpoint))
        if self.connect_timeout_seconds > self.request_timeout_seconds:
            raise AIConfigurationError(
                [
                    f"CX_AI__{self.vendor_id.upper()}__CONNECT_TIMEOUT_SECONDS",
                    f"CX_AI__{self.vendor_id.upper()}__REQUEST_TIMEOUT_SECONDS",
                ]
            )
        return self


class TaskSettings(_SettingsModel):
    vendor: VendorID = "azure_openai"
    model: str = Field(default="gpt-5.6-sol", min_length=1, max_length=200)
    prompt_version: Literal["v1"] = "v1"
    reasoning_effort: ReasoningEffort = "medium"
    use_web_search: bool = False
    max_tool_calls: int = Field(default=0, ge=0, le=25)
    max_input_tokens: int = Field(default=14000, ge=1, le=300000)
    max_output_tokens: int = Field(default=4500, ge=1, le=120000)
    timeout_seconds: float = Field(default=600.0, gt=0, le=900)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @field_validator("vendor", mode="before")
    @classmethod
    def normalize_vendor(cls, value: object) -> object:
        return normalize_vendor_id(value) if isinstance(value, str) else value

    @field_validator("model", "prompt_version", mode="before")
    @classmethod
    def normalize_identifier(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
                raise ValueError("Invalid identifier")
        return value

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def normalize_effort(cls, value: object) -> object:
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("temperature", mode="before")
    @classmethod
    def normalize_temperature(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("max_tool_calls")
    @classmethod
    def validate_tools(cls, value: int, info: ValidationInfo) -> int:
        if info.data.get("use_web_search") and value < 1:
            raise ValueError("Web search requires a positive tool budget")
        return value


class PipelineSettings(_SettingsModel):
    default_audiences: Annotated[tuple[AudienceID, ...], NoDecode] = (
        "ceo",
        "cto",
        "cio",
        "cfo",
        "ciso",
    )
    talk_points_per_audience: int = Field(default=3, ge=1, le=5)
    max_seed_urls: int = Field(default=6, ge=1, le=6)
    max_fetched_pages: int = Field(default=12, ge=1, le=12)
    fetch_concurrency: int = Field(default=4, ge=1, le=8)
    extraction_concurrency: int = Field(default=4, ge=1, le=8)
    audience_concurrency: int = Field(default=3, ge=1, le=5)
    fetch_timeout_seconds: float = Field(default=20.0, gt=0, le=20)
    max_redirects: int = Field(default=3, ge=0, le=3)
    max_html_bytes: int = Field(default=2097152, ge=1, le=2097152)
    max_pdf_bytes: int = Field(default=15728640, ge=1, le=15728640)
    max_pdf_pages: int = Field(default=200, ge=1, le=200)
    max_active_jobs_per_user: int = Field(default=1, ge=1, le=4)
    max_running_jobs: int = Field(default=2, ge=1, le=4)
    max_queued_jobs: int = Field(default=4, ge=0, le=16)
    max_model_calls: int = Field(default=40, ge=1, le=40)
    max_total_input_tokens: int = Field(default=300000, ge=1, le=300000)
    max_total_output_tokens: int = Field(default=120000, ge=1, le=120000)
    max_repair_rounds: int = Field(default=1, ge=0, le=1)
    job_timeout_seconds: float = Field(default=900.0, gt=0, le=900)
    news_days: int = Field(default=90, ge=90, le=90)
    max_news_articles: int = Field(default=3, ge=1, le=3)
    result_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    max_results: int = Field(default=16, ge=1, le=16)
    tokenizer_encoding: Literal["o200k_base", "cl100k_base"] = "o200k_base"
    token_estimate_multiplier: float = Field(default=1.25, ge=1.1, le=3)

    @field_validator("default_audiences", mode="before")
    @classmethod
    def normalize_audiences(cls, value: object) -> object:
        if isinstance(value, str):
            value = tuple(item.strip().casefold() for item in value.split(","))
        elif isinstance(value, (list, tuple)):
            value = tuple(
                item.strip().casefold() if isinstance(item, str) else item for item in value
            )
        if (
            isinstance(value, tuple)
            and all(isinstance(item, str) for item in value)
            and (not value or len(value) != len(set(value)))
        ):
            raise ValueError("Audiences must be nonempty and unique")
        return value

    @model_validator(mode="after")
    def validate_capacity(self) -> PipelineSettings:
        if self.max_fetched_pages < self.max_seed_urls:
            raise AIConfigurationError(
                ["CX_AI_PIPELINE__MAX_FETCHED_PAGES", "CX_AI_PIPELINE__MAX_SEED_URLS"]
            )
        return self


def _default_vendors() -> dict[str, VendorSettings]:
    return {
        "openai": VendorSettings(vendor_id="openai", auth_mode="api_key"),
        "azure_openai": VendorSettings(),
    }


def _default_tasks() -> dict[str, TaskSettings]:
    return {
        "verify_sources": TaskSettings(
            reasoning_effort="low",
            max_input_tokens=12000,
            max_output_tokens=2500,
        ),
        "discover_news": TaskSettings(
            use_web_search=True,
            max_tool_calls=25,
            max_input_tokens=32000,
            max_output_tokens=6000,
        ),
        "extract_evidence": TaskSettings(
            model="gpt-5.6-luna",
            reasoning_effort="low",
            max_input_tokens=12000,
            max_output_tokens=4000,
        ),
        "consolidate_evidence": TaskSettings(
            model="gpt-5.6-terra",
            max_input_tokens=30000,
            max_output_tokens=6000,
        ),
        "synthesize_strategy": TaskSettings(
            model="gpt-5.6-terra",
            reasoning_effort="high",
            max_input_tokens=24000,
            max_output_tokens=8000,
        ),
        "generate_talk_points": TaskSettings(),
        "review_report": TaskSettings(
            model="gpt-5.6-terra",
            reasoning_effort="high",
            max_input_tokens=32000,
            max_output_tokens=6000,
        ),
    }


def _registry_defaults[ConfigT: BaseModel](value: object, defaults: dict[str, ConfigT]) -> object:
    if not isinstance(value, dict):
        return value
    # Pydantic merges source layers, but partial dictionaries replace their default entries.
    merged: dict[str, object] = dict(defaults)
    for name, settings in value.items():
        if not isinstance(name, str) or name.casefold() not in defaults:
            raise ValueError("Unknown AI registry entry")
        key = name.casefold()
        merged[key] = (
            {**dict(defaults[key]), **settings} if isinstance(settings, dict) else settings
        )
    return merged


class AIVendorSettings(BaseSettings):
    """Vendor connections loaded from the CX_AI configuration tree."""

    vendors: dict[str, VendorSettings] = Field(
        alias="CX_AI", default_factory=_default_vendors, repr=False
    )
    model_config = SettingsConfigDict(
        env_file=("ai_vendors.env", "ai_vendors.local.env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    @field_validator("vendors", mode="before")
    @classmethod
    def vendor_defaults(cls, value: object) -> object:
        return _registry_defaults(value, _default_vendors())

    @field_validator("vendors")
    @classmethod
    def matching_vendor_ids(cls, value: dict[str, VendorSettings]) -> dict[str, VendorSettings]:
        for name, vendor in value.items():
            if vendor.vendor_id != name:
                raise AIConfigurationError(f"CX_AI__{name.upper()}__VENDOR_ID")
        return value


class AITaskSettings(BaseSettings):
    """Task routing and pipeline limits loaded from their separate configuration trees."""

    tasks: dict[str, TaskSettings] = Field(alias="CX_AI_TASK", default_factory=_default_tasks)
    pipeline: PipelineSettings = Field(alias="CX_AI_PIPELINE", default_factory=PipelineSettings)
    model_config = SettingsConfigDict(
        env_file=("ai_tasks.env", "ai_tasks.local.env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    @field_validator("tasks", mode="before")
    @classmethod
    def task_defaults(cls, value: object) -> object:
        return _registry_defaults(value, _default_tasks())


class AISettings(_SettingsModel):
    vendors: dict[str, VendorSettings] = Field(default_factory=_default_vendors, repr=False)
    tasks: dict[str, TaskSettings] = Field(default_factory=_default_tasks)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)

    @property
    def enabled(self) -> bool:
        return any(vendor.enabled for vendor in self.vendors.values())

    @property
    def fingerprint(self) -> str:
        """Fingerprint routing and budgets only; rotating a credential does not change it."""
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def normalize_endpoint(vendor_id: str, endpoint: str) -> str:
    """Accept a resource root or a v1 base, never a deployment/Responses URL or URL credentials."""
    name = f"CX_AI__{vendor_id.upper()}__ENDPOINT"
    endpoint = endpoint.strip()
    if not endpoint:
        return ""
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
            or any(char.isspace() or ord(char) < 32 for char in endpoint)
            or "\\" in endpoint
        ):
            raise ValueError("Invalid endpoint")
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise ValueError("Invalid endpoint")
        path = parsed.path.rstrip("/")
        target = "/openai/v1" if vendor_id == "azure_openai" else "/v1"
        if path not in {"", target}:
            raise ValueError("Invalid endpoint")
        return urlunsplit(("https", host, target + "/", "", ""))
    except (ValueError, UnicodeError):
        raise AIConfigurationError(name) from None


def _placeholder(value: str) -> bool:
    text = value.strip().casefold()
    return (
        not text
        or text
        in {
            "...",
            "secret",
            "placeholder",
            "example",
            "sample",
            "dummy",
            "todo",
            "tbd",
            "changeme",
            "replace_me",
            "sk-...",
        }
        or text.startswith(("${", "<", "remember to ", "please ", "todo:"))
        or re.match(r"^(?:your|example|dummy|sample|placeholder|insert|replace|change)[ _-]", text)
        is not None
        or re.match(r"^sk-(?:your|placeholder|replace)(?:[ _-]|$)", text) is not None
    )


def _load_settings[SettingsT: BaseSettings](
    model_type: type[SettingsT], filename: str | Path, label: str
) -> SettingsT:
    path = Path(filename)
    try:
        return model_type(_env_file=(path, path.with_name(path.stem + ".local" + path.suffix)))
    except ValidationError as error:
        names: list[str] = []
        for detail in error.errors(include_input=False, include_url=False):
            original = detail.get("ctx", {}).get("error")
            if isinstance(original, AIConfigurationError):
                names.extend(original.errors)
                continue
            names.append(
                "__".join(part.upper() for part in detail["loc"] if isinstance(part, str)) or label
            )
        raise AIConfigurationError(names) from None
    except (SettingsError, OSError, UnicodeError):
        raise AIConfigurationError(label) from None


def load_ai_settings(
    vendor_file: str | Path = "ai_vendors.env",
    task_file: str | Path = "ai_tasks.env",
) -> AISettings:
    """Combine the native settings sources without acquiring or managing Azure credentials."""
    vendors = _load_settings(AIVendorSettings, vendor_file, "CX_AI").vendors
    task_settings = _load_settings(AITaskSettings, task_file, "CX_AI_TASK")
    tasks, pipeline = task_settings.tasks, task_settings.pipeline
    errors: list[str] = []
    if not tasks["discover_news"].use_web_search:
        errors.append("CX_AI_TASK__DISCOVER_NEWS__USE_WEB_SEARCH")
    for task_id, task in tasks.items():
        if task.max_input_tokens > pipeline.max_total_input_tokens:
            errors.append(f"CX_AI_TASK__{task_id.upper()}__MAX_INPUT_TOKENS")
        if task.max_output_tokens > pipeline.max_total_output_tokens:
            errors.append(f"CX_AI_TASK__{task_id.upper()}__MAX_OUTPUT_TOKENS")
        if task.timeout_seconds > pipeline.job_timeout_seconds:
            errors.append(f"CX_AI_TASK__{task_id.upper()}__TIMEOUT_SECONDS")
    if errors:
        raise AIConfigurationError(errors)
    return AISettings(vendors=vendors, tasks=tasks, pipeline=pipeline)
