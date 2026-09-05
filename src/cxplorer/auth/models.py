"""Validated identity stored in the signed application session."""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuthenticationClaimsError(ValueError):
    """Raised when an identity provider omits required validated claims."""


class AuthenticatedUser(BaseModel):
    """Minimal identity data retained after OpenID Connect login."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["microsoft"]
    subject: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)

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
        email = _first_text(claims, "email", "preferred_username")
        return cls(
            provider="microsoft",
            subject=subject.strip(),
            display_name=display_name or "Microsoft user",
            email=email,
        )


def _first_text(claims: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
