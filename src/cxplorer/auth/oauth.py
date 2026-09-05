"""OAuth client registration."""

from authlib.integrations.starlette_client import OAuth

from cxplorer.config import Settings


def build_oauth(settings: Settings) -> OAuth:
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
                "https://login.microsoftonline.com/"
                f"{settings.ms_tenant}/v2.0/.well-known/openid-configuration"
            ),
            client_kwargs={"scope": "openid profile email"},
        )
    return oauth
