"""OAuth client registration."""

from authlib.integrations.starlette_client import OAuth

from cxplorer.auth.microsoft import MICROSOFT_ISSUER_TEMPLATE
from cxplorer.config import IdentityVendorSettings


def build_oauth(settings: IdentityVendorSettings) -> OAuth:
    """Register configured external identity providers."""
    oauth = OAuth()
    if settings.microsoft_auth_enabled:
        oauth.register(
            name="microsoft",
            client_id=settings.ms_client_id,
            client_secret=settings.ms_client_secret.get_secret_value()
            if settings.ms_client_secret
            else None,
            server_metadata_url=(
                MICROSOFT_ISSUER_TEMPLATE.format(tenantid=settings.ms_tenant)
                + "/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": "openid profile email"},
        )
    return oauth
