"""Application and identity-provider configuration with separate dotenv sources."""

import json
import re
from pathlib import Path
from typing import Annotated, Literal, Self

from email_validator import EmailNotValidError, validate_email
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _normalize_login_email_rule(value: str) -> str:
    rule = value.strip()
    if not rule:
        return ""
    if len(rule) > 320 or any(character.isspace() for character in rule):
        raise ValueError("LOGIN_ALLOWED_EMAILS contains an invalid email rule")
    try:
        if "*" not in rule:
            return validate_email(rule, check_deliverability=False).normalized.casefold()
        if not rule.isascii() or rule.count("@") != 1:
            raise EmailNotValidError("Invalid wildcard email rule")
        validate_email(rule.replace("*", "wildcard"), check_deliverability=False)
    except EmailNotValidError:
        raise ValueError(
            "LOGIN_ALLOWED_EMAILS entries must be email addresses or whole-email '*' patterns"
        ) from None
    return rule.casefold()


def _email_matches_rule(email: str, rule: str) -> bool:
    expression = re.escape(rule).replace(r"\*", ".*")
    return re.fullmatch(expression, email, flags=re.IGNORECASE) is not None


class AppSettings(BaseSettings):
    """Generic application defaults with optional private local overrides."""

    model_config = SettingsConfigDict(
        env_file=("app_config.env", "app_config.local.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "CXplorer"
    environment: Literal["development", "test", "production"] = "development"
    session_secret: SecretStr = Field(min_length=32)
    session_cookie_secure: bool | None = None
    session_max_age_seconds: int = Field(default=60 * 60 * 8, ge=300, le=60 * 60 * 24 * 30)
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"],
        min_length=1,
    )
    login_allowed_emails: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(),
        max_length=100,
    )
    docs_enabled: bool | None = None
    reload: bool = False
    ai_vendor_config_file: Path = Path("ai_vendors.env")
    ai_task_config_file: Path = Path("ai_tasks.env")

    @field_validator("login_allowed_emails", mode="before")
    @classmethod
    def normalize_login_allowed_emails(cls, value: object) -> tuple[str, ...]:
        if value is None or (isinstance(value, str) and not value.strip()):
            return ()
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    value = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    raise ValueError(
                        "LOGIN_ALLOWED_EMAILS must be a JSON array or comma-separated list"
                    ) from None
            else:
                value = text.split(",")
        if not isinstance(value, (list, tuple)):
            raise ValueError("LOGIN_ALLOWED_EMAILS must be a list")
        normalized_rules = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("LOGIN_ALLOWED_EMAILS entries must be strings")
            rule = _normalize_login_email_rule(item)
            if rule:
                normalized_rules.append(rule)
        normalized = tuple(normalized_rules)
        if len(normalized) != len(set(normalized)):
            raise ValueError("LOGIN_ALLOWED_EMAILS must not contain duplicate rules")
        return normalized

    def allows_login_email(self, email: str) -> bool:
        """Return whether a provider-validated email passes the optional login allowlist."""
        if not self.login_allowed_emails:
            return True
        try:
            normalized = validate_email(email, check_deliverability=False).normalized.casefold()
        except EmailNotValidError:
            return False
        return any(_email_matches_rule(normalized, rule) for rule in self.login_allowed_emails)

    @property
    def use_secure_cookies(self) -> bool:
        """Enable HTTPS-only cookies by default outside local development and tests."""
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.environment == "production"

    @property
    def expose_api_docs(self) -> bool:
        """Expose interactive API documentation unless production disables it by default."""
        if self.docs_enabled is not None:
            return self.docs_enabled
        return self.environment != "production"


class IdentityVendorSettings(BaseSettings):
    """External identity defaults with optional private local overrides."""

    model_config = SettingsConfigDict(
        env_file=("id_vendor.env", "id_vendor.local.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    ms_client_id: str | None = None
    ms_client_secret: SecretStr | None = None
    ms_tenant: str = Field(
        default="common",
        pattern=r"^(common|organizations|consumers|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$",
    )

    @model_validator(mode="after")
    def validate_ms_credentials(self) -> Self:
        """Require the Microsoft client ID and secret as a pair."""
        has_client_id = bool(self.ms_client_id and self.ms_client_id.strip())
        has_client_secret = bool(
            self.ms_client_secret and self.ms_client_secret.get_secret_value().strip()
        )
        if has_client_id != has_client_secret:
            raise ValueError("MS_CLIENT_ID and MS_CLIENT_SECRET must be configured together")
        return self

    @property
    def microsoft_auth_enabled(self) -> bool:
        """Return whether Microsoft authentication is configured."""
        return bool(
            self.ms_client_id
            and self.ms_client_secret
            and self.ms_client_id.strip()
            and self.ms_client_secret.get_secret_value().strip()
        )
