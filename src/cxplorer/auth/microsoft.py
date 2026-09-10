"""Microsoft Entra ID issuer and signing-key scope validation."""

from collections.abc import Mapping
from uuid import UUID

from authlib.integrations.starlette_client import StarletteOAuth2App
from authlib.oauth2.claims import BaseClaims, ClaimsOption

from cxplorer.config import IdentityVendorSettings

MICROSOFT_ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenantid}/v2.0"
MICROSOFT_PERSONAL_TENANT_ID = "9188040d-6c67-4c5b-b112-36a304b66dad"


def _allowed_tenant(value: object, configured_tenant: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        tenant_id = str(UUID(value))
    except ValueError:
        return False
    if value.lower() != tenant_id:
        return False

    if configured_tenant == "common":
        return True
    if configured_tenant == "organizations":
        return tenant_id != MICROSOFT_PERSONAL_TENANT_ID
    if configured_tenant == "consumers":
        return tenant_id == MICROSOFT_PERSONAL_TENANT_ID
    return tenant_id == configured_tenant.lower()


def _matches_issuer(value: object, tenant_id: str, expected: str) -> bool:
    return isinstance(value, str) and value.replace("{tenantid}", tenant_id) == expected


def microsoft_claims_options(
    client: StarletteOAuth2App,
    settings: IdentityVendorSettings,
) -> dict[str, ClaimsOption]:
    """Bind validated ID tokens to their tenant, issuer, audience, and signing-key scope."""
    client_id = settings.ms_client_id
    if client_id is None or not client_id.strip():
        raise ValueError("Microsoft ID token validation requires a configured client ID")

    def validate_issuer(claims: BaseClaims, value: object) -> bool:
        tenant_id = claims.get("tid")
        if not isinstance(tenant_id, str) or not _allowed_tenant(tenant_id, settings.ms_tenant):
            return False
        expected = MICROSOFT_ISSUER_TEMPLATE.format(tenantid=tenant_id)
        if value != expected:
            return False

        metadata = client.server_metadata
        metadata_issuer = metadata.get("issuer")
        if not _matches_issuer(metadata_issuer, tenant_id, expected):
            return False

        # Authlib has verified the signature and refreshed unknown keys before this hook runs.
        jwks = metadata.get("jwks")
        if not isinstance(jwks, Mapping) or not isinstance(jwks.get("keys"), list):
            return False
        key_id = claims.header.get("kid")
        if not isinstance(key_id, str) or not key_id:
            return False
        matches = [
            key for key in jwks["keys"] if isinstance(key, Mapping) and key.get("kid") == key_id
        ]
        if len(matches) != 1:
            return False

        key = matches[0]
        if "issuer" not in key:
            return metadata_issuer == expected
        return _matches_issuer(key["issuer"], tenant_id, expected)

    # Authlib consumes validation hooks, so never reuse these options between callbacks.
    return {
        "iss": {"essential": True, "validate": validate_issuer},
        "tid": {"essential": True},
        "aud": {"essential": True, "value": client_id},
    }
