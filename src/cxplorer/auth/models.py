"""Validated identity stored in the signed application session."""

import hashlib
from collections.abc import Mapping
from typing import Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuthenticationClaimsError(ValueError):
    """Raised when an identity provider omits required validated claims."""


class AuthenticationEmailError(AuthenticationClaimsError):
    """Raised when a provider does not supply a usable email address."""


class AuthenticatedUser(BaseModel):
    """Minimal identity data retained after OpenID Connect login."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    provider: Literal["microsoft"]
    subject: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def usable_email(cls, value: str) -> str:
        return validate_email(value, check_deliverability=False).normalized

    @property
    def cache_namespace(self) -> str:
        return hashlib.sha256(self.email.encode("utf-8")).hexdigest()

    @classmethod
    def from_microsoft_claims(
        cls,
        claims: Mapping[str, object],
    ) -> "AuthenticatedUser":
        """Build a session identity from Authlib-validated Microsoft claims."""
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise AuthenticationClaimsError("Microsoft identity is missing the subject claim")

        display_name = _first_text(
            claims,
            "name",
            "given_name",
            "preferred_username",
            "email",
        )
        email = _first_text(claims, "email")
        if email is None:
            raise AuthenticationEmailError("The identity provider did not supply an email.")
        try:
            email = cls.usable_email(email)
        except EmailNotValidError as error:
            raise AuthenticationEmailError(
                "The identity provider did not supply a usable email."
            ) from error
        return cls(
            provider="microsoft",
            subject=subject.strip(),
            display_name=display_name or "CXplorer user",
            email=email,
        )


def _first_text(claims: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
