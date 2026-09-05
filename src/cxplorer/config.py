"""Application configuration loaded from environment variables."""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings with secure production defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
    docs_enabled: bool | None = None

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
