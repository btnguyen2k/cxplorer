"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cxplorer.auth.oauth import build_oauth
from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.middleware import SecurityHeadersMiddleware
from cxplorer.routers import authentication, private, public

STATIC_DIRECTORY = Path(__file__).parent / "static"


def create_app(
    app_settings: AppSettings | None = None,
    identity_settings: IdentityVendorSettings | None = None,
) -> FastAPI:
    """Create a configured CXplorer application."""
    app_settings = app_settings if app_settings is not None else AppSettings()
    identity_settings = (
        identity_settings if identity_settings is not None else IdentityVendorSettings()
    )
    docs_url = "/api/docs" if app_settings.expose_api_docs else None
    openapi_url = "/api/openapi.json" if app_settings.expose_api_docs else None

    app = FastAPI(
        title=app_settings.app_name,
        version="0.1.0",
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
    )
    app.state.app_settings = app_settings
    app.state.identity_settings = identity_settings
    app.state.oauth = build_oauth(identity_settings)

    # noinspection PyTypeChecker
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=app_settings.environment == "production",
    )

    # noinspection PyTypeChecker
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.session_secret.get_secret_value(),
        session_cookie="cxplorer_session",
        max_age=app_settings.session_max_age_seconds,
        same_site="lax",
        https_only=app_settings.use_secure_cookies,
    )

    # noinspection PyTypeChecker
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=app_settings.allowed_hosts)

    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIRECTORY),
        name="static",
    )
    app.include_router(public.router)
    app.include_router(authentication.router)
    app.include_router(private.web_router)
    app.include_router(private.api_router)
    return app
